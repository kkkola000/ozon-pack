"""Yandex FBS proof uses exact codes and item identity, never GTIN alone or HTTP status."""

from collections import Counter
from copy import deepcopy

from pack_v2.integrations.public import ReadError
from pack_v2.modules.marking.public import Marking
from pack_v2.shared.errors import Conflict

from . import policy

STATUS_MESSAGES = {
    "FAILED": "Не удалось проверить код. Повторите попытку позже или удалите код маркировки.",
    "IN_PROGRESS": "В процессе проверки.",
    "NOT_ON_VALIDATION": "Код маркировки не отправлен на проверку.",
    "OK": "Проверка успешно пройдена.",
    "INVALID": "Проверка не пройдена. Продажа товара с этим кодом запрещена.",
}
SUBSTATUS_MESSAGES = {
    "WRONG_OWNER_INN": "Проверка не пройдена. ИНН владельца кода отличается от ИНН продавца.",
    "CIS_VALIDATION_ERROR": "Проверка не пройдена.",
    "CIS_GTIN_NOT_FOUND": "Код маркировки не содержит GTIN.",
    "CIS_SERIAL_NUMBER_NOT_FOUND": "Код маркировки не содержит серийный номер.",
    "INVALID_SYMBOLS_FOUND": "Код маркировки содержит недопустимые символы.",
    "CRYPTO_TAIL_FORMAT_MISMATCH_CIS_TYPE": "Формат криптоподписи не соответствует типу кода маркировки.",
    "INVALID_CRYPTO_TAIL": "Криптоподпись не валидна.",
    "INVALID_CRYPTO_KEY": "Криптоключ не валиден.",
    "VERIFICATION_FAILED_IN_EMITTER_COUNTRY": "Код маркировки не прошел верификацию в стране эмитента.",
    "UNSUPPORTED_AI_FOUND": "Найденные в коде маркировки AI не поддерживаются.",
    "CIS_NOT_FOUND_IN_GIS_MT": "Код маркировки не найден в ГИС МТ.",
    "NOT_PLACED_ON_MARKET": "Код маркировки не введен в оборот.",
    "NOT_PRINTED_ON_PACKAGE": "Код маркировки не нанесен на упаковку.",
    "EXPIRED_ITEM": "У маркированного товара истек срок годности.",
    "SALE_BLOCKED_BY_OGB": "Розничная продажа продукции заблокирована по решению ОГВ.",
    "ITEM_SOLD": "Маркированный товар был продан.",
}


def mark_of(unit):
    return next((m["mark"] for m in unit.get("marks", []) if m["mark_type"] == "mandatory_mark"), None)


def short(raw):
    try:
        value = Marking.validate(raw)
        return "01" + value.gtin + "21" + value.serial
    except (ValueError, TypeError, AttributeError):
        return raw


def matches(value, wanted):
    return bool(value and wanted) and (Marking.equivalent(value, wanted) or value == short(wanted))


def unit_key(unit):
    code = mark_of(unit) or unit.get("native_short_cis")
    return (
        short(code),
        unit.get("gtd") or None,
        unit.get("rnpt") or None,
        unit.get("uin") or None,
        unit.get("countryCode") or None,
    )


def shape(native):
    return Counter(
        tuple(
            sorted(
                (str(e["id"]), e.get("fullCount"), tuple(sorted((e.get("partialCount") or {}).items())))
                for e in b["items"]
            )
        )
        for b in native["boxes"]
    )


def box_values(boxes):
    """Keep box order as well as unit identity; a remote move requires a fresh local binding."""
    out = []
    for box in boxes:
        entries = []
        for entry in box["items"]:
            values = [
                (
                    short(v.get("cisFull") or v.get("cis")),
                    v.get("gtd"),
                    v.get("rnpt"),
                    v.get("uin"),
                    v.get("countryCode"),
                )
                for v in entry.get("instances", [])
            ]
            entries.append(
                (
                    str(entry["id"]),
                    entry.get("fullCount") or 0,
                    tuple(sorted((entry.get("partialCount") or {}).items())),
                    tuple(sorted(values, key=repr)),
                )
            )
        out.append(tuple(sorted(entries, key=repr)))
    return tuple(out)


def fresh_matches(fresh, baseline, desired, *, after_write=False):
    if shape(fresh.posting["yandex"]) not in (shape(baseline["native"]), shape({"boxes": desired["boxes"]})):
        return False
    native = fresh.posting["yandex"]
    same_boxes = native.get("box_keys") and native["box_keys"] == baseline["native"].get("box_keys")
    compare = (
        (lambda boxes: Counter(box_values(boxes)))
        if after_write or native.get("box_keys") and not same_boxes
        else box_values
    )
    if compare(native["boxes"]) not in (
        compare(baseline.get("boxes", baseline["native"]["boxes"])),
        compare(desired["boxes"]),
    ):
        return False
    old = {str(p["product_id"]): p for p in baseline["products"]}
    wanted = {str(p["product_id"]): p for p in desired["products"]}
    for p in fresh.products:
        key = str(p["product_id"])
        got = Counter(unit_key(e) for e in p["exemplars"])
        if (
            key not in old
            or key not in wanted
            or got
            not in (
                Counter(unit_key(e) for e in old[key]["exemplars"]),
                Counter(unit_key(e) for e in wanted[key]["exemplars"]),
            )
        ):
            return False
        # If full cryptographic values are readable, a different tail is a conflict too.
        known = [mark_of(e) for e in old[key]["exemplars"] + wanted[key]["exemplars"] if mark_of(e)]
        for e in p["exemplars"]:
            code = mark_of(e)
            same = [v for v in known if short(v) == short(code)] if code else []
            if same and not any(Marking.equivalent(v, code) for v in same):
                return False
    return True


def to_request(context, details, marks):
    wanted = policy.to_request(context, details, marks)
    native = context["native"]
    wanted["campaign_id"] = native["campaign_id"]
    wanted["native"] = deepcopy(native)
    products = {
        str(p["product_id"]): {str(e["exemplar_id"]): e for e in p["exemplars"]} for p in wanted["products"]
    }
    boxes = []
    for box in native["boxes"]:
        entries = []
        for entry in box["items"]:
            clean = {k: deepcopy(v) for k, v in entry.items() if k in ("id", "fullCount", "partialCount")}
            values = []
            for ordinal in entry["slots"]:
                unit = products[str(entry["id"])][str(ordinal)]
                value = {k: unit[k] for k in ("gtd", "rnpt", "uin", "countryCode") if unit.get(k)}
                code = mark_of(unit) or unit.get("native_short_cis")
                if code:
                    value["cis"] = code
                values.append(value)
            if any(values):
                if not all(values):
                    raise Conflict(
                        "Яндекс Маркет требует данные для каждой единицы товара. Проверьте полный набор экземпляров"
                    )
                clean["instances"] = values
            entries.append(clean)
        boxes.append({"items": entries})
    wanted["boxes"] = boxes
    if context.get("layout_groups") is not None:
        wanted["boxes"] = build_boxes(context["layout_groups"], details, wanted)
    return wanted


def validate_groups(groups, details):
    if not isinstance(groups, list) or not 1 <= len(groups) <= 100:
        raise Conflict("Укажите от одного до ста грузомест")
    expected = {str(d.unit_id) for d in details}
    counts = Counter()
    for group in groups:
        if (
            not isinstance(group, list)
            or not group
            or len(group) != len(set(group))
            or not set(group) <= expected
        ):
            raise Conflict(
                "Проверьте товары в грузоместах: пустые места и повторы внутри места не допускаются"
            )
        counts.update(group)
    if set(counts) != expected:
        raise Conflict("Каждый товар должен находиться в грузоместе")
    for uid, copies in counts.items():
        if copies > 1:
            if any(uid in g and len(g) != 1 for g in groups):
                raise Conflict("Часть товара должна занимать отдельное грузоместо")
            product = next(d.product_id for d in details if str(d.unit_id) == uid)
            if sum(d.product_id == product for d in details) > 1:
                raise Conflict(
                    "Несколько одинаковых многоместных товаров требуют сверки в кабинете Яндекс Маркета"
                )
    return groups


def build_boxes(groups, details, payload):
    validate_groups(groups, details)
    lookup = {str(d.unit_id): (d.product_id, d.exemplar_id) for d in details}
    products = {
        str(p["product_id"]): {str(e["exemplar_id"]): e for e in p["exemplars"]} for p in payload["products"]
    }
    totals = Counter(uid for g in groups for uid in g)
    positions = Counter()
    boxes = []
    for group in groups:
        entries = {}
        for uid in group:
            key, slot = lookup[uid]
            unit = products[key][slot]
            entry = entries.setdefault(key, {"id": int(key), "fullCount": 0, "instances": []})
            if totals[uid] > 1:
                positions[uid] += 1
                entry.pop("fullCount", None)
                entry["partialCount"] = {"current": positions[uid], "total": totals[uid]}
            else:
                entry["fullCount"] += 1
            value = {k: unit[k] for k in ("gtd", "rnpt", "uin", "countryCode") if unit.get(k)}
            code = mark_of(unit) or unit.get("native_short_cis")
            if code:
                value["cis"] = code
            entry["instances"].append(value)
        for entry in entries.values():
            if not any(entry["instances"]):
                entry.pop("instances")
            elif not all(entry["instances"]):
                raise Conflict("Заполните данные всех экземпляров товара перед сохранением грузомест")
        boxes.append({"items": list(entries.values())})
    return boxes


def layout_matches(fresh, payload):
    return fresh.posting["yandex"].get("has_layout", True) and box_values(
        fresh.posting["yandex"]["boxes"]
    ) == box_values(payload["boxes"])


def checked(fresh, payload, req, unit_ids, status):
    if not isinstance(status, dict) or not isinstance(status.get("items"), list):
        raise ReadError("invalid_response", marketplace="yandex")
    index = {}
    for item in status["items"]:
        key = str(item.get("id"))
        if key in index:
            raise ReadError("invalid_response", marketplace="yandex")
        rows = item.get("cis") or []
        if not isinstance(rows, list):
            raise ReadError("invalid_response", marketplace="yandex")
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("status") not in STATUS_MESSAGES
                or not isinstance(row.get("value"), str)
                or row.get("substatus") is not None
                and not isinstance(row["substatus"], str)
            ):
                raise ReadError("invalid_response", marketplace="yandex")
        index[key] = rows
    available = {str(p["product_id"]): p["exemplars"] for p in fresh.products}
    errors, results = {}, {}
    ready = bool(layout_matches(fresh, payload)) if "boxes" in payload else True
    for product in payload["products"]:
        key = str(product["product_id"])
        pool = list(available.get(key, []))
        checks = list(index.get(key, []))
        for wanted in product["exemplars"]:
            uid = unit_ids[key, str(wanted["exemplar_id"])]
            proof = {}
            results[uid] = proof
            code = mark_of(wanted)
            matches_native = [
                (i, e)
                for i, e in enumerate(pool)
                if (not code or matches(mark_of(e) or e.get("native_short_cis"), code))
                and (not wanted.get("gtd") or e.get("gtd") == wanted["gtd"])
            ]
            actual = matches_native[0][1] if matches_native else None
            if actual is not None:
                pool.pop(matches_native[0][0])
            if req[key]["gtd"]:
                proof["gtd"] = (
                    "accepted"
                    if actual is not None and wanted.get("gtd") and actual.get("gtd") == wanted["gtd"]
                    else "pending"
                )
                ready &= proof["gtd"] == "accepted"
            if code:
                found = [(i, c) for i, c in enumerate(checks) if matches(c["value"], code)]
                check = found[0][1] if len(found) == 1 else None
                if check:
                    checks.pop(found[0][0])
                    state = check["status"]
                    sub = check.get("substatus") if state == "INVALID" else None
                    proof.update(
                        cis_status=state,
                        cis_substatus=sub if sub in SUBSTATUS_MESSAGES else None,
                        cis_message=SUBSTATUS_MESSAGES.get(sub, STATUS_MESSAGES[state])
                        if state == "INVALID"
                        else STATUS_MESSAGES[state],
                    )
                    full_saved = (
                        Marking.equivalent(check["value"], code)
                        or actual is not None
                        and Marking.equivalent(mark_of(actual), code)
                    )
                    proof["cis"] = (
                        "accepted"
                        if state == "OK" and full_saved
                        else "rejected"
                        if state in ("INVALID", "FAILED")
                        else "pending"
                    )
                    if state in ("INVALID", "FAILED"):
                        errors.setdefault(uid, []).append("cis")
                else:
                    proof["cis"] = "pending"
                ready &= proof["cis"] == "accepted"
    return ready, errors, results


def message(results):
    values = [v for v in results.values() if v.get("cis_message")]
    for state in ("INVALID", "FAILED", "NOT_ON_VALIDATION", "IN_PROGRESS"):
        found = next((v["cis_message"] for v in values if v.get("cis_status") == state), None)
        if found:
            return found
    if any(v.get("cis") == "pending" for v in results.values()):
        return "Ожидаем подтверждения записи полного кода Честного знака Яндекс Маркетом"
    if any(v.get("gtd") == "pending" for v in results.values()):
        return "Ожидаем подтверждения ГТД Яндекс Маркета"
    return "Ожидаем подтверждения данных экземпляров Яндекс Маркета"


def align_snapshot(snapshot, planned, items, existing, marks):
    """Yandex has no exemplar IDs. Restore local slots by exact CIS identity, not response order."""
    by_product = {str(p["product_id"]): p for p in snapshot["products"]}
    for item_id, item in items.items():
        units = sorted((u for u in planned if u.item_id == item_id), key=lambda u: u.ordinal)
        key = policy.product_key(item)
        product = by_product[key]
        free = list(product["exemplars"])
        selected = {}
        mapping = {}
        # Reserve all known identities first: an unmarked unit must not steal another unit's code.
        for physical in units:
            local = marks.get(physical.unit_id)
            found = next(
                (e for e in free if local and matches(mark_of(e) or e.get("native_short_cis"), local.raw)),
                None,
            )
            if found is not None:
                selected[physical.unit_id] = found
                free.remove(found)
        reordered = []
        for physical in units:
            slot = existing.get(physical.unit_id)
            ordinal = (
                str(slot.exemplar_id)
                if slot
                else str(product["exemplars"][physical.ordinal - 1]["exemplar_id"])
            )
            chosen = selected.get(physical.unit_id)
            if chosen is None:
                chosen = free.pop(0)
            mapping[int(chosen["exemplar_id"])] = int(ordinal)
            value = deepcopy(chosen)
            value["exemplar_id"] = ordinal
            reordered.append(value)
        product["exemplars"] = sorted(reordered, key=lambda e: int(e["exemplar_id"]))
        for box in snapshot["native"]["boxes"]:
            for entry in box["items"]:
                if str(entry["id"]) == key:
                    entry["slots"] = [mapping[n] for n in entry["slots"]]
