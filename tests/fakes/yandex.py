"""Подделка Partner API Яндекс Маркета — только для тестов: заказы, ярлыки, каталог."""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from app.markets.yandex.client import (
    SUBSTATUS_READY_TO_SHIP,
    SUBSTATUS_STARTED,
    YandexClient,
    YandexError,
)

from tests.fakes.ozon import CATALOG_ARCHIVED, CATALOG_EXTRA, SAMPLE_PRODUCTS


# Заказы Маркета собираются из тех же артикулов, что и каталог Ozon: именно так
# панель находит штрихкоды. Пара заказов уже уехала — их панель обязана отсеять.
YANDEX_DELIVERY = [
    ("DELIVERY", "Яндекс Доставка"),
    ("PICKUP", "Пункт выдачи Маркета"),
    ("DELIVERY", "Курьер Маркета"),
]


class FakeYandexClient(YandexClient):
    """Подделка Partner API Маркета: заказы в работе, ярлыки к ним и каталог товаров."""

    def __init__(self, seed: int = 0) -> None:  # noqa: D107 - без сетевого клиента
        self.business_id = str(9000000 + seed)
        self.api_key = "fake"
        self.base_url = "fake://yandex"
        self.max_retries = 1
        self._rnd = random.Random(20250901 + seed)
        self._seed = seed
        self._orders: dict[str, dict] = {}
        self._reports: dict[str, list[str]] = {}
        # Проверки включают это, чтобы изобразить Маркет, который отдал заказ
        # мимо фильтра по этапу: панель обязана отсеять такое сама.
        self.ignore_filter = False
        # Каталог кабинета: те же товары, что у Ozon, — панель ищет штрихкоды
        # по артикулу продавца, и он у магазина один на все площадки.
        self.catalog = [(offer, name, barcode) for _sku, offer, name, barcode
                        in SAMPLE_PRODUCTS + CATALOG_EXTRA]
        self.archived = [(offer, name, barcode) for _sku, offer, name, barcode in CATALOG_ARCHIVED]
        self._generate()

    def _generate(self) -> None:
        now = datetime.now(timezone.utc)
        for index in range(10):
            order = self._make_order(index, now)
            self._orders[str(order["orderId"])] = order

    def _make_order(self, index: int, now: datetime) -> dict:
        rnd = self._rnd
        status, substatus = "PROCESSING", (SUBSTATUS_STARTED if index % 3 else SUBSTATUS_READY_TO_SHIP)
        if index >= 8:
            # Уехавшие заказы: Маркет их с фильтром не отдаст, но на всякий
            # случай они есть — панель отсеивает такое и у себя.
            status, substatus = "DELIVERY", "DELIVERY_SERVICE_RECEIVED"
        positions = rnd.choice([1, 1, 2, 2, 3])
        chosen = rnd.sample(SAMPLE_PRODUCTS, positions)
        items = []
        for pos, (sku, offer, name, _bc) in enumerate(chosen):
            count = rnd.choice([1, 1, 2])
            price = float(rnd.randrange(490, 9990))
            items.append({
                "id": 500000 + self._seed * 1000 + index * 10 + pos,
                "offerId": offer,
                "offerName": name,
                "marketSku": int(sku),
                "shopSku": offer,
                "count": count,
                "vat": "VAT_20",
                "price": price,
                "buyerPrice": price,
                "subsidy": 0,
                "prices": {"payment": {"value": price * count, "currencyId": "RUR"}},
            })
        delivery_type, service = YANDEX_DELIVERY[index % len(YANDEX_DELIVERY)]
        created = now - timedelta(hours=rnd.randint(1, 30))
        shipment = now + timedelta(hours=rnd.choice([4, 10, 22, 36]))
        return {
            "orderId": 80000000 + self._seed * 100000 + index,
            "campaignId": 21000000 + self._seed,
            "externalOrderId": f"YM-{self._seed}-{index:03d}",
            "status": status,
            "substatus": substatus,
            "programType": "FBS",
            "buyerType": "PERSON",
            "creationDate": created.strftime("%d-%m-%Y %H:%M:%S"),
            "updateDate": now.strftime("%d-%m-%Y %H:%M:%S"),
            "notes": "Позвонить перед доставкой" if index % 4 == 0 else "",
            "delivery": {
                "type": delivery_type,
                "serviceName": service,
                "shipment": {"id": 700000 + index, "shipmentDate": shipment.strftime("%d-%m-%Y")},
            },
            "items": items,
            "prices": {
                "payment": {"value": sum(i["prices"]["payment"]["value"] for i in items), "currencyId": "RUR"},
            },
        }

    # -- имитация методов -------------------------------------------------
    def orders(self, *, substatuses=None, page_token=None, limit=50):  # type: ignore[override]
        wanted = set(substatuses or [])
        rows = [
            o for o in self._orders.values()
            if o["status"] == "PROCESSING"
            and (self.ignore_filter or not wanted or o["substatus"] in wanted)
        ]
        rows.sort(key=lambda o: o["orderId"])
        start = int(page_token or 0)
        chunk = rows[start : start + limit]
        next_token = str(start + limit) if start + limit < len(rows) else None
        return [json.loads(json.dumps(o)) for o in chunk], next_token

    def offer_mappings(self, *, page_token=None, limit=100, offer_ids=None, archived=None):  # type: ignore[override]
        """Каталог страницами. Архив Маркет отдаёт только по отдельной просьбе."""
        rows = self.archived if archived else self.catalog
        if offer_ids:
            wanted = {str(offer) for offer in offer_ids}
            rows = [row for row in self.catalog + self.archived if row[0] in wanted]
            return [self._mapping(*row, archived=bool(row in self.archived)) for row in rows], None
        start = int(page_token or 0)
        page = rows[start : start + limit]
        next_token = str(start + limit) if start + limit < len(rows) else None
        return [self._mapping(*row, archived=bool(archived)) for row in page], next_token

    def _mapping(self, offer: str, name: str, barcode: str, *, archived: bool = False) -> dict:
        """Карточка в том виде, в каком её отдаёт offer-mappings."""
        return {
            "offer": {
                "offerId": offer,
                "name": name,
                "barcodes": [barcode],
                "pictures": [f"fake://yandex/{offer}.jpg"],
                "vendor": "Ozon Pack",
                "vendorCode": offer,
                "archived": archived,
                "cardStatus": "HAS_CARD_CAN_UPDATE",
            },
            "mapping": {"marketSku": 100000 + sum(ord(c) for c in offer), "marketSkuName": name},
        }

    def labels_task(self, order_ids, *, sorted_as_given=True):  # type: ignore[override]
        if not order_ids:
            raise YandexError("Не передано ни одного заказа")
        report_id = f"fake-report-{len(self._reports) + 1}"
        self._reports[report_id] = [str(i) for i in order_ids]
        return report_id

    def report_info(self, report_id):  # type: ignore[override]
        if report_id not in self._reports:
            raise YandexError("Отчёт не найден", status=404)
        return {"status": "DONE", "sub_status": "", "link": f"fake://yandex/{report_id}.pdf", "raw": {}}

    def download(self, url):  # type: ignore[override]
        report_id = url.rsplit("/", 1)[-1].removesuffix(".pdf")
        return self._pdf(self._reports.get(report_id, []))

    def labels_pdf(self, order_ids, *, wait=120):  # type: ignore[override]
        report_id = self.labels_task(order_ids)
        return self.download(self.report_info(report_id)["link"]), "yandex-labels.pdf"

    def order_labels(self, campaign_id, order_id):  # type: ignore[override]
        return self._pdf([str(order_id)])

    def _pdf(self, order_ids: list[str]) -> bytes:
        from tests.pdfstub import make_label_pdf

        pages = []
        for order_id in order_ids:
            order = self._orders.get(str(order_id))
            if not order:
                continue
            delivery = order.get("delivery") or {}
            pages.append({
                "posting_number": str(order["orderId"]),
                "order_number": order.get("externalOrderId") or "",
                "city": "",
                "warehouse": delivery.get("type") or "",
                "tpl": delivery.get("serviceName") or "Яндекс Маркет",
                "products": [(i["offerName"], i["count"]) for i in order.get("items") or []],
            })
        if not pages:
            raise YandexError("Нет заказов для печати", status=404)
        return make_label_pdf(pages)

    def ping(self):  # type: ignore[override]
        return {"orders": len(self._orders), "business_id": self.business_id, "fake": True}

    def close(self):  # type: ignore[override]
        return None
