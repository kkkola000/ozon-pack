"""FBS boxes write client. Native identifiers are scoped through the business order API."""

from copy import deepcopy

from pack_v2.integrations.instances import InstanceSnapshot
from pack_v2.shared.errors import DomainError
from pack_v2.shared.gs1 import parse

from .reader import YandexReadAdapter, count
from .transport import body, error, positive

IDENTIFIERS = {"cis", "cisFull", "gtd", "rnpt", "uin", "countryCode"}


def full_code(value):
    try:
        return parse(value).raw if isinstance(value, str) else None
    except ValueError:
        return None


def identity(value):
    full = full_code(value)
    if full:
        code = parse(full)
        return "01" + code.gtin + "21" + code.serial
    return value


def instance(raw):
    if not isinstance(raw, dict):
        raise error("invalid_response")
    clean = {}
    for key in IDENTIFIERS:
        value = raw.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or len(value) > (512 if key.startswith("cis") else 128):
            raise error("invalid_response")
        if any(ord(c) < 32 and not (key.startswith("cis") and c == "\x1d") for c in value):
            raise error("invalid_response")
        clean[key] = value
    return clean


def merge_parts(rows):
    """Every part belongs to the same single physical unit; never discard another part's data."""
    merged = {}
    for row in rows:
        old = merged.get("cisFull") or merged.get("cis")
        new = row.get("cisFull") or row.get("cis")
        if (
            old
            and new
            and (
                identity(old) != identity(new)
                or full_code(old)
                and full_code(new)
                and parse(old).fields != parse(new).fields
            )
        ):
            raise DomainError(
                "У частей одного товара разные коды Честного знака. Сверьте грузоместа в Яндекс Маркете"
            )
        for field in ("gtd", "rnpt", "uin", "countryCode"):
            if merged.get(field) and row.get(field) and merged[field] != row[field]:
                raise DomainError(
                    "У частей одного товара разные данные экземпляра. Сверьте грузоместа в Яндекс Маркете"
                )
            if row.get(field):
                merged[field] = row[field]
        if new and (not old or full_code(new)):
            merged["cisFull" if full_code(new) else "cis"] = new
    return merged


def bind_box_slots(boxes, key, exemplars):
    """The API may enumerate item instances and boxes in different orders."""
    positions = []
    for box in boxes:
        for entry in box["items"]:
            if str(entry["id"]) != key:
                continue
            if entry.get("partialCount"):
                entry["slots"] = [int(exemplars[0]["exemplar_id"])]
                continue
            for offset, ordinal in enumerate(entry["slots"]):
                original = int(exemplars[ordinal - 1]["exemplar_id"])
                value = entry["instances"][offset] if offset < len(entry["instances"]) else {}
                cis = value.get("cisFull") or value.get("cis")
                candidates = []
                for unit in exemplars:
                    code = next(
                        (m["mark"] for m in unit["marks"] if m["mark_type"] == "mandatory_mark"), None
                    ) or unit.get("native_short_cis")
                    if cis and identity(cis) != identity(code):
                        continue
                    if any(
                        value.get(f) and value[f] != unit.get(f)
                        for f in ("gtd", "rnpt", "uin", "countryCode")
                    ):
                        continue
                    candidates.append(int(unit["exemplar_id"]))
                if not candidates:
                    raise DomainError(
                        "Экземпляр в грузоместе не соответствует товару. Сверьте заказ в Яндекс Маркете"
                    )
                positions.append((entry, offset, original, candidates))
    occupied = set()
    resolved = {}
    for index, (_, _, _, candidates) in enumerate(positions):
        if len(candidates) == 1:
            slot = candidates[0]
            if slot in occupied:
                raise DomainError(
                    "Один экземпляр указан в нескольких целых товарах. Сверьте грузоместа в Яндекс Маркете"
                )
            occupied.add(slot)
            resolved[index] = slot
    for index, (entry, offset, original, candidates) in enumerate(positions):
        if index not in resolved:
            free = [v for v in candidates if v not in occupied]
            if not free:
                raise error("incomplete")
            resolved[index] = original if original in free else free[0]
            occupied.add(resolved[index])
        entry["slots"][offset] = resolved[index]


def layout(raw, items):
    incoming = (raw.get("delivery") or {}).get("boxesLayout") or []
    if not incoming:
        incoming = [{"items": [{"id": int(key), "fullCount": item["count"]} for key, item in items.items()]}]
    if not isinstance(incoming, list) or len(incoming) > 1000:
        raise error("invalid_response")
    positions = {key: 0 for key in items}
    parts = {}
    out = []
    for box in incoming:
        values = box.get("items")
        if not isinstance(values, list) or not values or len(values) > 1000:
            raise error("incomplete")
        entries = []
        for entry in values:
            key = positive(entry.get("id"))
            if key not in items:
                raise error("invalid_response")
            value = {"id": int(key)}
            part = entry.get("partialCount")
            if part is not None:
                if entry.get("fullCount") is not None or not isinstance(part, dict):
                    raise error("invalid_response")
                current, total = count(part.get("current")), count(part.get("total"))
                if current > total:
                    raise error("invalid_response")
                if items[key]["count"] != 1:
                    raise DomainError(
                        "Для нескольких одинаковых многоместных товаров нужна сверка раскладки в Яндекс Маркете"
                    )
                seen, total_known = parts.setdefault(key, (set(), total))
                if current in seen or total != total_known:
                    raise error("incomplete")
                seen.add(current)
                value["partialCount"] = {"current": current, "total": total}
                value["slots"] = [1]
            else:
                quantity = count(entry.get("fullCount"))
                start = positions[key]
                positions[key] += quantity
                if positions[key] > items[key]["count"]:
                    raise error("incomplete")
                value["fullCount"] = quantity
                value["slots"] = list(range(start + 1, start + quantity + 1))
            values = entry.get("instances") or []
            if not isinstance(values, list) or len(values) > len(value["slots"]):
                raise error("incomplete")
            value["instances"] = [instance(row) for row in values]
            entries.append(value)
        out.append({"items": entries})
    for key, item in items.items():
        if key in parts:
            seen, total = parts[key]
            if positions[key] or seen != set(range(1, total + 1)):
                raise error("incomplete")
        elif positions[key] != item["count"]:
            raise error("incomplete")
    return out


def normalized(raw, source):
    # Reader validation covers identity, count, requirements, state and avoids personal information.
    products = []
    items = {}
    for item in raw["items"]:
        key = positive(item["id"])
        rows = item.get("instances") or []
        if not isinstance(rows, list) or len(rows) > item["count"]:
            raise error("incomplete")
        items[key] = {
            "id": key,
            "count": item["count"],
            "offer_id": item["offerId"],
            "instances": [instance(row) for row in rows],
        }
    boxes = layout(raw, items)
    for item in raw["items"]:
        key = positive(item["id"])
        current = items[key]
        layout_instances = []
        partial_instances = []
        for box in boxes:
            for entry in box["items"]:
                if str(entry["id"]) == key:
                    if entry.get("partialCount"):
                        partial_instances.extend(entry["instances"])
                    else:
                        layout_instances.extend(entry["instances"])
        if partial_instances:
            layout_instances = [merge_parts(partial_instances)]
        rows = deepcopy(current["instances"] or layout_instances)
        if item["count"] == 1 and layout_instances:
            rows = [merge_parts(rows + layout_instances)]
        if len(rows) > item["count"]:
            raise error("incomplete")
        remaining = list(layout_instances)
        for position, row in enumerate(rows):
            cis = row.get("cisFull") or row.get("cis")
            found = next(
                (v for v in remaining if cis and identity(v.get("cisFull") or v.get("cis")) == identity(cis)),
                None,
            )
            if found is None and not cis and position < len(layout_instances):
                candidate = layout_instances[position]
                if candidate in remaining and not (candidate.get("cisFull") or candidate.get("cis")):
                    found = candidate
            if found is not None:
                remaining.remove(found)
                for field in ("gtd", "rnpt", "uin", "countryCode"):
                    if row.get(field) and found.get(field) and row[field] != found[field]:
                        raise DomainError(
                            "Данные экземпляров и грузомест Яндекс Маркета различаются. Сверьте заказ в кабинете"
                        )
                    if found.get(field):
                        row[field] = found[field]
                full = full_code(found.get("cisFull") or found.get("cis"))
                if full:
                    if full_code(cis) and parse(full).fields != parse(cis).fields:
                        raise DomainError(
                            "Честный знак в экземпляре и грузоместе различается. Сверьте заказ в кабинете"
                        )
                    row["cisFull"] = full
        if remaining and rows and any(remaining):
            raise DomainError("Не удалось однозначно сопоставить экземпляры с грузоместами Яндекс Маркета")
        rows.extend({} for _ in range(item["count"] - len(rows)))
        exemplars = []
        for index, value in enumerate(rows, 1):
            full = full_code(value.get("cisFull") or value.get("cis"))
            unit = {
                # Yandex has no native exemplar ID. Keep internal slots unique across
                # all order items too; the existing DB constraint protects physical units.
                "exemplar_id": str(int(key) * 1001 + index),
                "marks": [{"mark_type": "mandatory_mark", "mark": full}] if full else [],
            }
            if not full and value.get("cis"):
                unit["native_short_cis"] = value["cis"]
            for field in ("gtd", "rnpt", "uin", "countryCode"):
                if value.get(field):
                    unit[field] = value[field]
            exemplars.append(unit)
        bind_box_slots(boxes, key, exemplars)
        needed = item.get("requiredInstanceTypes") or []
        required = [
            {"CIS": "cis", "GTD": "gtd", "RNPT": "rnpt", "UIN": "uin"}[field]
            for field in needed
            if field != "CIS_OPTIONAL"
        ]
        products.append(
            {
                "product_id": key,
                "quantity": item["count"],
                "is_mandatory_mark_needed": "CIS" in needed,
                "is_mandatory_mark_possible": bool(set(needed) & {"CIS", "CIS_OPTIONAL"})
                or any(e["marks"] or e.get("native_short_cis") for e in exemplars),
                "is_gtd_needed": "GTD" in needed,
                "is_rnpt_needed": "RNPT" in needed,
                "is_weight_needed": False,
                "has_imei": False,
                "required_fields": required,
                "optional_fields": ["cis"] if "CIS_OPTIONAL" in needed else [],
                "exemplars": exemplars,
            }
        )
    native = {
        "order_id": positive(raw["orderId"]),
        "campaign_id": positive(raw["campaignId"]),
        "status": raw["status"],
        "substatus": raw["substatus"],
        "items": list(items.values()),
        "boxes": boxes,
    }
    posting = {
        "posting_number": native["order_id"],
        "status": "awaiting_packaging"
        if raw["status"] == "PROCESSING" and raw["substatus"] == "STARTED"
        else "awaiting_deliver"
        if raw["status"] == "PROCESSING" and raw["substatus"] == "READY_TO_SHIP"
        else "unavailable",
        "substatus": raw["substatus"],
        "products": [{"sku": p["product_id"], "quantity": p["quantity"]} for p in products],
        "yandex": native,
    }
    return InstanceSnapshot(source, native["order_id"], tuple(products), len(boxes), posting)


class YandexInstanceAdapter(YandexReadAdapter):
    def __init__(self, source, credentials, *, transport=None):
        super().__init__(source, credentials, transport=transport)
        self.http.instances = True
        self.http.attempts = 1
        self.campaigns = {}

    def discover(self, posting_number):
        raw = self.raw_order(posting_number)
        if raw is None:
            raise error("not_found", status=404)
        self.shipment(raw)
        self.campaigns[posting_number] = positive(raw["campaignId"])
        return normalized(raw, self.source)

    def status(self, posting_number):
        if posting_number not in self.campaigns:
            self.discover(posting_number)
        path = f"/v2/campaigns/{self.campaigns[posting_number]}/orders/{positive(posting_number)}/identifiers/status"
        return body(self.http.once("POST", path))

    def submit(self, payload):
        number = positive(payload["posting_number"])
        campaign = positive(payload["campaign_id"])
        if self.campaigns.get(number) != campaign:
            raise error("invalid_request")
        path = f"/v2/campaigns/{campaign}/orders/{number}/boxes"
        result = body(
            self.http.once("PUT", path, {"boxes": payload["boxes"], "allowRemove": False}, write=True)
        )
        if not isinstance(result.get("boxes"), list):
            # A malformed successful response is ambiguous; never retry the mutation blindly.
            from pack_v2.integrations.instances import OutcomeUnknown

            raise OutcomeUnknown("yandex")

    def posting(self, posting_number):
        return self.discover(posting_number).posting
