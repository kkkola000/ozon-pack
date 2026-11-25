"""Official business API snapshots; buyer/address/payment data is not retained."""

import hashlib
import json
from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from pack_v2.integrations.reading import (
    WORKING_STATES,
    Barcode,
    LineSnapshot,
    OfferSnapshot,
    ReadPage,
    ShipmentSnapshot,
)
from pack_v2.integrations.reading import (
    ExternalReference as Ref,
)

from .transport import YandexTransport, body, error, positive

MSK = ZoneInfo("Europe/Moscow")
STATUSES = {
    "DELIVERY": "in_delivery",
    "PICKUP": "in_delivery",
    "DELIVERED": "delivered",
    "CANCELLED": "cancelled",
}
INSTANCE_TYPES = {"CIS": "cis", "CIS_OPTIONAL": "cis", "GTD": "gtd", "RNPT": "rnpt", "UIN": "uin"}


def text(value, limit=256):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or not value.isprintable():
        raise error("invalid_response")
    return value


def count(value):
    if type(value) is not int or not 1 <= value <= 1000:
        raise error("invalid_response")
    return value


def layout_digest(raw):
    boxes = (raw.get("delivery") or {}).get("boxesLayout") or []
    clean = [
        {
            "id": b.get("boxId"),
            "barcode": b.get("barcode"),
            "items": [
                {k: e[k] for k in ("id", "fullCount", "partialCount") if k in e} for e in b.get("items", [])
            ],
        }
        for b in boxes
    ]
    for box in clean:
        box["items"].sort(key=lambda value: json.dumps(value, sort_keys=True))
    clean.sort(key=lambda value: json.dumps(value, sort_keys=True))
    shipment_id = ((raw.get("delivery") or {}).get("shipment") or {}).get("id")
    value = {"shipment_id": str(shipment_id) if shipment_id is not None else None, "boxes": clean}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def order_state(raw):
    status, substatus = raw.get("status"), raw.get("substatus")
    if status == "PROCESSING":
        return {"STARTED": "awaiting_assembly", "READY_TO_SHIP": "awaiting_dispatch"}.get(
            substatus, "unknown"
        )
    return STATUSES.get(status, "unknown")


class YandexReadAdapter:
    def __init__(self, source, credentials, *, transport=None):
        if source.marketplace != "yandex":
            raise ValueError("Wrong marketplace")
        self.source = source
        self.http = YandexTransport(credentials, transport=transport)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def pages(self, path, payload, *, limit, cursor="", envelope=True):
        seen = set()
        for _ in range(10000):
            params = {"limit": limit}
            if cursor:
                params["pageToken"] = cursor
            raw = self.http.read("POST", path, payload, params)
            data = body(raw) if envelope else raw
            paging = data.get("paging") or {}
            if not isinstance(paging, dict):
                raise error("pagination")
            following = paging.get("nextPageToken") or None
            if following is not None:
                text(following, 4096)
                if following == cursor or following in seen:
                    raise error("pagination")
                seen.add(following)
            yield data, following
            if not following:
                return
            cursor = following
        raise error("pagination")

    def offer(self, raw, *, archived_default=False):
        if not isinstance(raw, dict) or not isinstance(raw.get("offer"), dict):
            raise error("invalid_response")
        offer = raw["offer"]
        number = text(offer.get("offerId"))
        mapping = raw.get("mapping") or {}
        refs = [Ref("product_id", number, True), Ref("offer_id", number)]
        if mapping.get("marketSku") is not None:
            refs.append(Ref("sku", positive(mapping["marketSku"])))
        values = offer.get("barcodes") or []
        if not isinstance(values, list) or len(values) > 100:
            raise error("invalid_response")
        codes = []
        for value in sorted(set(text(v, 512) for v in values)):
            gtin = value.isascii() and value.isdigit() and len(value) in (8, 12, 13, 14)
            codes.append(
                Barcode(
                    value,
                    "gtin" if gtin else "marketplace" if value.upper().startswith("MRKT") else "other",
                    "yandex",
                )
            )
        pictures = offer.get("pictures") or []
        if not isinstance(pictures, list):
            raise error("invalid_response")
        image = next(
            (p for p in pictures if isinstance(p, str) and p.startswith("https://") and len(p) <= 4096), None
        )
        declared = offer.get("markingDeclared")
        if declared is not None and type(declared) is not bool:
            raise error("invalid_response")
        archived = offer.get("archived", archived_default)
        if type(archived) is not bool:
            raise error("invalid_response")
        fields = {"article", "archived"}
        if "name" in offer:
            fields.add("name")
        if "barcodes" in offer:
            fields.add("barcodes")
        if "pictures" in offer:
            fields.add("image_url")
        if declared is not None:
            fields.add("marking_declared")
        return OfferSnapshot(
            self.source,
            number,
            tuple(refs),
            text(offer["name"], 500) if offer.get("name") else None,
            number,
            tuple(codes),
            image,
            archived,
            frozenset(fields),
            datetime.now(UTC),
            declared,
        )

    def iter_catalog(self, *, cursor=""):
        seen = set()
        for archived in (False, True):
            for data, next_token in self.pages(
                self.http.catalog_path, {"archived": archived}, limit=100, cursor=cursor
            ):
                values = data.get("offerMappings")
                if not isinstance(values, list):
                    raise error("incomplete")
                snapshots = tuple(self.offer(v, archived_default=archived) for v in values)
                ids = [s.source_id for s in snapshots]
                if any(v in seen for v in ids) or len(ids) != len(set(ids)):
                    raise error("pagination")
                seen.update(ids)
                yield ReadPage(self.source, "catalog", snapshots, next_token, archived and next_token is None)
            cursor = ""

    def shipment(self, raw):
        if not isinstance(raw, dict):
            raise error("invalid_response")
        number = positive(raw.get("orderId"))
        campaign = positive(raw.get("campaignId"))
        if raw.get("programType", "FBS") != "FBS":
            raise error("invalid_response")
        status = text(raw.get("status"), 64)
        substatus = text(raw.get("substatus"), 64)
        values = raw.get("items")
        if not isinstance(values, list) or not values or len(values) > 1000:
            raise error("incomplete")
        lines = []
        seen = set()
        for item in values:
            key = positive(item.get("id"))
            offer = text(item.get("offerId"))
            if key in seen:
                raise error("invalid_response")
            seen.add(key)
            required = item.get("requiredInstanceTypes") or []
            if not isinstance(required, list) or any(r not in INSTANCE_TYPES for r in required):
                raise error("invalid_response")
            prices = item.get("prices") or {}
            amount, currency = None, None
            components = [prices.get(k) for k in ("payment", "cashback") if prices.get(k) is not None]
            if components:
                if any(
                    not isinstance(p, dict) or "value" not in p or "currencyId" not in p for p in components
                ):
                    raise error("invalid_response")
                currencies = {p["currencyId"] for p in components}
                if len(currencies) != 1:
                    raise error("invalid_response")
                currency = text(currencies.pop(), 8)
                if currency == "RUR":
                    currency = "RUB"
                try:
                    amounts = [Decimal(str(p["value"])) for p in components]
                    if any(not n.is_finite() or n < 0 for n in amounts):
                        raise ValueError("Invalid price")
                    amount = (sum(amounts) / count(item.get("count"))).quantize(Decimal("0.0001"))
                except (ValueError, ArithmeticError):
                    raise error("invalid_response") from None
            req = tuple(sorted({INSTANCE_TYPES[r] for r in required if r != "CIS_OPTIONAL"}))
            lines.append(
                LineSnapshot(
                    key,
                    (Ref("product_id", offer), Ref("offer_id", offer), Ref("item_id", key)),
                    text(item.get("offerName"), 500),
                    count(item.get("count")),
                    amount,
                    currency,
                    req,
                    ("cis",) if "CIS_OPTIONAL" in required else (),
                    frozenset({"name", "price", "currency", "article", "requirements"}),
                )
            )
        delivery = raw.get("delivery") or {}
        if not isinstance(delivery, dict):
            raise error("invalid_response")
        refs = [Ref("posting_number", number, True), Ref("campaign_id", campaign)]
        boxes = delivery.get("boxesLayout") or []
        if not isinstance(boxes, list) or len(boxes) > 1000:
            raise error("invalid_response")
        refs.append(Ref("layout_digest", layout_digest(raw), origin="boxesLayout"))
        for box in boxes:
            if box.get("barcode"):
                refs.append(Ref("label_barcode", text(box["barcode"], 512), origin="boxesLayout"))
        if raw.get("externalOrderId"):
            refs.append(Ref("external_order_id", text(raw["externalOrderId"]), origin="externalOrderId"))
        deadline = None
        shipment = delivery.get("shipment") or {}
        if shipment.get("shipmentDate"):
            try:
                deadline = datetime.combine(
                    date.fromisoformat(shipment["shipmentDate"]),
                    time.fromisoformat(shipment.get("shipmentTime") or "23:59:59"),
                    MSK,
                ).astimezone(UTC)
            except (ValueError, TypeError):
                raise error("invalid_response") from None
        warehouse = delivery.get("warehouseId")
        warehouse = text(str(warehouse)) if warehouse is not None else None
        return ShipmentSnapshot(
            self.source,
            number,
            (Ref("order_id", number, True),),
            tuple(refs),
            status,
            substatus,
            order_state(raw),
            False,
            tuple(lines),
            deadline,
            None,
            warehouse,
            warehouse,
            None,
            (),
            len(boxes) or None,
            frozenset(
                {
                    "substatus",
                    "deadline",
                    "warehouse_id",
                    "warehouse_name",
                    "package_count_hint",
                    "parent",
                    "related",
                    "reference:boxesLayout",
                    "reference:externalOrderId",
                }
            ),
            True,
            datetime.now(UTC),
        )

    def raw_order(self, number):
        number = positive(number)
        data = self.http.read(
            "POST", self.http.orders_path, {"orderIds": [int(number)], "programTypes": ["FBS"]}, {"limit": 50}
        )
        rows = data.get("orders")
        if not isinstance(rows, list) or len(rows) > 1:
            raise error("invalid_response")
        if not rows:
            return None
        if positive(rows[0].get("orderId")) != number:
            raise error("invalid_response")
        if rows[0].get("programType", "FBS") != "FBS":
            return None
        return rows[0]

    def get_shipment(self, posting_number):
        raw = self.raw_order(posting_number)
        return self.shipment(raw) if raw else None

    def find_shipment_by_barcode(self, barcode):
        return (
            self.get_shipment(barcode)
            if barcode.isascii() and barcode.isdigit() and int(barcode) > 0
            else None
        )

    def iter_shipments(self, since, until, *, cursor="", states=WORKING_STATES):
        request = {
            "programTypes": ["FBS"],
            "dates": {"updateDateFrom": since.isoformat(), "updateDateTo": until.isoformat()},
        }
        if states is not None:
            request["statuses"] = sorted(
                set(["PROCESSING"] + ["DELIVERY", "PICKUP"] if "in_delivery" in states else ["PROCESSING"])
            )
        seen = set()
        for data, next_token in self.pages(
            self.http.orders_path, request, limit=50, cursor=cursor, envelope=False
        ):
            rows = data.get("orders")
            if not isinstance(rows, list) or len(rows) > 50:
                raise error("incomplete")
            snapshots = tuple(self.shipment(row) for row in rows)
            ids = [r.source_id for r in snapshots]
            if any(v in seen for v in ids) or len(ids) != len(set(ids)):
                raise error("pagination")
            seen.update(ids)
            yield ReadPage(self.source, "shipments", snapshots, next_token, next_token is None)
