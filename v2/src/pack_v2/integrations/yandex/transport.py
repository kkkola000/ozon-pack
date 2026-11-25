"""Bounded Partner API calls; read POSTs and mutation attempts have separate policies."""

import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime

import httpx

from pack_v2.integrations.instances import OutcomeUnknown
from pack_v2.integrations.reading import ReadError

BASE_URL = "https://api.partner.market.yandex.ru"


def error(kind, **values):
    return ReadError(kind, marketplace="yandex", **values)


def positive(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, str))
        or not str(value).isascii()
        or not str(value).isdigit()
        or not 0 < int(value) < 2**63
    ):
        raise error("invalid_response")
    return str(int(value))


def retry_after(value):
    if not value:
        return None
    try:
        return (
            max(0, int(value))
            if value.isascii() and value.isdigit()
            else max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        )
    except (ValueError, TypeError, OverflowError):
        return None


def body(data):
    value = data.get("result", data)
    if not isinstance(value, dict):
        raise error("invalid_response")
    return value


def _constant(value):
    raise ValueError("Non-finite JSON")


class YandexTransport:
    def __init__(self, credentials, *, transport=None, attempts=3, wait=time.sleep, instances=False):
        self.business = positive(credentials.business_id)
        self.orders_path = f"/v1/businesses/{self.business}/orders"
        self.catalog_path = f"/v2/businesses/{self.business}/offer-mappings"
        self.attempts, self.wait, self.instances = attempts, wait, instances
        self.client = httpx.Client(
            base_url=BASE_URL,
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(10, connect=5),
            headers={"Api-Key": credentials.api_key.get_secret_value(), "Accept": "application/json"},
        )

    def close(self):
        self.client.close()

    def read(self, method, path, payload=None, params=None):
        for attempt in range(self.attempts):
            try:
                return self.once(method, path, payload, params)
            except ReadError as failure:
                last = failure
            if not last.retryable or attempt + 1 == self.attempts or (last.retry_after or 0) > 2:
                raise last from None
            self.wait(last.retry_after or 0.15 * (attempt + 1))
        raise last

    def once(self, method, path, payload=None, params=None, *, write=False):
        read = (method, path) in {
            ("POST", self.orders_path),
            ("POST", self.catalog_path),
            ("GET", "/v2/campaigns"),
        }
        check = method == "POST" and re.fullmatch(
            r"/v2/campaigns/[1-9][0-9]*/orders/[1-9][0-9]*/identifiers/status", path
        )
        change = method == "PUT" and re.fullmatch(r"/v2/campaigns/[1-9][0-9]*/orders/[1-9][0-9]*/boxes", path)
        if not (read and not write or self.instances and (check and not write or change and write)):
            raise ValueError("Undeclared Yandex operation")
        try:
            with self.client.stream(method, path, json=payload, params=params) as response:
                status = response.status_code
                if status != 200:
                    kind = {
                        401: "unauthorized",
                        403: "denied",
                        404: "not_found",
                        420: "rate_limited",
                        429: "rate_limited",
                    }.get(status, "unavailable" if status >= 500 else "invalid_request")
                    raise error(
                        kind, status=status, retry_after=retry_after(response.headers.get("retry-after"))
                    )
                data = bytearray()
                for part in response.iter_bytes(chunk_size=8192):
                    data.extend(part)
                    if len(data) > 8 * 1024 * 1024:
                        raise error("invalid_response")
                try:
                    result = json.loads(data, parse_float=Decimal, parse_constant=_constant)
                except (ValueError, RecursionError):
                    raise error("invalid_response") from None
                if (
                    not isinstance(result, dict)
                    or result.get("errors")
                    or result.get("status") not in (None, "OK")
                ):
                    raise error("invalid_response")
                return result
        except httpx.HTTPError:
            if write:
                raise OutcomeUnknown("yandex") from None
            raise error("unavailable") from None
        except ReadError as failure:
            if write and (failure.status is None or failure.status >= 500):
                raise OutcomeUnknown("yandex") from None
            raise
