"""Original per-box PDFs. Page-to-box identity never depends on a combined PDF's order."""

import io
import re

import httpx
from pypdf import PdfReader, PdfWriter

from pack_v2.integrations.documents import MAX_LABEL_BYTES, LabelTask

from .reader import YandexReadAdapter, layout_digest
from .transport import error, positive, retry_after


class YandexLabelAdapter(YandexReadAdapter):
    def create(self, posting_number, size):
        self._resolve(posting_number)
        return "yandex-original"

    def _resolve(self, number):
        raw = self.raw_order(number)
        if raw is None:
            raise error("not_found", status=404)
        if self.shipment(raw).state not in ("awaiting_assembly", "awaiting_dispatch"):
            raise error("invalid_request", status=409)
        delivery = raw.get("delivery") or {}
        boxes = delivery.get("boxesLayout") or []
        shipment = (delivery.get("shipment") or {}).get("id")
        if not boxes or shipment is None:
            raise error("incomplete")
        base = f"/v2/campaigns/{positive(raw['campaignId'])}/orders/{positive(number)}/delivery/shipments/{positive(shipment)}/boxes"
        paths = [
            f"{base}/{positive(box['boxId'])}/label?format=A7"
            for box in sorted(boxes, key=lambda box: int(positive(box["boxId"])))
        ]
        if len(set(paths)) != len(paths):
            raise error("invalid_response")
        self.identity_digest = layout_digest(raw)
        return paths

    def poll(self, task_id, posting_number):
        if task_id != "yandex-original":
            raise error("invalid_request")
        self.paths = self._resolve(posting_number)
        self.token = "yandex-original:" + positive(posting_number)
        return LabelTask("ready", self.token, self.identity_digest)

    def download(self, url):
        if (
            not isinstance(url, str)
            or not re.fullmatch(r"yandex-original:[1-9][0-9]*", url)
            or url != getattr(self, "token", None)
            or not getattr(self, "paths", None)
        ):
            raise error("invalid_request")
        writer = PdfWriter()
        size = 0
        for path in self.paths:
            data = self._download(path)
            size += len(data)
            if size > MAX_LABEL_BYTES:
                raise error("invalid_response")
            try:
                document = PdfReader(io.BytesIO(data), strict=True)
                if document.is_encrypted or len(document.pages) != 1:
                    raise ValueError("Wrong page count")
                writer.add_page(document.pages[0])
            except Exception:
                raise error("invalid_response") from None
        result = io.BytesIO()
        writer.write(result)
        if len(result.getvalue()) > MAX_LABEL_BYTES:
            raise error("invalid_response")
        return result.getvalue()

    def _download(self, path):
        if not re.fullmatch(
            r"/v2/campaigns/[1-9][0-9]*/orders/[1-9][0-9]*/delivery/shipments/[1-9][0-9]*/boxes/[1-9][0-9]*/label\?format=A7",
            path,
        ):
            raise error("invalid_request")
        try:
            with self.http.client.stream("GET", path, headers={"Accept": "application/pdf"}) as response:
                if response.status_code != 200:
                    status = response.status_code
                    raise error(
                        {
                            401: "unauthorized",
                            403: "denied",
                            404: "not_found",
                            420: "rate_limited",
                            429: "rate_limited",
                        }.get(status, "unavailable" if status >= 500 else "invalid_request"),
                        status=status,
                        retry_after=retry_after(response.headers.get("retry-after")),
                    )
                data = bytearray()
                for chunk in response.iter_bytes(8192):
                    data.extend(chunk)
                    if len(data) > MAX_LABEL_BYTES:
                        raise error("invalid_response")
                if not data.startswith(b"%PDF-"):
                    raise error("invalid_response")
                return bytes(data)
        except httpx.HTTPError:
            raise error("unavailable") from None
