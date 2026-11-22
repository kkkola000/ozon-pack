"""Pure requirements, full-payload construction and exact confirmation checks."""

import hashlib
import json
from collections import Counter
from copy import deepcopy

from pack_v2.integrations.public import ReadError
from pack_v2.modules.marking.public import Marking
from pack_v2.shared.errors import Conflict

POLICY_VERSION = 1
SUPPORTED = {"cis", "gtd"}
PASSED = {"passed", "success"}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def product_key(item):
    keys = [value for kind, value in item.references if kind == "sku"]
    if len(keys) != 1:
        raise Conflict("Не подтверждён SKU товара для данных экземпляров")
    return keys[0]


def requirements(manifest, snapshot):
    local = {product_key(i): i for i in manifest.items}
    if len(local) != len(manifest.items) or {
        str(p["product_id"]): p["quantity"] for p in snapshot.products
    } != {k: i.quantity for k, i in local.items()}:
        raise Conflict("Состав экземпляров Ozon не совпадает с отправлением. Обновите заказ")
    result = {}
    for product in snapshot.products:
        key = str(product["product_id"])
        item = local[key]
        required = set(product.get("required_fields", item.required_fields))
        optional = set(product.get("optional_fields", item.optional_fields))
        cis_positive = bool(
            product.get("is_mandatory_mark_needed")
            or product.get("is_mandatory_mark_possible")
            or "cis" in required | optional
            or any(m["mark_type"] == "mandatory_mark" for e in product["exemplars"] for m in e["marks"])
        )
        gtd_positive = bool(product.get("is_gtd_needed") or "gtd" in required)
        unknown = []
        if not cis_positive and any(
            product.get(f) is None for f in ("is_mandatory_mark_needed", "is_mandatory_mark_possible")
        ):
            unknown.append("cis")
        if not gtd_positive and product.get("is_gtd_needed") is None:
            unknown.append("gtd")
        other = set(required - SUPPORTED)
        for flag, field in (
            ("is_rnpt_needed", "rnpt"),
            ("is_weight_needed", "weight"),
            ("is_jw_uin_needed", "uin"),
            ("has_imei", "imei"),
        ):
            if product.get(flag):
                other.add(field)
        missing_other = []
        for field in other:
            for unit in product["exemplars"]:
                if field in ("imei", "uin"):
                    kind = "jw_uin" if field == "uin" else field
                    present = any(m["mark_type"] == kind and m["mark"] for m in unit["marks"])
                else:
                    present = bool(unit.get(field)) and not unit.get("is_" + field + "_absent")
                if not present:
                    missing_other.append(field)
        result[key] = {
            "quantity": item.quantity,
            "cis_market": "required"
            if product.get("is_mandatory_mark_needed") or "cis" in required
            else "optional"
            if product.get("is_mandatory_mark_possible") or "cis" in optional
            else "unknown"
            if "cis" in unknown
            else "not_applicable",
            "cis": cis_positive,
            "gtd": gtd_positive,
            "unknown": unknown,
            "unsupported_missing": sorted(set(missing_other)),
            "policy_version": POLICY_VERSION,
        }
    return result


def signature(products, req):
    return digest(
        [
            [(str(p["product_id"]), tuple(str(e["exemplar_id"]) for e in p["exemplars"])) for p in products],
            req,
        ]
    )


def compatible(fresh, baseline, old_requirements, new_requirements):
    old = {
        (str(p["product_id"]), tuple(str(e["exemplar_id"]) for e in p["exemplars"]))
        for p in baseline["products"]
    }
    now = {
        (str(p["product_id"]), tuple(str(e["exemplar_id"]) for e in p["exemplars"])) for p in fresh.products
    }
    if old != now or old_requirements.keys() != new_requirements.keys():
        return False
    for key, req in new_requirements.items():
        previous = old_requirements[key]
        if req["quantity"] != previous["quantity"] or req["unknown"] or req["unsupported_missing"]:
            return False
        if any(req[field] and not previous[field] for field in ("cis", "gtd")):
            return False
    return True


def to_request(context, details, marks):
    data = {"posting_number": context["posting_number"], "products": []}
    if context.get("multi_box_qty") is not None:
        data["multi_box_qty"] = context["multi_box_qty"]
    units = {(d.product_id, d.exemplar_id): d for d in details}
    for source in context["products"]:
        key = str(source["product_id"])
        product = {"product_id": int(key), "exemplars": []}
        for original in source["exemplars"]:
            unit = deepcopy(original)
            unit["exemplar_id"] = int(unit["exemplar_id"])
            detail = units[(key, str(original["exemplar_id"]))]
            value = detail.values
            if "gtd" in value:
                unit["gtd"], unit["is_gtd_absent"] = value["gtd"], False
            if detail.unit_id in marks:
                unit["marks"] = [m for m in unit.get("marks", []) if m["mark_type"] != "mandatory_mark"]
                unit["marks"].append({"mark_type": "mandatory_mark", "mark": marks[detail.unit_id].raw})
            product["exemplars"].append(unit)
        data["products"].append(product)
    return data


def validation_request(payload):
    result = {"posting_number": payload["posting_number"], "products": deepcopy(payload["products"])}
    for product in result["products"]:
        for unit in product["exemplars"]:
            for key in ("exemplar_id", "is_gtd_absent", "is_rnpt_absent"):
                unit.pop(key, None)
    return result


def _mark_key(mark):
    value = mark.get("mark")
    if mark.get("mark_type") == "mandatory_mark":
        try:
            value = Marking.validate(value).fields
        except (ValueError, TypeError, AttributeError):
            raise ReadError("invalid_response") from None
    return mark.get("mark_type"), value


def same_unit(left, right, *, check_id=False):
    if check_id and str(left.get("exemplar_id")) != str(right.get("exemplar_id")):
        return False
    if Counter(_mark_key(m) for m in left.get("marks", [])) != Counter(
        _mark_key(m) for m in right.get("marks", [])
    ):
        return False
    for field in ("gtd", "rnpt", "weight"):
        if left.get(field) != right.get(field):
            return False
        if field != "weight" and bool(left.get("is_" + field + "_absent")) != bool(
            right.get("is_" + field + "_absent")
        ):
            return False
    return True


def fresh_matches(fresh, baseline, desired):
    """Never overwrite a concurrent seller change, even in an unrelated field."""
    if fresh.multi_box_qty != baseline.get("multi_box_qty"):
        return False
    by_id = {(str(p["product_id"]), str(e["exemplar_id"])): e for p in fresh.products for e in p["exemplars"]}
    old = {
        (str(p["product_id"]), str(e["exemplar_id"])): e for p in baseline["products"] for e in p["exemplars"]
    }
    wanted = {
        (str(p["product_id"]), str(e["exemplar_id"])): e for p in desired["products"] for e in p["exemplars"]
    }
    if by_id.keys() != old.keys() or old.keys() != wanted.keys():
        return False
    return all(
        same_unit(got, old[key], check_id=True) or same_unit(got, wanted[key], check_id=True)
        for key, got in by_id.items()
    )


def checked_validation(data, payload, unit_ids):
    rows = data.get("products")
    desired = {str(p["product_id"]): p for p in payload["products"]}
    if (
        not isinstance(rows, list)
        or len(rows) != len(desired)
        or {str(p.get("product_id")) for p in rows} != desired.keys()
    ):
        raise ReadError("incomplete")
    errors = {}
    for product in rows:
        key = str(product["product_id"])
        expected = validation_request({"posting_number": "", "products": [desired[key]]})["products"][0][
            "exemplars"
        ]
        actual = product.get("exemplars")
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise ReadError("incomplete")
        remaining = list(enumerate(expected))
        for got in actual:
            matches = [(i, value) for i, value in remaining if same_unit(got, value)]
            if not matches:
                raise ReadError("invalid_response")
            index, _ = matches[0]
            remaining = [(i, e) for i, e in remaining if i != index]
            uid = unit_ids[key, str(desired[key]["exemplars"][index]["exemplar_id"])]
            if got.get("valid") is not True or got.get("errors"):
                errors[uid] = ["data"]
            for mark in got.get("marks", []):
                if mark.get("valid") is not True or mark.get("errors"):
                    errors.setdefault(uid, []).append(
                        "cis" if mark.get("mark_type") == "mandatory_mark" else "data"
                    )
        if product.get("valid") is not True or product.get("error"):
            for unit in desired[key]["exemplars"]:
                errors.setdefault(unit_ids[key, str(unit["exemplar_id"])], ["data"])
    return errors


def checked_status(data, payload, req, unit_ids):
    expected = {
        (str(p["product_id"]), str(e["exemplar_id"])): e for p in payload["products"] for e in p["exemplars"]
    }
    actual = {}
    for product in data.get("products", []):
        for unit in product.get("exemplars", []):
            key = str(product.get("product_id")), str(unit.get("exemplar_id"))
            if key in actual:
                raise ReadError("invalid_response")
            actual[key] = unit
    if actual.keys() != expected.keys():
        return False, {}, {}
    ready, errors, results = True, {}, {}
    for key, want in expected.items():
        got, fields = actual[key], req[key[0]]
        uid = unit_ids[key]
        proof = results.setdefault(uid, {})
        ready &= same_unit(got, want, check_id=True)
        for name in ("gtd", "rnpt", "weight"):
            matches = got.get(name) == want.get(name) and not got.get("is_" + name + "_absent")
            bad = bool(
                got.get(name + "_error_codes")
                or str(got.get(name + "_check_status")).lower() in ("error", "invalid")
            )
            passed = matches and not bad and str(got.get(name + "_check_status")).lower() in PASSED
            if matches and bad:
                errors.setdefault(uid, []).append(name)
            if want.get(name) and not want.get("is_" + name + "_absent"):
                ready &= passed
                if name == "gtd":
                    proof[name] = "accepted" if passed else "rejected" if matches and bad else "pending"
        for mark in want.get("marks", []):
            found = next((m for m in got.get("marks", []) if _mark_key(m) == _mark_key(mark)), {})
            bad = bool(
                found.get("error_codes") or str(found.get("check_status")).lower() in ("error", "invalid")
            )
            passed = bool(found and not bad and str(found.get("check_status")).lower() in PASSED)
            if bad:
                errors.setdefault(uid, []).append("cis" if mark["mark_type"] == "mandatory_mark" else "data")
            ready &= passed
            if mark["mark_type"] == "mandatory_mark":
                proof["cis"] = "accepted" if passed else "rejected" if bad else "pending"
        if fields["cis"] and not any(m["mark_type"] == "mandatory_mark" for m in want.get("marks", [])):
            ready = False
    available = data.get("status") in ("ship_available", "update_available", "update_not_available")
    return bool(ready and available), errors, results


def applied_field_proofs(posting, payload, unit_ids, results):
    if posting.get("status") != "awaiting_deliver":
        return results
    actual = {
        (str(p.get("sku")), str(e.get("exemplar_id"))): e
        for p in (posting.get("product_exemplars") or {}).get("products", [])
        for e in p.get("exemplars", [])
    }
    out = deepcopy(results)
    for product in payload["products"]:
        for wanted in product["exemplars"]:
            key = str(product["product_id"]), str(wanted["exemplar_id"])
            uid = unit_ids[key]
            got = actual.get(key, {})
            for field, status in list(out.get(uid, {}).items()):
                if status != "accepted":
                    continue
                if field == "cis":
                    code = next(
                        (m["mark"] for m in wanted.get("marks", []) if m["mark_type"] == "mandatory_mark"),
                        None,
                    )
                    matches = Marking.equivalent(got.get("mandatory_mark"), code)
                else:
                    matches = got.get(field) == wanted.get(field) and not got.get("is_" + field + "_absent")
                if not matches:
                    out[uid][field] = "pending"
    return out


def applied(posting, payload):
    if posting.get("status") != "awaiting_deliver" or posting.get("substatus") == "ship_failed":
        return False
    expected = {
        (str(p["product_id"]), str(e["exemplar_id"])): e for p in payload["products"] for e in p["exemplars"]
    }
    actual = {
        (str(p.get("sku")), str(e.get("exemplar_id"))): e
        for p in (posting.get("product_exemplars") or {}).get("products", [])
        for e in p.get("exemplars", [])
    }
    if actual.keys() != expected.keys():
        return False
    for key, want in expected.items():
        got = dict(actual[key])
        mandatory = got.pop("mandatory_mark", None)
        got["marks"] = [{"mark_type": "mandatory_mark", "mark": mandatory}] if mandatory else []
        # UIN/IMEI must be explicitly readable to verify preservation; never guess them.
        if not same_unit(got, want, check_id=True):
            return False
    return True
