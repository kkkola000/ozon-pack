"""Separate, explicitly limited client for Ozon exemplar operations.

Writes are single attempts. Their durable dispatch and reconciliation belong to
instance_data; the catalog/order read transport remains unable to invoke them.
"""

from decimal import Decimal

import httpx

from pack_v2.integrations.instances import InstanceSnapshot, OutcomeUnknown
from pack_v2.integrations.reading import ReadError

from .transport import OzonTransport, body_of

ROOT = "/fbs/posting/product/exemplar/"
GET = "/v6" + ROOT + "create-or-get"
VALIDATE = "/v5" + ROOT + "validate"
SET = "/v6" + ROOT + "set"
STATUS = "/v5" + ROOT + "status"
UPDATE = "/v1" + ROOT + "update"
POSTING = "/v3/posting/fbs/get"


def positive_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ReadError("invalid_response")
    text = str(value)
    if not text.isascii() or not text.isdigit() or not 0 < int(text) < 2**63:
        raise ReadError("invalid_response")
    return str(int(text))


def products_of(data):
    rows = data.get("products")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ReadError("incomplete")
    if any(not isinstance(p, dict) for p in rows):
        raise ReadError("invalid_response")
    ids = [positive_id(p.get("product_id")) for p in rows]
    if len(set(ids)) != len(ids):
        raise ReadError("invalid_response")
    return rows


class OzonInstanceAdapter:
    def __init__(self, source, credentials, *, transport=None):
        if source.marketplace != "ozon":
            raise ValueError("Wrong marketplace")
        self.source = source
        self.http = OzonTransport(credentials, transport=transport, attempts=1)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def _call(self, path, payload, *, write=False):
        allowed = {SET, UPDATE} if write else {GET, VALIDATE, STATUS, POSTING}
        if path not in allowed:
            raise ValueError("Undeclared exemplar operation")
        try:
            return _plain(body_of(self.http._once(path, payload, allow_empty=write)))
        except httpx.HTTPError:
            if write:
                raise OutcomeUnknown() from None
            raise ReadError("unavailable") from None
        except ReadError as error:
            if write and (error.status is None or error.status >= 500):
                raise OutcomeUnknown() from None
            raise

    def discover(self, posting_number):
        data = self._call(GET, {"posting_number": posting_number})
        if data.get("posting_number") != posting_number:
            raise ReadError("invalid_response")
        products = products_of(data)
        boxes = data.get("multi_box_qty")
        if boxes is not None and (type(boxes) is not int or not 1 <= boxes <= 1000):
            raise ReadError("invalid_response")
        # Only schema fields; no arbitrary raw response persistence.
        rows, seen = [], set()
        for product in products:
            quantity = product.get("quantity")
            exemplars = product.get("exemplars")
            if (
                type(quantity) is not int
                or not 1 <= quantity <= 1000
                or not isinstance(exemplars, list)
                or len(exemplars) != quantity
            ):
                raise ReadError("incomplete")
            row = {"product_id": positive_id(product["product_id"]), "quantity": quantity}
            for flag in (
                "is_mandatory_mark_needed",
                "is_mandatory_mark_possible",
                "is_gtd_needed",
                "is_rnpt_needed",
                "is_jw_uin_needed",
                "has_imei",
                "is_weight_needed",
            ):
                value = product.get(flag)
                if value is not None and type(value) is not bool:
                    raise ReadError("invalid_response")
                row[flag] = value
            row["exemplars"] = []
            for raw in exemplars:
                if not isinstance(raw, dict):
                    raise ReadError("invalid_response")
                eid = positive_id(raw.get("exemplar_id"))
                if eid in seen:
                    raise ReadError("invalid_response")
                seen.add(eid)
                unit = {"exemplar_id": eid}
                for key in ("gtd", "is_gtd_absent", "rnpt", "is_rnpt_absent", "weight", "marks"):
                    if key in raw and raw[key] is not None:
                        unit[key] = raw[key]
                marks = unit.setdefault("marks", [])
                if not isinstance(marks, list) or len(marks) > 10:
                    raise ReadError("invalid_response")
                clean = []
                for mark in marks:
                    if (
                        not isinstance(mark, dict)
                        or mark.get("mark_type") not in ("mandatory_mark", "imei", "jw_uin")
                        or not isinstance(mark.get("mark"), str)
                        or not 1 <= len(mark["mark"]) <= 512
                    ):
                        raise ReadError("invalid_response")
                    clean.append({"mark_type": mark["mark_type"], "mark": mark["mark"]})
                if sum(m["mark_type"] == "mandatory_mark" for m in clean) > 1:
                    raise ReadError("invalid_response")
                unit["marks"] = clean
                for key in ("gtd", "rnpt"):
                    if key in unit and (
                        not isinstance(unit[key], str)
                        or len(unit[key]) > 128
                        or bool(unit[key])
                        and not unit[key].isprintable()
                    ):
                        raise ReadError("invalid_response")
                    absent = "is_" + key + "_absent"
                    if absent in unit and type(unit[absent]) is not bool:
                        raise ReadError("invalid_response")
                if "weight" in unit:
                    weight = unit["weight"]
                    if (
                        isinstance(weight, bool)
                        or not isinstance(weight, (int, float))
                        or not 0 <= weight < 10**9
                    ):
                        raise ReadError("invalid_response")
                row["exemplars"].append(unit)
            row["exemplars"].sort(key=lambda e: int(e["exemplar_id"]))
            rows.append(row)
        posting = self.posting(posting_number, full=False)
        required = posting.get("requirements") or {}
        optional = posting.get("optional") or {}
        if not isinstance(required, dict) or not isinstance(optional, dict):
            raise ReadError("invalid_response")
        for product in rows:
            key = product["product_id"]
            product["required_fields"] = [
                field
                for suffix, field in (
                    ("mandatory_mark", "cis"),
                    ("gtd", "gtd"),
                    ("rnpt", "rnpt"),
                    ("weight", "weight"),
                    ("imei", "imei"),
                    ("jw_uin", "uin"),
                    ("country", "country"),
                    ("change_country", "country"),
                )
                if key in {positive_id(v) for v in required.get("products_requiring_" + suffix, [])}
            ]
            product["optional_fields"] = (
                ["cis"]
                if key in {positive_id(v) for v in optional.get("products_with_possible_mandatory_mark", [])}
                else []
            )
        return InstanceSnapshot(
            self.source,
            posting_number,
            tuple(sorted(rows, key=lambda p: int(p["product_id"]))),
            boxes,
            posting,
        )

    def validate(self, payload):
        return self._call(VALIDATE, payload)

    def submit(self, payload):
        self._call(SET, payload, write=True)

    def status(self, posting_number):
        data = self._call(STATUS, {"posting_number": posting_number})
        if data.get("posting_number") != posting_number:
            raise ReadError("invalid_response")
        products_of(data)
        return data

    def update(self, posting_number):
        self._call(UPDATE, {"posting_number": posting_number}, write=True)

    def posting(self, posting_number, *, full=True):
        raw = self._call(POSTING, {"posting_number": posting_number, "with": {"product_exemplars": full}})
        if raw.get("posting_number") != posting_number:
            raise ReadError("invalid_response")
        return {
            key: raw.get(key)
            for key in (
                "posting_number",
                "status",
                "substatus",
                "products",
                "product_exemplars",
                "requirements",
                "optional",
            )
        }


def _plain(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value
