"""Подделка Avito API — только для тестов: заказы в рабочих статусах и возвраты."""
from __future__ import annotations

import random
import threading
from datetime import datetime, timedelta, timezone

from app.markets.avito.client import (
    RETURN_IN_TRANSIT,
    RETURN_READY,
    RETURN_READY_ALT,
    SERVICE_LABELS,
    STATUS_CANCELED,
    STATUS_CLOSED,
    STATUS_DELIVERED,
    STATUS_IN_TRANSIT,
    STATUS_ON_CONFIRMATION,
    STATUS_ON_RETURN,
    STATUS_READY_TO_SHIP,
    TRANSITION_CONFIRM,
    TRANSITION_PERFORM,
    AvitoClient,
    AvitoError,
)


SAMPLE_ITEMS = [
    ("2799377316", "Кеды Venice, 42", 4990.0),
    ("2799377317", "Куртка ветровка, M", 3590.0),
    ("2799377318", "Рюкзак городской 20 л", 2450.0),
    ("2799377319", "Гантели разборные 2×8 кг", 5900.0),
    ("2799377320", "Кофеварка гейзерная 300 мл", 1890.0),
    ("2799377321", "Настольная лампа LED", 1290.0),
]

SAMPLE_SERVICES = [
    ("pvz", "Boxberry"),
    ("pvz", "СДЭК"),
    ("dbs", "Своя доставка"),
    ("rdbs", "Курьер продавца"),
    ("postamat", "Halva Postamat"),
]

SAMPLE_BUYERS = [
    ("Иванов Иван Иванович", "79161234567"),
    ("Петрова Мария Сергеевна", "79031234568"),
    ("Кузнецов Пётр Алексеевич", "79219876543"),
]


class FakeAvitoClient(AvitoClient):
    """Подделка Avito API: заказы в рабочих статусах и возвраты."""

    def __init__(self, seed: int = 0) -> None:  # noqa: D107 - без сетевого клиента
        self.client_id = "fake"
        self.client_secret = "fake"
        self.base_url = "fake://avito"
        self.max_retries = 1
        self._lock = threading.Lock()
        self._rnd = random.Random(20240902 + seed)
        # Номера заказов зависят от кабинета: разные демо-магазины — разные заказы.
        self._seed = seed
        self._orders: dict[str, dict] = {}
        self._generate()

    def _generate(self) -> None:
        now = datetime.now(timezone.utc)
        for index in range(12):
            order = self._make_order(index, now)
            self._orders[order["id"]] = order

    def _make_order(self, index: int, now: datetime) -> dict:
        rnd = self._rnd
        status = STATUS_ON_CONFIRMATION if index % 2 == 0 else STATUS_READY_TO_SHIP
        if 7 <= index < 9:
            status = rnd.choice([STATUS_IN_TRANSIT, STATUS_DELIVERED, STATUS_CLOSED])
        elif index >= 9:
            status = STATUS_ON_RETURN
        service_type, service_name = SAMPLE_SERVICES[index % len(SAMPLE_SERVICES)]
        positions = rnd.randint(1, 3)
        items = []
        for pos in range(positions):
            avito_id, title, price = SAMPLE_ITEMS[(index + pos) % len(SAMPLE_ITEMS)]
            count = rnd.randint(1, 2)
            items.append(
                {
                    "avitoId": avito_id,
                    "id": f"ART-{avito_id[-4:]}",
                    "title": title,
                    "count": count,
                    "location": "Москва",
                    "prices": {"price": price, "total": price * count, "commission": round(price * 0.05, 2)},
                }
            )
        total = sum(item["prices"]["total"] for item in items)
        buyer_name, buyer_phone = SAMPLE_BUYERS[index % len(SAMPLE_BUYERS)]
        created = now - timedelta(hours=rnd.randint(1, 40))
        if status == STATUS_ON_RETURN:
            # Возврат приезжает через недели и месяцы после покупки.
            created = now - timedelta(days=rnd.randint(45, 150))
        return_policy = None
        if status == STATUS_ON_RETURN:
            # Два возврата уже в пункте выдачи — двумя написаниями, которые
            # встречаются у Avito, — и один ещё едет: видно, что его отбросят.
            by_index = {9: RETURN_READY_ALT, 10: RETURN_IN_TRANSIT, 11: RETURN_READY}
            return_policy = {
                "returnStatus": by_index.get(index, RETURN_IN_TRANSIT),
                "trackingNumber": f"RT{index:011d}",
            }
        actions = []
        if status == STATUS_ON_CONFIRMATION:
            actions = [{"name": "confirm", "required": True}, {"name": "reject", "required": False}]
        elif status == STATUS_READY_TO_SHIP:
            actions = [{"name": "reject", "required": False}]
            if service_type == "rdbs":
                actions.insert(0, {"name": "perform", "required": True})
        return {
            "id": f"500000000{self._seed:03d}{index:04d}",
            "marketplaceId": f"700000000{self._seed:03d}{index:04d}",
            "status": status,
            "createdAt": created.isoformat().replace("+00:00", "Z"),
            "updatedAt": now.isoformat().replace("+00:00", "Z"),
            "availableActions": actions,
            "delivery": {
                "serviceType": service_type,
                "serviceName": service_name,
                "dispatchNumber": f"0000{index:09d}",
                "trackingNumber": f"AV{index:011d}",
                "buyerInfo": {"fullName": buyer_name, "phoneNumber": buyer_phone}
                if service_type in {"dbs", "rdbs"}
                else None,
                "terminalInfo": {"code": f"MSK{index:02d}", "address": "Москва, Настасьинский 8с2"}
                if service_type in {"pvz", "postamat"}
                else None,
            },
            "items": items,
            "returnPolicy": return_policy,
            "prices": {
                "price": total,
                "total": round(total * 0.93, 2),
                "delivery": 0 if service_type == "pvz" else 350,
                "commission": round(total * 0.07, 2),
                "discount": 0,
            },
            "schedules": {
                "confirmTill": (created + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
                "shipTill": (created + timedelta(hours=48)).isoformat().replace("+00:00", "Z"),
                "deliveryDateMin": None,
                "deliveryDateMax": None,
            },
        }

    # -- имитация методов -------------------------------------------------
    def token(self, *, force: bool = False):  # type: ignore[override]
        return "fake-token"

    def orders(self, *, statuses=None, ids=None, date_from=None, page=1, limit=20):  # type: ignore[override]
        wanted = set(statuses or [])
        wanted_ids = {str(i) for i in (ids or [])}

        def fresh_enough(order: dict) -> bool:
            # dateFrom у Avito отсекает по дате СОЗДАНИЯ заказа, а не по дате
            # события. Подделка обязана вести себя так же, иначе слишком
            # узкое окно в синхронизации останется незамеченным.
            if date_from is None:
                return True
            created = datetime.fromisoformat(order["createdAt"].replace("Z", "+00:00"))
            return created >= date_from

        rows = [
            o for o in self._orders.values()
            if (not wanted or o["status"] in wanted)
            and (not wanted_ids or o["id"] in wanted_ids)
            and fresh_enough(o)
        ]
        rows.sort(key=lambda o: o["createdAt"])
        start = (max(page, 1) - 1) * limit
        chunk = rows[start : start + limit]
        return [dict(o) for o in chunk], start + limit < len(rows)

    def apply_transition(self, order_id, transition):  # type: ignore[override]
        order = self._orders.get(str(order_id))
        if order is None:
            raise AvitoError("Заказ не найден", status=404)
        if transition == TRANSITION_CONFIRM:
            if order["status"] != STATUS_ON_CONFIRMATION:
                raise AvitoError("Заказ уже подтверждён", status=409)
            order["status"] = STATUS_READY_TO_SHIP
            order["availableActions"] = (
                [{"name": "perform", "required": True}]
                if order["delivery"]["serviceType"] == "rdbs"
                else [{"name": "reject", "required": False}]
            )
        elif transition == TRANSITION_PERFORM:
            if order["status"] != STATUS_READY_TO_SHIP:
                raise AvitoError("Заказ не готов к отправке", status=409)
            order["status"] = STATUS_IN_TRANSIT
            order["availableActions"] = []
        elif transition == "reject":
            order["status"] = STATUS_CANCELED
            order["availableActions"] = []
        else:
            raise AvitoError(f"Неизвестный переход {transition}", status=400)
        order["updatedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return True

    def label_task(self, marketplace_ids):  # type: ignore[override]
        return "fake-task-" + "-".join(str(i) for i in marketplace_ids)[:40]

    def label_pdf(self, marketplace_ids, *, wait=60):  # type: ignore[override]
        from tests.pdfstub import make_label_pdf

        wanted = {str(i) for i in marketplace_ids}
        pages = []
        for order in self._orders.values():
            if str(order.get("marketplaceId")) not in wanted:
                continue
            delivery = order.get("delivery") or {}
            pages.append(
                {
                    "posting_number": str(order.get("marketplaceId")),
                    "order_number": str(order.get("id")),
                    "city": ((delivery.get("terminalInfo") or {}).get("address") or "")[:40],
                    "warehouse": SERVICE_LABELS.get(delivery.get("serviceType") or "", ""),
                    "tpl": delivery.get("serviceName") or "Avito Доставка",
                    "products": [(i["title"], i["count"]) for i in order.get("items") or []],
                }
            )
        if not pages:
            raise AvitoError("Нет заказов для печати", status=404)
        return make_label_pdf(pages), "avito-label-fake.pdf"

    def self_info(self):  # type: ignore[override]
        return {"id": 94235311, "name": "Тестовый магазин", "email": "fake@example.com"}

    def close(self):  # type: ignore[override]
        return None
