import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import httpx

from pack_v2.integrations.contracts import OzonCredentials
from pack_v2.integrations.reading import (
    WORKING_STATES,
    AccountSource,
    OfferSnapshot,
    ReadError,
    ReadPage,
    ShipmentSnapshot,
)

from . import normalize
from .transport import OzonTransport, body_of

FILTER_STATES = {
    "awaiting_assembly": ("awaiting_packaging",),
    "awaiting_dispatch": ("awaiting_deliver",),
    "in_delivery": ("acceptance_in_progress", "delivering", "driver_pickup"),
    "delivered": ("delivered",),
    "cancelled": ("cancelled",),
}


def _cursor(value) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ReadError("invalid_response")
    return value


def _hint(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ReadError("invalid_response")
    if isinstance(value, str) and (not value.isascii() or not value.isdigit() or len(value) > 20):
        raise ReadError("invalid_response")
    result = int(value)
    if result < 0:
        raise ReadError("invalid_response")
    return result


def _date(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Use timezone-aware dates")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OzonReadAdapter:
    """Lazy, bounded page reads; no database, no raw payload export, no mutation methods."""

    def __init__(
        self,
        source: AccountSource,
        credentials: OzonCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        attempts: int = 3,
        max_pages: int = 200,
        catalog_page_size: int = 1000,
        posting_page_size: int = 100,
        info_chunk_size: int = 100,
        wait: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if source.marketplace != "ozon" or source.credential_revision < 0:
            raise ValueError("Wrong account source")
        if (
            not 1 <= max_pages <= 10_000
            or not 1 <= catalog_page_size <= 1000
            or not 1 <= posting_page_size <= 100
            or not 1 <= info_chunk_size <= 100
        ):
            raise ValueError("Invalid page policy")
        self.source, self.clock = source, clock
        self.max_pages, self.catalog_page_size = max_pages, catalog_page_size
        self.posting_page_size, self.info_chunk_size = posting_page_size, info_chunk_size
        self.http = OzonTransport(credentials, transport=transport, attempts=attempts, wait=wait, clock=clock)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def iter_catalog(self, *, cursor: str = "") -> Iterator[ReadPage[OfferSnapshot]]:
        cursor = _cursor(cursor)
        cursors, seen = {cursor}, set()
        for _ in range(self.max_pages):
            data = body_of(
                self.http.read(
                    "/v3/product/list",
                    {"filter": {"visibility": "ALL"}, "last_id": cursor, "limit": self.catalog_page_size},
                )
            )
            items = normalize.objects(data.get("items"))
            if items and "last_id" not in data:
                raise ReadError("invalid_response")
            next_cursor = _cursor(data.get("last_id", "")) if items else ""
            if next_cursor and next_cursor in cursors:
                raise ReadError("pagination")
            listed = {}
            for item in items:
                key = normalize.numeric_identifier(item.get("product_id"))
                if key in seen or key in listed:
                    raise ReadError("pagination")
                listed[key] = item
            details = {}
            keys = list(listed)
            for start in range(0, len(keys), self.info_chunk_size):
                chunk = keys[start : start + self.info_chunk_size]
                info = body_of(self.http.read("/v3/product/info/list", {"product_id": chunk}))
                found = {}
                for item in normalize.objects(info.get("items")):
                    key = normalize.numeric_identifier(item.get("id"))
                    if key in found or key not in chunk:
                        raise ReadError("incomplete")
                    found[key] = item
                if set(found) != set(chunk):
                    raise ReadError("incomplete")
                details.update(found)
            observed = self.clock()
            snapshots = tuple(
                normalize.offer(self.source, listed[key], details[key], observed) for key in keys
            )
            seen.update(keys)
            total = _hint(data.get("total_items", data.get("total")))
            yield ReadPage(
                self.source, "catalog", snapshots, next_cursor or None, not bool(next_cursor), total
            )
            if not next_cursor:
                return
            cursors.add(next_cursor)
            cursor = next_cursor
        raise ReadError("pagination")

    def iter_shipments(
        self,
        since: datetime,
        until: datetime,
        *,
        cursor: str = "",
        states: tuple[str, ...] | None = WORKING_STATES,
    ) -> Iterator[ReadPage[ShipmentSnapshot]]:
        since_iso, until_iso = _date(since), _date(until)
        if since > until:
            raise ValueError("The date range is reversed")
        if states is not None and (not states or any(state not in FILTER_STATES for state in states)):
            raise ValueError("Unknown fulfillment state")
        filter_ = {"since": since_iso, "to": until_iso}
        if states is not None:
            filter_["statuses"] = sorted({raw for state in states for raw in FILTER_STATES[state]})
        cursor = _cursor(cursor)
        cursors, seen = {cursor}, set()
        for _ in range(self.max_pages):
            data = body_of(
                self.http.read(
                    "/v4/posting/fbs/list",
                    {
                        "sort_dir": "asc",
                        "cursor": cursor,
                        "filter": filter_,
                        "limit": self.posting_page_size,
                        "with": {
                            "analytics_data": True,
                            "barcodes": True,
                            "financial_data": False,
                            "legal_info": False,
                        },
                    },
                )
            )
            items = normalize.objects(data.get("postings"))
            if not isinstance(data.get("has_next"), bool):
                raise ReadError("invalid_response")
            more = data["has_next"]
            next_cursor = _cursor(data.get("cursor", "")) if more else ""
            if more and (not items or not next_cursor or next_cursor in cursors):
                raise ReadError("pagination")
            observed = self.clock()
            snapshots = tuple(normalize.shipment(self.source, raw, observed) for raw in items)
            ids = [item.source_id for item in snapshots]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise ReadError("pagination")
            seen.update(ids)
            yield ReadPage(self.source, "shipments", snapshots, next_cursor or None, not more)
            if not more:
                return
            cursors.add(next_cursor)
            cursor = next_cursor
        raise ReadError("pagination")

    def get_shipment(self, posting_number: str) -> ShipmentSnapshot | None:
        posting_number = normalize.identifier(posting_number)
        try:
            data = body_of(
                self.http.read(
                    "/v3/posting/fbs/get",
                    {
                        "posting_number": posting_number,
                        "with": {
                            "analytics_data": True,
                            "barcodes": True,
                            "financial_data": False,
                            "legal_info": False,
                            "product_exemplars": False,
                            "related_postings": True,
                        },
                    },
                )
            )
        except ReadError as error:
            if error.kind == "not_found":
                return None
            raise
        result = normalize.shipment(self.source, data, self.clock())
        if result.source_id != posting_number:
            raise ReadError("invalid_response")
        return result

    def find_shipment_by_barcode(self, barcode: str) -> ShipmentSnapshot | None:
        barcode = normalize.identifier(barcode)
        try:
            data = body_of(self.http.read("/v2/posting/fbs/get-by-barcode", {"barcode": barcode}))
        except ReadError as error:
            if error.kind == "not_found":
                return None
            raise
        # This endpoint returns fewer fields. Missing scanit/requirements stay unknown.
        return normalize.shipment(self.source, data, self.clock())
