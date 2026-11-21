from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from pack_v2.integrations.reading import (
    AccountSource,
    Barcode,
    ExternalReference,
    LineSnapshot,
    OfferSnapshot,
    ReadError,
    ShipmentSnapshot,
)

STATE_MAP = {
    "awaiting_packaging": "awaiting_assembly",
    "awaiting_deliver": "awaiting_dispatch",
    "acceptance_in_progress": "in_delivery",
    "delivering": "in_delivery",
    "driver_pickup": "in_delivery",
    "delivered": "delivered",
    "cancelled": "cancelled",
}
REQUIREMENTS = {
    "mandatory_mark": "cis",
    "gtd": "gtd",
    "rnpt": "rnpt",
    "weight": "weight",
    "imei": "imei",
    "jw_uin": "uin",
    "country": "country",
}


def identifier(value, *, optional: bool = False, maximum: int = 256) -> str | None:
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ReadError("invalid_response")
    value = str(value)
    if (
        not value
        or len(value) > maximum
        or not value.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ReadError("invalid_response")
    return value


def positive_integer(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ReadError("invalid_response")
    if isinstance(value, str) and (not value.isascii() or not value.isdigit() or len(value) > 10):
        raise ReadError("invalid_response")
    number = int(value)
    if not 1 <= number <= 2_147_483_647:
        raise ReadError("invalid_response")
    return number


def numeric_identifier(value) -> str:
    result = identifier(value)
    if not result.isascii() or not result.isdigit() or not 0 < int(result) <= 9_223_372_036_854_775_807:
        raise ReadError("invalid_response")
    return result


def text(value, *, maximum: int = 500) -> str | None:
    if value in (None, ""):
        return None
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(c) < 32 and c not in "\r\n\t" for c in value)
    ):
        raise ReadError("invalid_response")
    return " ".join(value.split()) or None


def objects(value) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ReadError("invalid_response")
    return value


def mapping(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ReadError("invalid_response")
    return value


def moment(value) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ReadError("invalid_response")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except ValueError:
        raise ReadError("invalid_response") from None


def references(values) -> tuple[ExternalReference, ...]:
    return tuple(sorted(set(values), key=lambda r: (r.kind, r.value, r.authoritative)))


def _archived(item: dict) -> bool | None:
    for key in ("is_archived", "archived"):
        if key not in item or item[key] is None:
            continue
        if isinstance(item[key], bool):
            return item[key]
        if item[key] in ("true", "false"):
            return item[key] == "true"
        raise ReadError("invalid_response")
    return None


def _image(item: dict) -> str | None:
    for key in ("primary_image", "images"):
        value = item.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            if any(not isinstance(v, str) for v in value):
                raise ReadError("invalid_response")
            value = next((v for v in value if v), None)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 4096:
            raise ReadError("invalid_response")
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise ReadError("invalid_response") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ReadError("invalid_response")
        return value
    return None


def offer(source: AccountSource, listed: dict, detail: dict, observed: datetime) -> OfferSnapshot:
    product_id = numeric_identifier(listed.get("product_id"))
    detail_id = numeric_identifier(detail.get("id"))
    if detail_id != product_id:
        raise ReadError("incomplete")
    refs = [ExternalReference("product_id", product_id, True)]
    for row in (listed, detail):
        for key in ("offer_id", "sku"):
            if row.get(key) not in (None, "", 0):
                refs.append(ExternalReference(key, identifier(row[key]), True))
    for row in objects(detail["sources"] if detail.get("sources") is not None else []):
        if row.get("sku") not in (None, "", 0):
            refs.append(ExternalReference("sku", identifier(row["sku"]), True))
    codes = []
    raw_codes = detail.get("barcodes")
    if raw_codes is not None:
        if not isinstance(raw_codes, list):
            raise ReadError("invalid_response")
        for value in raw_codes:
            if not isinstance(value, str):
                raise ReadError("invalid_response")
            value = identifier(value, maximum=512)
            kind = (
                "marketplace"
                if value.upper().startswith("OZN")
                else "gtin"
                if value.isascii() and value.isdigit() and len(value) in {8, 12, 13, 14}
                else "other"
            )
            codes.append(Barcode(value, kind, "ozon"))
    archived = _archived(detail)
    if archived is None:
        archived = _archived(listed)
    fields = {
        name
        for name, raw in (("name", "name"), ("article", "offer_id"), ("barcodes", "barcodes"))
        if raw in detail and detail[raw] is not None
    }
    if "primary_image" in detail or "images" in detail:
        fields.add("image_url")
    if archived is not None:
        fields.add("archived")
    article = detail.get("offer_id")
    if article in (None, ""):
        article = listed.get("offer_id")
    if article not in (None, ""):
        fields.add("article")
    return OfferSnapshot(
        source,
        product_id,
        references(refs),
        text(detail.get("name")),
        identifier(article, optional=True),
        tuple(sorted(set(codes), key=lambda c: c.value)),
        _image(detail),
        archived,
        frozenset(fields),
        observed,
    )


def _money(item: dict) -> tuple[Decimal | None, str | None]:
    price, currency = item.get("price"), item.get("currency_code")
    if isinstance(price, dict):
        currency = price.get("currency", currency)
        price = price.get("amount")
    if price in (None, ""):
        number = None
    else:
        if isinstance(price, bool) or not isinstance(price, (str, int, Decimal)) or len(str(price)) > 128:
            raise ReadError("invalid_response")
        try:
            number = Decimal(price)
            if not number.is_finite() or number < 0:
                raise InvalidOperation
        except InvalidOperation:
            raise ReadError("invalid_response") from None
    currency = text(currency, maximum=3)
    if currency and (not currency.isascii() or not currency.isalpha() or len(currency) != 3):
        raise ReadError("invalid_response")
    return number, currency.upper() if currency else None


def shipment(source: AccountSource, raw: dict, observed: datetime) -> ShipmentSnapshot:
    number, status = identifier(raw.get("posting_number")), identifier(raw.get("status"), maximum=100)
    substatus = identifier(raw.get("substatus"), optional=True, maximum=100)
    refs = [ExternalReference("posting_number", number, True)]
    tracking = identifier(raw.get("tracking_number"), optional=True)
    if tracking:
        refs.append(ExternalReference("tracking_number", tracking))
    labels = mapping(raw.get("barcodes"))
    for value in (labels.get("upper_barcode"), labels.get("lower_barcode"), raw.get("scanit")):
        code = identifier(value, optional=True)
        if code:
            refs.append(ExternalReference("label_barcode", code))
    order_refs = [
        ExternalReference(key, identifier(raw[key]), True)
        for key in ("order_id", "order_number")
        if raw.get(key) not in (None, "", 0)
    ]
    required, optional = mapping(raw.get("requirements")), mapping(raw.get("optional"))
    hints = {}
    for suffix, field_name in REQUIREMENTS.items():
        key = "products_requiring_" + suffix
        if key in required:
            if not isinstance(required[key], list):
                raise ReadError("invalid_response")
            hints[field_name] = {identifier(v) for v in required[key]}
    possible = optional.get("products_with_possible_mandatory_mark", [])
    if not isinstance(possible, list):
        raise ReadError("invalid_response")
    possible = {identifier(v) for v in possible}
    lines, seen = [], set()
    for item in objects(raw.get("products")):
        sku = numeric_identifier(item.get("sku"))
        if sku in seen:
            raise ReadError("invalid_response")
        seen.add(sku)
        product_refs = [ExternalReference("sku", sku, True)]
        article = identifier(item.get("offer_id"), optional=True)
        if article:
            product_refs.append(ExternalReference("offer_id", article, True))
        price, currency = _money(item)
        lines.append(
            LineSnapshot(
                "sku:" + sku,
                references(product_refs),
                text(item.get("name")),
                positive_integer(item.get("quantity")),
                price,
                currency,
                tuple(sorted(k for k, values in hints.items() if sku in values)),
                ("cis",) if sku in possible else (),
            )
        )
    if not lines:
        raise ReadError("invalid_response")
    delivery, analytics = mapping(raw.get("delivery_method")), mapping(raw.get("analytics_data"))
    warehouse_id = identifier(delivery.get("warehouse_id") or analytics.get("warehouse_id"), optional=True)
    warehouse_name = text(delivery.get("warehouse"))
    if not warehouse_name or warehouse_name.isdigit():
        warehouse_name = text(analytics.get("warehouse")) or warehouse_name
    parent = identifier(raw.get("parent_posting_number"), optional=True)
    related = mapping(raw.get("related_postings")).get("related_posting_numbers", [])
    if not isinstance(related, list):
        raise ReadError("invalid_response")
    count = raw.get("multi_box_qty")
    if isinstance(count, bool):
        raise ReadError("invalid_response")
    count = positive_integer(count) if count not in (None, 0, "0") else None
    fields = {
        name
        for name, key in (
            ("deadline", "shipment_date"),
            ("processing_started_at", "in_process_at"),
            ("tracking_number", "tracking_number"),
            ("requirements", "requirements"),
            ("optional_requirements", "optional"),
        )
        if key in raw and raw[key] is not None
    }
    if order_refs:
        fields.add("order_identity")
    if raw.get("barcodes") is not None or raw.get("scanit") not in (None, ""):
        fields.add("label_codes")
    failed = substatus == "ship_failed"
    state = STATE_MAP.get(status, "unknown")
    if failed and state == "awaiting_dispatch":
        state = "unknown"
    return ShipmentSnapshot(
        source,
        number,
        references(order_refs),
        references(refs),
        status,
        substatus,
        state,
        failed,
        tuple(sorted(lines, key=lambda line: line.source_line_id)),
        moment(raw.get("shipment_date")),
        moment(raw.get("in_process_at")),
        warehouse_id,
        warehouse_name,
        ExternalReference("posting_number", parent, True) if parent else None,
        references(ExternalReference("posting_number", identifier(value), True) for value in related),
        count,
        frozenset(fields),
        False,
        observed,
    )
