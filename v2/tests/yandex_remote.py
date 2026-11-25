"""Official-shape FBS fixtures, only synthetic identifiers and mock HTTP."""

import json
from copy import deepcopy

import httpx

from pack_v2.integrations.yandex.access import YandexAccessChecker
from pack_v2.integrations.yandex.instances import YandexInstanceAdapter
from pack_v2.integrations.yandex.reader import YandexReadAdapter

ORDER = "90000000001"
ITEM = "900000001"
BARCODE = "MRKT900000001"
GTD = "10719040/123456/1234567"
GTIN = "09506000134352"


def mark(serial="TESTSERIAL001"):
    return "01" + GTIN + "21" + serial + "\x1d91ABCD\x1d92abcdefghijklmnop"


def order():
    return {
        "orderId": int(ORDER),
        "campaignId": 777,
        "programType": "FBS",
        "status": "PROCESSING",
        "substatus": "STARTED",
        "items": [
            {
                "id": int(ITEM),
                "offerId": "SYN-MARKED",
                "offerName": "Тестовый товар",
                "count": 1,
                "requiredInstanceTypes": ["CIS_OPTIONAL", "GTD"],
                "prices": {
                    "payment": {"value": 1800, "currencyId": "RUR"},
                    "cashback": {"value": 200, "currencyId": "RUR"},
                },
            }
        ],
        "delivery": {
            "warehouseId": 99,
            "shipment": {"shipmentDate": "2026-12-01"},
            "boxesLayout": [
                {"boxId": 888, "barcode": "seller-box-888", "items": [{"id": int(ITEM), "fullCount": 1}]}
            ],
        },
        "buyer": {"name": "PRIVATE BUYER"},
        "receiveCode": "PRIVATE SECRET",
    }


def offer():
    return {
        "offer": {
            "offerId": "SYN-MARKED",
            "name": "Тестовый товар",
            "barcodes": [GTIN, BARCODE],
            "markingDeclared": True,
            "pictures": ["https://example.invalid/p.png"],
        },
        "mapping": {"marketSku": 2345},
    }


class YandexRemote:
    def __init__(self):
        self.raw = order()
        self.card = offer()
        self.additional_cards = []
        self.calls = []
        self.cis_status, self.substatus = "IN_PROGRESS", None
        self.status_full = True
        self.timeout = None
        self.malformed = False
        self.apply = True
        self.transport = httpx.MockTransport(self.handle)

    def reader(self, source, credentials):
        return YandexReadAdapter(source, credentials, transport=self.transport)

    def instances(self, source, credentials):
        return YandexInstanceAdapter(source, credentials, transport=self.transport)

    def checker(self):
        return YandexAccessChecker(transport=self.transport)

    def handle(self, request):
        data = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, deepcopy(data), request.content))
        assert request.headers["Api-Key"] == "synthetic-yandex-key"
        assert "Client-Id" not in request.headers
        path = request.url.path
        if path.endswith("/offer-mappings"):
            rows = [] if data.get("archived") else deepcopy([self.card, *self.additional_cards])
            return httpx.Response(200, json={"status": "OK", "result": {"offerMappings": rows}})
        if path == "/v2/campaigns":
            return httpx.Response(200, json={"campaigns": [{"id": 777}]})
        if path == "/v1/businesses/123/orders":
            return httpx.Response(200, json={"orders": [deepcopy(self.raw)]})
        if path == f"/v2/campaigns/777/orders/{ORDER}/identifiers/status":
            cis = []
            for value in self.raw["items"][0].get("instances", []):
                full = value.get("cisFull") or value.get("cis")
                if full:
                    row = {
                        "value": full if self.status_full else full.split("\x1d")[0],
                        "status": self.cis_status,
                    }
                    if self.substatus:
                        row["substatus"] = self.substatus
                    cis.append(row)
            return httpx.Response(
                200, json={"status": "OK", "result": {"items": [{"id": int(ITEM), "cis": cis}]}}
            )
        if path == f"/v2/campaigns/777/orders/{ORDER}/boxes" and request.method == "PUT":
            assert set(data) == {"boxes", "allowRemove"} and data["allowRemove"] is False
            if self.apply:
                self.raw["delivery"]["boxesLayout"] = deepcopy(data["boxes"])
                for item in self.raw["items"]:
                    item["instances"] = [
                        deepcopy(i)
                        for box in data["boxes"]
                        for e in box["items"]
                        if e["id"] == item["id"] and e.get("partialCount", {}).get("current", 1) == 1
                        for i in e.get("instances", [])
                    ]
            if self.timeout:
                raise httpx.ReadTimeout("synthetic timeout")
            return httpx.Response(
                200,
                json={"status": "OK", "result": {} if self.malformed else {"boxes": deepcopy(data["boxes"])}},
            )
        raise AssertionError((request.method, path, data))
