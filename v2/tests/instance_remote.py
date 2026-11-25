"""Contract-shaped Ozon responses; no real credentials or live HTTP."""

import json
from copy import deepcopy

import httpx
from test_orders import OrderRemote

from pack_v2.integrations.ozon.instances import (
    GET,
    POSTING,
    SET,
    STATUS,
    UPDATE,
    VALIDATE,
    OzonInstanceAdapter,
)

GTIN = "09506000134352"
GTD = "10719040/123456/1234567"


def mark(serial="ABCDEF1234567", crypto="abcdefghijklmnop"):
    return f"01{GTIN}21{serial}\x1d91ABCD\x1d92{crypto}"


class InstanceRemote(OrderRemote):
    def __init__(self, db):
        super().__init__(db)
        self.details[1]["barcodes"] = (
            [GTIN, "OZN634662543"] if self.details[1]["id"] == 101 else self.details[1]["barcodes"]
        )
        for product in self.details:
            if product["id"] == 101:
                product["barcodes"] = [GTIN, "OZN634662543"]
        self.contexts = {}
        self.instance_calls = []
        self.saved = {}
        self.instance_mode = "ok"
        self.before_instance = None
        self.make_context(self.postings[0])

    def make_context(self, raw, *, cis=True, gtd=True):
        self.details_by_number[raw["posting_number"]] = deepcopy(raw)
        products = []
        ordinal = 1000
        for p in raw["products"]:
            needed = p["sku"] == 634662543
            units = []
            for _ in range(p["quantity"]):
                ordinal += 1
                units.append(
                    {
                        "exemplar_id": ordinal,
                        "is_gtd_absent": True,
                        "is_rnpt_absent": True,
                        "marks": [],
                        "weight": 0,
                    }
                )
            products.append(
                {
                    "product_id": p["sku"],
                    "quantity": p["quantity"],
                    "is_mandatory_mark_needed": False,
                    "is_mandatory_mark_possible": needed and cis,
                    "is_gtd_needed": needed and gtd,
                    "is_rnpt_needed": False,
                    "is_jw_uin_needed": False,
                    "has_imei": False,
                    "is_weight_needed": False,
                    "exemplars": units,
                }
            )
        self.contexts[raw["posting_number"]] = {
            "posting_number": raw["posting_number"],
            "multi_box_qty": 1,
            "products": products,
        }

    def factory_instances(self, source, credentials):
        return OzonInstanceAdapter(source, credentials, transport=httpx.MockTransport(self.handle_instances))

    def status_data(self, number):
        data = deepcopy(self.contexts[number])
        data["status"] = (
            "update_available"
            if self.details_by_number[number]["status"] == "awaiting_deliver"
            else "ship_available"
        )
        for p in data["products"]:
            for e in p["exemplars"]:
                for name in ("gtd", "rnpt", "weight"):
                    e[name + "_check_status"] = "passed"
                    e[name + "_error_codes"] = []
                for m in e["marks"]:
                    m["check_status"] = "passed"
                    m["error_codes"] = []
        return data

    def applied(self, number):
        raw = deepcopy(self.details_by_number[number])
        raw["product_exemplars"] = {"products": []}
        for p in self.contexts[number]["products"]:
            units = []
            for e in p["exemplars"]:
                d = deepcopy(e)
                marks = d.pop("marks", [])
                d["mandatory_mark"] = next(
                    (m["mark"] for m in marks if m["mark_type"] == "mandatory_mark"), None
                )
                units.append(d)
            raw["product_exemplars"]["products"].append({"sku": p["product_id"], "exemplars": units})
        return raw

    def handle_instances(self, request):
        assert self.db.engine.pool.checkedout() == 0, "No transaction may span HTTP"
        path = request.url.path
        data = json.loads(request.content)
        self.instance_calls.append((path, deepcopy(data), bytes(request.content)))
        if self.before_instance:
            callback, self.before_instance = self.before_instance, None
            callback(request, data)
        if self.instance_mode == "denied":
            return httpx.Response(403, json={"message": "private synthetic API-key and payload"})
        number = data.get("posting_number")
        if path == GET:
            return httpx.Response(200, json=self.contexts[number])
        if path == POSTING:
            raw = (
                self.applied(number)
                if number in self.saved and self.saved[number].get("updated")
                else self.details_by_number[number]
            )
            return httpx.Response(200, json={"result": raw})
        if path == VALIDATE:
            answer = deepcopy(data)
            for p in answer["products"]:
                p["valid"] = True
                for e in p["exemplars"]:
                    e["valid"], e["errors"] = True, []
                    for m in e["marks"]:
                        m["valid"], m["errors"] = self.instance_mode != "bad_mark", []
                        if not m["valid"]:
                            m["errors"] = ["synthetic secret full marking payload"]
                            e["valid"], p["valid"] = False, False
            return httpx.Response(200, json={"products": answer["products"]})
        if path == SET:
            if self.instance_mode == "set_429":
                return httpx.Response(429, headers={"retry-after": "1"})
            if self.instance_mode != "set_no_effect_timeout":
                by_id = {str(p["product_id"]): p for p in data["products"]}
                for p in self.contexts[number]["products"]:
                    p["exemplars"] = deepcopy(by_id[str(p["product_id"])]["exemplars"])
                self.saved[number] = {"request": deepcopy(data), "updated": False}
            if self.instance_mode in ("set_timeout", "set_no_effect_timeout"):
                raise httpx.ReadTimeout("synthetic secret full marking payload", request=request)
            return httpx.Response(200)
        if path == STATUS:
            answer = self.status_data(number)
            if self.instance_mode == "bad_cis_status":
                answer["status"] = "ship_not_available"
                for product in answer["products"]:
                    for unit in product["exemplars"]:
                        for mark in unit["marks"]:
                            mark["check_status"], mark["error_codes"] = "error", ["MARK_REJECTED"]
            if self.instance_mode in ("processing", "set_no_effect_timeout"):
                answer["status"] = "validation_in_process"
                for p in answer["products"]:
                    for e in p["exemplars"]:
                        for m in e["marks"]:
                            m["check_status"] = "processing"
            if self.instance_mode == "bad_gtd":
                for p in answer["products"]:
                    for e in p["exemplars"]:
                        if e.get("gtd"):
                            e["gtd_check_status"] = "error"
                            e["gtd_error_codes"] = ["synthetic full payload"]
            if self.instance_mode == "wrong_echo":
                for p in answer["products"]:
                    for e in p["exemplars"]:
                        for m in e["marks"]:
                            m["mark"] = mark(crypto="wrong-full-crypto")
            return httpx.Response(200, json=answer)
        if path == UPDATE:
            if self.instance_mode != "update_no_effect_timeout":
                self.saved[number]["updated"] = True
            if self.instance_mode in ("update_timeout", "update_no_effect_timeout"):
                raise httpx.ReadTimeout("private synthetic update", request=request)
            return httpx.Response(200)
        raise AssertionError("Unexpected instance endpoint")
