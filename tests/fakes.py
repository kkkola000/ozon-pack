"""Подделки Ozon и Avito API — только для тестов.

В самой панели их нет: она работает исключительно на данных площадок, а без
ключей ничего не показывает. Здесь же нужен предсказуемый источник заказов,
товаров и возвратов, чтобы проверять логику сборки без обращения к сети.
"""
from __future__ import annotations

import json
import random
import threading
from datetime import datetime, timedelta, timezone

from app import accounts, avito, ozon
from app.avito import (
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
from app.ozon import OzonClient, OzonError, _iso


SAMPLE_PRODUCTS = [
    ("1234567890", "ART-001", "Кофе зерновой Arabica 1 кг", "4600000000017"),
    ("1234567891", "ART-002", "Чайник электрический 1.7 л", "4600000000024"),
    ("1234567892", "ART-003", "Наушники TWS Pro", "4600000000031"),
    ("1234567893", "ART-004", "Пылесос вертикальный", "4600000000048"),
    ("1234567894", "ART-005", "Термокружка 500 мл", "4600000000055"),
    ("1234567895", "ART-006", "Гель для стирки 2 л", "4600000000062"),
    ("1234567896", "ART-007", "Лампа настольная LED", "4600000000079"),
    ("1234567897", "ART-008", "Рюкзак городской 20 л", "4600000000086"),
]

SAMPLE_CITIES = [
    ("Москва", "Москва"),
    ("Санкт-Петербург", "Санкт-Петербург"),
    ("Свердловская область", "Екатеринбург"),
    ("Татарстан", "Казань"),
    ("Новосибирская область", "Новосибирск"),
]


class FakeOzonClient(OzonClient):
    """Подделка Ozon Seller API: детерминированные отправления, товары и возвраты."""

    def __init__(self, seed: int = 0) -> None:  # noqa: D107 - без сетевого клиента
        self.client_id = "fake"
        self.api_key = "fake"
        self.base_url = "fake://ozon"
        self.max_retries = 1
        self._lock = threading.Lock()
        # Свой seed на кабинет: в разных кабинетах разные заказы, как в жизни.
        self._rnd = random.Random(20240501 + seed)
        # Номера отправлений тоже зависят от кабинета: иначе демо-магазины
        # выдавали бы одни и те же заказы и разделение данных нечем проверить.
        self._seed = seed
        self._postings: dict[str, dict] = {}
        self._returns: list[dict] = []
        self._generate()

    # -- генерация данных -------------------------------------------------
    def _generate(self) -> None:
        now = datetime.now(timezone.utc)
        for index in range(14):
            self._postings.update(self._make_posting(index, now))
        for index in range(11):
            self._returns.append(self._make_return(index, now))

    def _make_posting(self, index: int, now: datetime) -> dict[str, dict]:
        rnd = self._rnd
        base = 48000000 + self._seed * 100000 + index * 7
        number = f"{base}-{1000 + self._seed * 100 + index}-1"
        status = "awaiting_packaging" if index % 3 == 0 else "awaiting_deliver"
        positions = rnd.choice([1, 1, 1, 2, 2, 3])
        chosen = rnd.sample(SAMPLE_PRODUCTS, positions)
        products = [
            {
                "sku": int(sku),
                "offer_id": offer,
                "name": name,
                "quantity": rnd.choice([1, 1, 1, 2]),
                "price": f"{rnd.randrange(490, 9990)}.00",
                "currency_code": "RUB",
                "mandatory_mark": [],
            }
            for sku, offer, name, _bc in chosen
        ]
        region, city = rnd.choice(SAMPLE_CITIES)
        shipment = now + timedelta(hours=rnd.choice([3, 8, 20, 30, 44]))
        posting = {
            "posting_number": number,
            "order_id": 700000000 + self._seed * 10000 + index,
            "order_number": f"{base}-{1000 + self._seed * 100 + index}",
            "status": status,
            "substatus": "posting_acceptance_in_progress" if status == "awaiting_deliver" else "posting_created",
            "in_process_at": _iso(now - timedelta(hours=rnd.randrange(2, 40))),
            "shipment_date": _iso(shipment),
            "delivering_date": None,
            "delivery_method": {
                "id": 21321684811000 + index,
                "name": rnd.choice(["Ozon Логистика курьеру, Москва", "Ozon Логистика самостоятельно, Москва"]),
                "warehouse": "Основной склад",
                "warehouse_id": 21321684811,
                "tpl_provider": "Ozon Логистика",
                "tpl_provider_id": 24,
            },
            "tracking_number": "",
            "is_express": index % 7 == 0,
            "is_multibox": False,
            "multi_box_qty": 1,
            "barcodes": {"upper_barcode": f"%03{index:05d}", "lower_barcode": f"OZN{index:09d}"},
            "analytics_data": {
                "region": region,
                "city": city,
                "delivery_type": "PVZ",
                "is_premium": index % 4 == 0,
                "payment_type_group_name": rnd.choice(["Оплачено", "Оплата при получении"]),
                "warehouse": "Основной склад",
                "warehouse_id": 21321684811,
            },
            "products": products,
            "requirements": {
                "products_requiring_gtd": [],
                "products_requiring_country": [],
                "products_requiring_mandatory_mark": [],
                "products_requiring_rnpt": [],
            },
            "cancellation": {"cancel_reason": "", "cancel_reason_id": 0},
            "available_actions": ["ship", "cancel"],
        }
        return {number: posting}

    def _make_return(self, index: int, now: datetime) -> dict:
        rnd = self._rnd
        sku, offer, name, _bc = rnd.choice(SAMPLE_PRODUCTS)
        ready = index % 4 != 3
        scheme = rnd.choice(["FBO", "FBS"])
        status = ("ArrivedAtReturnPlace" if ready else "MovingToSeller")
        display = "В пункте выдачи" if ready else "Едет к продавцу"
        arrived = now - timedelta(days=rnd.randrange(0, 12))
        return {
            "id": 90000000 + self._seed * 1000 + index,
            "company_id": 1,
            "return_reason_name": rnd.choice(
                ["Не подошёл размер", "Товар повреждён", "Не соответствует описанию", "Передумал"]
            ),
            "type": scheme,
            "schema": scheme,
            "order_id": 700000000 + index,
            "order_number": f"{48000000 + index * 5}-{1200 + index}",
            "posting_number": f"{48000000 + index * 5}-{1200 + index}-1",
            "place": {"id": 100 + index % 3, "name": "ПВЗ Москва, Ленинский 25", "address": "Москва, Ленинский пр-т, 25"},
            "target_place": {"id": 5, "name": "Основной склад", "address": "Москва, ул. Складская, 1"},
            "storage": {
                "sum": {"currency_code": "RUB", "price": float(rnd.randrange(0, 400))},
                "arrived_moment": _iso(arrived) if ready else None,
                "days": (now - arrived).days if ready else 0,
                "tariffication_start_date": _iso(arrived + timedelta(days=5)),
                "utilization_forecast_date": _iso(arrived + timedelta(days=60))[:10],
            },
            "product": {
                "sku": int(sku),
                "offer_id": offer,
                "name": name,
                "quantity": 1,
                "price": {"currency_code": "RUB", "price": float(rnd.randrange(490, 9990))},
            },
            "logistic": {
                "return_date": _iso(arrived - timedelta(days=2)),
                "final_moment": _iso(arrived) if ready else None,
                "barcode": f"RET{90000000 + index}",
            },
            "visual": {"status": {"id": index, "display_name": display, "sys_name": status}, "change_moment": _iso(arrived)},
            "additional_info": {"is_opened": index % 5 == 0, "is_super_econom": False},
        }

    # -- методы API -------------------------------------------------------
    def posting_list(self, status, since, to, *, limit=1000, offset=0):  # type: ignore[override]
        items = [p for p in self._postings.values() if not status or p["status"] == status]
        items.sort(key=lambda p: p["shipment_date"])
        page = items[offset : offset + limit]
        return [json.loads(json.dumps(p)) for p in page], offset + limit < len(items)

    def posting_get(self, posting_number):  # type: ignore[override]
        posting = self._postings.get(posting_number)
        return json.loads(json.dumps(posting)) if posting else None

    def posting_by_barcode(self, barcode):  # type: ignore[override]
        for posting in self._postings.values():
            codes = posting.get("barcodes") or {}
            if barcode in {codes.get("upper_barcode"), codes.get("lower_barcode"), posting["posting_number"]}:
                return json.loads(json.dumps(posting))
        return None

    def ship(self, posting_number, packages):  # type: ignore[override]
        posting = self._postings.get(posting_number)
        if not posting:
            raise OzonError("Отправление не найдено", status=404)
        if posting["status"] != "awaiting_packaging":
            raise OzonError(f"Отправление уже в статусе {posting['status']}", status=409)
        posting["status"] = "awaiting_deliver"
        posting["substatus"] = "posting_awaiting_deliver"
        return {"postings": [posting_number], "additional_data": []}

    def package_label(self, posting_numbers):  # type: ignore[override]
        from tests.pdfstub import make_label_pdf

        pages = []
        for number in posting_numbers:
            posting = self._postings.get(number)
            if not posting:
                continue
            pages.append(
                {
                    "posting_number": number,
                    "order_number": posting.get("order_number", ""),
                    "city": (posting.get("analytics_data") or {}).get("city", ""),
                    "warehouse": (posting.get("delivery_method") or {}).get("warehouse", ""),
                    "tpl": (posting.get("delivery_method") or {}).get("tpl_provider", ""),
                    "products": [(p["name"], p["quantity"]) for p in posting.get("products", [])],
                }
            )
        if not pages:
            raise OzonError("Нет отправлений для печати", status=404)
        return make_label_pdf(pages), "label-fake.pdf"

    def product_info(self, skus=None, offer_ids=None):  # type: ignore[override]
        wanted_sku = {str(s) for s in (skus or [])}
        wanted_offer = {str(o) for o in (offer_ids or [])}
        items = []
        for sku, offer, name, barcode in SAMPLE_PRODUCTS:
            if sku in wanted_sku or offer in wanted_offer:
                items.append(
                    {
                        "sku": int(sku),
                        "id": int(sku),
                        "offer_id": offer,
                        "name": name,
                        "barcodes": [barcode],
                        "primary_image": [],
                    }
                )
        return items

    def returns_list(self, *, limit=500, last_id=0, filter_=None):  # type: ignore[override]
        items = [json.loads(json.dumps(r)) for r in self._returns]
        wanted = (filter_ or {}).get("visual_status_name")
        if wanted:
            items = [r for r in items if (r["visual"]["status"]["sys_name"] == wanted)]
        start = 0
        if last_id:
            ids = [r["id"] for r in items]
            start = ids.index(last_id) + 1 if last_id in ids else len(items)
        page = items[start : start + limit]
        return page, start + limit < len(items)

    def returns_fbs_points(self, *, limit=100, last_id=0):  # type: ignore[override]
        return [
            {"id": 100, "name": "ПВЗ Москва, Ленинский 25", "address": "Москва, Ленинский пр-т, 25", "returns_count": 6},
            {"id": 101, "name": "ПВЗ Москва, Профсоюзная 14", "address": "Москва, Профсоюзная, 14", "returns_count": 2},
        ]

    def giveout_pdf(self):  # type: ignore[override]
        from tests.pdfstub import make_giveout_pdf

        return make_giveout_pdf("FAKE-GIVEOUT-0001")

    def ping(self):  # type: ignore[override]
        return {"ok": True, "fake": True}

    def close(self):  # type: ignore[override]
        return None


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

# ------------------------------------------------------------------ подстановка
def install(monkeypatch) -> None:
    """Подменить фабрики клиентов на подделки — на время одного теста."""
    ozon_clients: dict[int, FakeOzonClient] = {}
    avito_clients: dict[int, FakeAvitoClient] = {}

    def fake_ozon(account: dict | None = None) -> FakeOzonClient:
        if account is None:
            account = accounts.default_account()
        account_id = int((account or {}).get("id") or 0)
        return ozon_clients.setdefault(account_id, FakeOzonClient(seed=account_id))

    def fake_avito(account: dict | None = None) -> FakeAvitoClient:
        account_id = int((account or {}).get("id") or 0)
        return avito_clients.setdefault(account_id, FakeAvitoClient(seed=account_id))

    monkeypatch.setattr(ozon, "get_client", fake_ozon)
    monkeypatch.setattr(avito, "get_client", fake_avito)
