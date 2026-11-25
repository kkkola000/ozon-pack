"""Async label tasks, bounded PDF downloads and no credentials on CDN requests."""

from urllib.parse import urljoin, urlsplit

import httpx

from pack_v2.integrations.documents import MAX_LABEL_BYTES, LabelTask
from pack_v2.integrations.reading import ReadError

from .transport import BASE_URL, OzonTransport, body_of, retry_after

CREATE = "/v3/posting/fbs/package-label/create"
GET = "/v2/posting/fbs/package-label/get"


def label_url(raw):
    if not isinstance(raw, str) or not raw or len(raw) > 8192 or any(ord(c) < 33 for c in raw) or "\\" in raw:
        raise ReadError("invalid_response")
    try:
        url = urljoin(BASE_URL + "/", raw)
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ReadError("invalid_response") from None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or port not in (None, 443)
        or parts.username
        or parts.password
        or parts.fragment
        or not any(host == d or host.endswith("." + d) for d in ("ozon.ru", "ozone.ru"))
    ):
        raise ReadError("invalid_response")
    return url


class OzonLabelAdapter:
    def __init__(self, source, credentials, *, transport=None, file_transport=None):
        if source.marketplace != "ozon":
            raise ValueError("Wrong marketplace")
        self.http = OzonTransport(credentials, transport=transport, attempts=1)
        self.files = httpx.Client(
            transport=file_transport,
            follow_redirects=False,
            timeout=httpx.Timeout(20, connect=5),
            headers={"Accept": "application/pdf"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()
        self.files.close()

    def create(self, posting_number, size):
        if (
            size not in ("small", "big")
            or not isinstance(posting_number, str)
            or not 1 <= len(posting_number) <= 256
        ):
            raise ValueError("Invalid label request")
        data = body_of(self.http._once(CREATE, {"posting_numbers": [posting_number]}))
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ReadError("incomplete")
        chosen = [t for t in tasks if isinstance(t, dict) and t.get("task_type") == size + "_label"]
        if (
            len(chosen) != 1
            or type(chosen[0].get("task_id")) is not int
            or not 0 < chosen[0]["task_id"] < 2**63
        ):
            raise ReadError("invalid_response")
        return str(chosen[0]["task_id"])

    def poll(self, task_id, posting_number):
        if (
            not isinstance(task_id, str)
            or not task_id.isascii()
            or not task_id.isdigit()
            or not 0 < int(task_id) < 2**63
        ):
            raise ValueError("Invalid label task")
        data = body_of(self.http._once(GET, {"task_id": int(task_id)}))
        status = data.get("status", {})
        if not isinstance(status, dict):
            raise ReadError("invalid_response")
        missing = status.get("unprinted_postings", [])
        if not isinstance(missing, list) or any(not isinstance(p, dict) for p in missing):
            raise ReadError("invalid_response")
        if missing:
            # One task is requested for one posting; partial PDFs must never be served.
            raise ReadError("invalid_response")
        code = str(status.get("code") or "").lower()
        if code in ("error", "failed", "failure") or data.get("error"):
            raise ReadError("invalid_response")
        if code in ("pending", "processing", "in_progress", "created", "waiting"):
            return LabelTask("pending")
        if code in ("", "success", "completed", "done", "ready", "processed") and data.get("file_url"):
            return LabelTask("ready", label_url(data["file_url"]))
        raise ReadError("invalid_response")

    def download(self, url):
        current = label_url(url)
        # Each redirect is checked. Seller API headers are supplied only for its exact host.
        for _ in range(4):
            headers = (
                {key: self.http.client.headers[key] for key in ("Client-Id", "Api-Key")}
                if urlsplit(current).hostname == "api-seller.ozon.ru"
                else {}
            )
            with self.files.stream("GET", current, headers=headers) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise ReadError("invalid_response")
                    current = label_url(urljoin(current, location))
                    continue
                if response.status_code != 200:
                    status = response.status_code
                    raise ReadError(
                        "rate_limited"
                        if status == 429
                        else "unavailable"
                        if status >= 500
                        else "invalid_response",
                        status=status,
                        retry_after=retry_after(response.headers.get("retry-after"), self.http.clock()),
                    )
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    data.extend(chunk)
                    if len(data) > MAX_LABEL_BYTES:
                        raise ReadError("invalid_response")
                if not data.startswith(b"%PDF-"):
                    raise ReadError("invalid_response")
                return bytes(data)
        raise ReadError("invalid_response")
