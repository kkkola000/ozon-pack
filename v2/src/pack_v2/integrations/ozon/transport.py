import json
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime

import httpx

from pack_v2.integrations.contracts import OzonCredentials
from pack_v2.integrations.reading import ReadError

BASE_URL = "https://api-seller.ozon.ru"
READ_PATHS = frozenset(
    {
        "/v3/product/list",
        "/v3/product/info/list",
        "/v4/posting/fbs/list",
        "/v3/posting/fbs/get",
        "/v2/posting/fbs/get-by-barcode",
        "/v1/returns/list",
    }
)


def _reject_constant(value):
    raise ValueError("Non-finite JSON")


def body_of(data: dict) -> dict:
    if "result" not in data:
        return data
    if not isinstance(data["result"], dict):
        raise ReadError("invalid_response")
    return data["result"]


def retry_after(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        if value.isascii() and value.isdigit():
            return int(value)
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return max(0.0, (date - now).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


class OzonTransport:
    """A deliberately restricted transport for idempotent read POSTs only."""

    def __init__(
        self,
        credentials: OzonCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        attempts: int = 3,
        response_limit: int = 8 * 1024 * 1024,
        wait: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if not 1 <= attempts <= 5 or response_limit < 1:
            raise ValueError("Invalid read policy")
        self.attempts, self.response_limit, self.wait, self.clock = attempts, response_limit, wait, clock
        self.client = httpx.Client(
            base_url=BASE_URL,
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(10, connect=5),
            headers={
                "Client-Id": credentials.client_id,
                "Api-Key": credentials.api_key.get_secret_value(),
                "Accept": "application/json",
            },
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.client.close()

    def read(self, path: str, payload: dict) -> dict:
        if path not in READ_PATHS:
            raise ValueError("Only declared read endpoints are allowed")
        last = None
        for attempt in range(self.attempts):
            try:
                return self._once(path, payload)
            except ReadError as error:
                last = error
            except httpx.HTTPError:
                last = ReadError("unavailable")
            if not last.retryable or attempt + 1 == self.attempts:
                break
            # A long server delay belongs in a future worker's schedule, not a short sleep.
            if last.retry_after is not None and last.retry_after > 2:
                break
            delay = (
                last.retry_after if last.retry_after is not None else random.uniform(0.1, 0.25 * 2**attempt)
            )
            self.wait(delay)
        raise last from None

    def _once(self, path: str, payload: dict) -> dict:
        with self.client.stream("POST", path, json=payload) as response:
            status = response.status_code
            if status != 200:
                kind = {401: "unauthorized", 403: "denied", 404: "not_found", 429: "rate_limited"}.get(status)
                kind = kind or ("unavailable" if status >= 500 else "invalid_request")
                raise ReadError(
                    kind,
                    status=status,
                    retry_after=retry_after(response.headers.get("retry-after"), self.clock()),
                )
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                data.extend(chunk)
                if len(data) > self.response_limit:
                    raise ReadError("invalid_response")
            try:
                parsed = json.loads(data, parse_float=Decimal, parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                raise ReadError("invalid_response") from None
            if not isinstance(parsed, dict):
                raise ReadError("invalid_response")
            for envelope in (parsed, parsed.get("result")):
                if isinstance(envelope, dict) and (
                    envelope.get("error")
                    or envelope.get("errors")
                    or envelope.get("code") not in (None, 0, "0")
                ):
                    raise ReadError("invalid_response")
            return parsed
