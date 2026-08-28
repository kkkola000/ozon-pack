"""Клиент Ozon Seller API.

Пути методов сверены с публичной схемой Seller API:
  POST /v3/posting/fbs/list            — список FBS-отправлений
  POST /v3/posting/fbs/get             — одно отправление
  POST /v4/posting/fbs/ship            — сборка отправления (в «Ожидает отгрузки»)
  POST /v2/posting/fbs/package-label   — стикер отправления (PDF)
  POST /v2/posting/fbs/package-label/create + /v1/posting/fbs/package-label/get
                                       — асинхронная генерация стикера
  POST /v2/posting/fbs/get-by-barcode  — отправление по штрихкоду стикера
  POST /v3/product/info/list           — карточки товаров (штрихкоды, фото)
  POST /v1/returns/list                — возвраты FBO и FBS
  POST /v1/returns/company/fbs/info    — количество возвратов по пунктам выдачи
  POST /v1/return/giveout/get-pdf      — акт/штрихкод на выдачу возвратов
"""
from __future__ import annotations

import base64
import json
import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("ozon")

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4


class OzonError(RuntimeError):
    """Ошибка обращения к Ozon Seller API."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None, payload: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.payload = payload

    def __str__(self) -> str:  # pragma: no cover - тривиально
        parts = [self.message]
        if self.status:
            parts.append(f"HTTP {self.status}")
        if self.code:
            parts.append(str(self.code))
        return " | ".join(parts)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class OzonClient:
    """Синхронный клиент: приложение работает в пуле потоков, async не нужен."""

    def __init__(self, client_id: str | None = None, api_key: str | None = None, base_url: str | None = None):
        self.client_id = client_id or settings.ozon_client_id
        self.api_key = api_key or settings.ozon_api_key
        self.base_url = (base_url or settings.ozon_base_url).rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(settings.ozon_timeout),
            headers={
                "Client-Id": self.client_id,
                "Api-Key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ низкий уровень
    def _request(self, path: str, payload: dict | None = None) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.post(path, content=json.dumps(payload or {}, ensure_ascii=False).encode())
            except httpx.HTTPError as exc:  # сеть/таймаут
                last_error = exc
                time.sleep(min(2 ** attempt, 10))
                continue
            if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                delay = float(response.headers.get("Retry-After") or min(2 ** attempt, 10))
                log.warning("Ozon %s -> %s, повтор через %.0fс", path, response.status_code, delay)
                time.sleep(delay)
                continue
            return response
        raise OzonError(f"Сеть недоступна: {last_error}")

    def post(self, path: str, payload: dict | None = None) -> dict:
        response = self._request(path, payload)
        if response.status_code >= 400:
            message, code = self._extract_error(response)
            raise OzonError(message, status=response.status_code, code=code, payload=response.text[:2000])
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise OzonError(f"Некорректный ответ Ozon: {exc}", status=response.status_code) from exc

    @staticmethod
    def _extract_error(response: httpx.Response) -> tuple[str, str | None]:
        try:
            data = response.json()
        except ValueError:
            return response.text[:300] or f"HTTP {response.status_code}", None
        message = data.get("message") or data.get("error", {}).get("message") or json.dumps(data, ensure_ascii=False)[:300]
        code = str(data.get("code") or data.get("error", {}).get("code") or "") or None
        return message, code

    # ------------------------------------------------------------------ отправления FBS
    def posting_list(
        self,
        status: str | None,
        since: datetime,
        to: datetime,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> tuple[list[dict], bool]:
        payload = {
            "dir": "ASC",
            "filter": {"since": _iso(since), "to": _iso(to)},
            "limit": limit,
            "offset": offset,
            "with": {"analytics_data": True, "barcodes": True, "financial_data": False, "translit": False},
        }
        if status:
            payload["filter"]["status"] = status
        data = self.post("/v3/posting/fbs/list", payload)
        result = data.get("result") or {}
        return list(result.get("postings") or []), bool(result.get("has_next"))

    def posting_get(self, posting_number: str) -> dict | None:
        payload = {
            "posting_number": posting_number,
            "with": {"analytics_data": True, "barcodes": True, "financial_data": False, "product_exemplars": False},
        }
        try:
            data = self.post("/v3/posting/fbs/get", payload)
        except OzonError as exc:
            if exc.status == 404:
                return None
            raise
        return data.get("result") or None

    def posting_by_barcode(self, barcode: str) -> dict | None:
        try:
            data = self.post("/v2/posting/fbs/get-by-barcode", {"barcode": barcode})
        except OzonError as exc:
            if exc.status in (400, 404):
                return None
            raise
        return data.get("result") or None

    def ship(self, posting_number: str, packages: list[list[dict]]) -> dict:
        """Собрать отправление: v4/posting/fbs/ship.

        packages — список коробок, каждая: [{"product_id": sku, "quantity": n}].
        Ozon может разделить отправление; в ответе — итоговые номера.
        """
        payload = {
            "posting_number": posting_number,
            "packages": [{"products": products} for products in packages],
            "with": {"additional_data": True},
        }
        data = self.post("/v4/posting/fbs/ship", payload)
        return {
            "postings": list(data.get("result") or []),
            "additional_data": list(data.get("additional_data") or []),
        }

    # ------------------------------------------------------------------ стикеры
    def package_label(self, posting_numbers: list[str]) -> tuple[bytes, str]:
        """PDF со стикерами. Сначала синхронный метод, затем асинхронный."""
        response = self._request("/v2/posting/fbs/package-label", {"posting_number": posting_numbers})
        if response.status_code < 400:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "pdf" in content_type or response.content[:4] == b"%PDF":
                return response.content, "label.pdf"
            try:
                data = response.json()
            except ValueError:
                data = {}
            body = data.get("result") if isinstance(data.get("result"), dict) else data
            content = (body or {}).get("file_content") or (body or {}).get("content")
            if content:
                return base64.b64decode(content), (body or {}).get("file_name") or "label.pdf"
        else:
            message, code = self._extract_error(response)
            log.warning("Синхронный стикер недоступен (%s %s), пробуем асинхронный", response.status_code, message)
        return self._package_label_async(posting_numbers)

    def _package_label_async(self, posting_numbers: list[str]) -> tuple[bytes, str]:
        created = self.post("/v2/posting/fbs/package-label/create", {"posting_number": posting_numbers})
        tasks = ((created.get("result") or {}).get("tasks")) or []
        if not tasks:
            raise OzonError("Ozon не вернул задание на генерацию стикера")
        task_id = tasks[0].get("task_id")
        deadline = time.time() + 60
        while time.time() < deadline:
            state = self.post("/v1/posting/fbs/package-label/get", {"task_id": task_id}).get("result") or {}
            status = (state.get("status") or "").lower()
            if status in {"completed", "ready", "success"} and state.get("file_url"):
                file_response = self._client.get(state["file_url"], timeout=60)
                file_response.raise_for_status()
                return file_response.content, "label.pdf"
            if status in {"error", "failed"}:
                raise OzonError(f"Ozon не смог сгенерировать стикер: {state.get('error') or 'неизвестная ошибка'}")
            time.sleep(2)
        raise OzonError("Истекло время ожидания генерации стикера")

    # ------------------------------------------------------------------ товары
    def product_info(self, skus: list[str] | None = None, offer_ids: list[str] | None = None) -> list[dict]:
        payload: dict[str, Any] = {}
        if skus:
            payload["sku"] = [int(s) for s in skus if str(s).isdigit()]
        if offer_ids:
            payload["offer_id"] = list(offer_ids)
        if not payload:
            return []
        data = self.post("/v3/product/info/list", payload)
        items = data.get("items")
        if items is None:
            items = (data.get("result") or {}).get("items") or []
        return list(items)

    # ------------------------------------------------------------------ возвраты
    def returns_list(self, *, limit: int = 500, last_id: int = 0, filter_: dict | None = None) -> tuple[list[dict], bool]:
        # В /v1/returns/list допускается только один фильтр за запрос.
        payload: dict[str, Any] = {"limit": limit, "last_id": last_id}
        if filter_:
            payload["filter"] = filter_
        data = self.post("/v1/returns/list", payload)
        returns = data.get("returns")
        if returns is None:
            returns = (data.get("result") or {}).get("returns") or []
        return list(returns), bool(data.get("has_next"))

    def returns_fbs_points(self, *, limit: int = 100, last_id: int = 0) -> list[dict]:
        """Пункты выдачи с количеством ожидающих возвратов (FBS)."""
        data = self.post("/v1/returns/company/fbs/info", {"filter": {}, "pagination": {"limit": limit, "last_id": last_id}})
        return list(data.get("drop_off_points") or [])

    def giveout_pdf(self) -> bytes:
        """Штрихкод/акт на получение возвратов (одна активная выдача на компанию)."""
        response = self._request("/v1/return/giveout/get-pdf", {})
        if response.status_code >= 400:
            message, code = self._extract_error(response)
            raise OzonError(message, status=response.status_code, code=code)
        if response.content[:4] == b"%PDF":
            return response.content
        try:
            data = response.json()
        except ValueError:
            return response.content
        body = data.get("result") if isinstance(data.get("result"), dict) else data
        content = (body or {}).get("file_content") or (body or {}).get("content")
        if content:
            return base64.b64decode(content)
        raise OzonError("Ozon не вернул PDF выдачи возвратов")

    def ping(self) -> dict:
        """Проверка ключей: лёгкий запрос списка отправлений."""
        now = datetime.now(timezone.utc)
        self.posting_list(None, now - timedelta(days=1), now, limit=1)
        return {"ok": True}

    def close(self) -> None:
        self._client.close()


# ====================================================================== демо-режим
DEMO_PRODUCTS = [
    ("1234567890", "ART-001", "Кофе зерновой Arabica 1 кг", "4600000000017"),
    ("1234567891", "ART-002", "Чайник электрический 1.7 л", "4600000000024"),
    ("1234567892", "ART-003", "Наушники TWS Pro", "4600000000031"),
    ("1234567893", "ART-004", "Пылесос вертикальный", "4600000000048"),
    ("1234567894", "ART-005", "Термокружка 500 мл", "4600000000055"),
    ("1234567895", "ART-006", "Гель для стирки 2 л", "4600000000062"),
    ("1234567896", "ART-007", "Лампа настольная LED", "4600000000079"),
    ("1234567897", "ART-008", "Рюкзак городской 20 л", "4600000000086"),
]

DEMO_CITIES = [
    ("Москва", "Москва"),
    ("Санкт-Петербург", "Санкт-Петербург"),
    ("Свердловская область", "Екатеринбург"),
    ("Татарстан", "Казань"),
    ("Новосибирская область", "Новосибирск"),
]


class DemoOzonClient(OzonClient):
    """Полностью автономная подделка API — для настройки рабочего места без ключей."""

    def __init__(self) -> None:  # noqa: D107 - без сетевого клиента
        self.client_id = "demo"
        self.api_key = "demo"
        self.base_url = "demo://ozon"
        self._lock = threading.Lock()
        self._rnd = random.Random(20240501)
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
        number = f"{48000000 + index * 7}-{1000 + index}-1"
        status = "awaiting_packaging" if index % 3 == 0 else "awaiting_deliver"
        positions = rnd.choice([1, 1, 1, 2, 2, 3])
        chosen = rnd.sample(DEMO_PRODUCTS, positions)
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
        region, city = rnd.choice(DEMO_CITIES)
        shipment = now + timedelta(hours=rnd.choice([3, 8, 20, 30, 44]))
        posting = {
            "posting_number": number,
            "order_id": 700000000 + index,
            "order_number": f"{48000000 + index * 7}-{1000 + index}",
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
        sku, offer, name, _bc = rnd.choice(DEMO_PRODUCTS)
        ready = index % 4 != 3
        status = ("ArrivedAtReturnPlace" if ready else "MovingToSeller")
        display = "Готов к выдаче" if ready else "Едет к продавцу"
        arrived = now - timedelta(days=rnd.randrange(0, 12))
        return {
            "id": 90000000 + index,
            "company_id": 1,
            "return_reason_name": rnd.choice(
                ["Не подошёл размер", "Товар повреждён", "Не соответствует описанию", "Передумал"]
            ),
            "type": rnd.choice(["FBO", "FBS"]),
            "schema": rnd.choice(["FBO", "FBS"]),
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
        from .pdfgen import make_label_pdf

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
        return make_label_pdf(pages), "label-demo.pdf"

    def product_info(self, skus=None, offer_ids=None):  # type: ignore[override]
        wanted_sku = {str(s) for s in (skus or [])}
        wanted_offer = {str(o) for o in (offer_ids or [])}
        items = []
        for sku, offer, name, barcode in DEMO_PRODUCTS:
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
        from .pdfgen import make_giveout_pdf

        return make_giveout_pdf("DEMO-GIVEOUT-0001")

    def ping(self):  # type: ignore[override]
        return {"ok": True, "demo": True}

    def close(self):  # type: ignore[override]
        return None


_client: OzonClient | None = None
_client_lock = threading.Lock()


def get_client() -> OzonClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = DemoOzonClient() if settings.demo else OzonClient()
        return _client


def reset_client() -> None:
    """Пересоздать клиент после смены ключей."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None
