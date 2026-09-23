"""Клиент Partner API Яндекс Маркета.

Пути методов сверены с присланной документацией:
  POST /v1/businesses/{businessId}/orders        — заказы кабинета
  POST /v2/businesses/{businessId}/offer-mappings — каталог товаров со штрихкодами
  POST /v2/reports/documents/labels/generate     — ярлыки пачкой (до 1000 заказов)
  GET  /v2/reports/info/{reportId}               — готов ли файл и где его взять
  GET  /v2/campaigns/{campaignId}/orders/{orderId}/delivery/labels — ярлык одного заказа

Авторизация — заголовок `Api-Key: <токен>`; идентификатор кабинета (businessId)
идёт в пути и в теле, а идентификатор магазина (campaignId) панель не
спрашивает: он приходит в каждом заказе.

Ярлыки берём массовым методом, а не поштучным. Причин три: он принимает
businessId, который у нас и так есть, забирает до 1000 заказов за раз и, в
отличие от поштучного, доступен с правом только на чтение.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import httpx

from ...core.config import settings
from ..base import KeyCheckError, MarketError

log = logging.getLogger("yandex")

RETRY_STATUSES = {420, 429, 500, 502, 503, 504}
MAX_RETRIES = 4
# Столько заказов Маркет отдаёт за одну страницу — больше он не примет.
PAGE_LIMIT = 50
# И столько товаров за страницу каталога: предел метода offer-mappings.
CATALOG_PAGE_LIMIT = 100
# Столько заказов принимает массовый запрос ярлыков.
LABELS_MAX_ORDERS = 1000
# Ярлык 75×120 мм — тот же размер, что панель печатает для Ozon.
LABEL_FORMAT = "A7"

# Статус и этап обработки, по которым заказ попадает на склад.
STATUS_PROCESSING = "PROCESSING"
SUBSTATUS_STARTED = "STARTED"            # подтверждён, можно собирать
SUBSTATUS_READY_TO_SHIP = "READY_TO_SHIP"  # собран и ждёт отгрузки

# Что панель вообще загружает. Остальные статусы сборщику не нужны: заказ уже
# уехал или отменён, и лишняя строка на складе — лишняя ошибка.
WORK_SUBSTATUSES = (SUBSTATUS_STARTED, SUBSTATUS_READY_TO_SHIP)

SUBSTATUS_LABELS = {
    SUBSTATUS_STARTED: "Ожидает сборки",
    SUBSTATUS_READY_TO_SHIP: "Ожидает отгрузки",
}

DELIVERY_LABELS = {
    "DELIVERY": "Курьером",
    "PICKUP": "Самовывоз",
    "POST": "Почта",
    "DIGITAL": "Цифровой товар",
    "UNKNOWN": "—",
}


class YandexError(MarketError):
    """Ошибка обращения к Partner API Яндекс Маркета."""

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


class YandexClient:
    """Синхронный клиент: приложение работает в пуле потоков, async не нужен."""

    def __init__(
        self,
        business_id: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        max_retries: int = MAX_RETRIES,
        timeout: int | None = None,
    ):
        self.business_id = str(business_id or "").strip()
        self.api_key = api_key or ""
        self.base_url = (base_url or settings.yandex_base_url).rstrip("/")
        self.max_retries = max(1, max_retries)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout or settings.yandex_timeout),
            headers={
                "Api-Key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    # ------------------------------------------------------------ низкий уровень
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:  # сеть/таймаут
                last_error = exc
                time.sleep(min(2 ** attempt, 10))
                continue
            if response.status_code in RETRY_STATUSES and attempt < self.max_retries - 1:
                delay = float(response.headers.get("Retry-After") or min(2 ** attempt, 10))
                log.warning("Маркет %s -> %s, повтор через %.0fс", path, response.status_code, delay)
                time.sleep(delay)
                continue
            return response
        raise YandexError(f"Сеть недоступна: {last_error}")

    def request_json(self, method: str, path: str, payload: dict | None = None,
                     params: dict | None = None) -> dict:
        kwargs: dict[str, Any] = {}
        if payload is not None:
            kwargs["content"] = json.dumps(payload, ensure_ascii=False).encode()
        if params:
            kwargs["params"] = params
        response = self._request(method, path, **kwargs)
        if response.status_code >= 400:
            message, code = self._extract_error(response)
            raise YandexError(message, status=response.status_code, code=code, payload=response.text[:2000])
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise YandexError(f"Некорректный ответ Маркета: {exc}", status=response.status_code) from exc

    @staticmethod
    def _extract_error(response: httpx.Response) -> tuple[str, str | None]:
        """Ошибка Маркета приходит списком в `errors` — берём первую понятную."""
        try:
            data = response.json()
        except ValueError:
            return response.text[:300] or f"HTTP {response.status_code}", None
        errors = data.get("errors") or []
        if errors:
            first = errors[0] or {}
            return (first.get("message") or first.get("code") or "Ошибка Маркета"), (first.get("code") or None)
        return json.dumps(data, ensure_ascii=False)[:300], None

    # ------------------------------------------------------------------- заказы
    def orders(self, *, substatuses: list[str] | None = None, page_token: str | None = None,
               limit: int = PAGE_LIMIT) -> tuple[list[dict], str | None]:
        """Страница заказов кабинета и токен следующей страницы.

        Фильтр по статусу уходит в запрос, но полагаться на него нельзя: разбор
        всё равно отсеивает чужое у себя — одной перемены в API хватит, чтобы
        сборщик увидел лишнее.
        """
        if not self.business_id:
            raise YandexError("Не задан идентификатор кабинета (businessId)")
        payload: dict[str, Any] = {"statuses": [STATUS_PROCESSING]}
        if substatuses:
            payload["substatuses"] = list(substatuses)
        params: dict[str, Any] = {"limit": min(limit, PAGE_LIMIT)}
        if page_token:
            params["pageToken"] = page_token
        data = self.request_json(
            "POST", f"/v1/businesses/{self.business_id}/orders", payload=payload, params=params
        )
        orders = list(data.get("orders") or [])
        next_token = ((data.get("paging") or {}).get("nextPageToken")) or None
        return orders, next_token

    # ------------------------------------------------------------------ каталог
    def offer_mappings(self, *, page_token: str | None = None, limit: int = CATALOG_PAGE_LIMIT,
                       offer_ids: list[str] | None = None,
                       archived: bool | None = None) -> tuple[list[dict], str | None]:
        """Страница каталога кабинета и токен следующей страницы.

        Один метод отдаёт сразу всё, что нужно панели: артикул продавца, имя,
        картинку и штрихкоды. Второго запроса за карточками, как у Ozon, тут не
        требуется.

        Список по конкретным артикулам Маркет отдаёт целиком: с ним нельзя
        передавать ни страницы, ни фильтры — поэтому ветка отдельная.
        """
        if not self.business_id:
            raise YandexError("Не задан идентификатор кабинета (businessId)")
        payload: dict[str, Any] = {}
        params: dict[str, Any] = {}
        if offer_ids:
            payload["offerIds"] = [str(offer) for offer in offer_ids][:CATALOG_PAGE_LIMIT]
        else:
            if archived is not None:
                payload["archived"] = bool(archived)
            params["limit"] = max(1, min(limit, CATALOG_PAGE_LIMIT))
            if page_token:
                params["pageToken"] = page_token
        data = self.request_json(
            "POST", f"/v2/businesses/{self.business_id}/offer-mappings",
            payload=payload, params=params,
        )
        result = data.get("result") or {}
        mappings = [item for item in (result.get("offerMappings") or []) if isinstance(item, dict)]
        next_token = ((result.get("paging") or {}).get("nextPageToken")) or None
        return mappings, next_token

    # ------------------------------------------------------------------ ярлыки
    def labels_task(self, order_ids: list[int | str], *, sorted_as_given: bool = True) -> str:
        """Запустить сборку PDF с ярлыками. Возвращает идентификатор отчёта."""
        if not order_ids:
            raise YandexError("Не передано ни одного заказа")
        payload = {
            "businessId": int(self.business_id),
            "orderIds": [int(i) for i in order_ids][:LABELS_MAX_ORDERS],
            "sortingType": "SORT_BY_GIVEN_ORDER" if sorted_as_given else "SORT_BY_ORDER_CREATED_AT",
        }
        data = self.request_json(
            "POST", "/v2/reports/documents/labels/generate",
            payload=payload, params={"format": LABEL_FORMAT},
        )
        report_id = ((data.get("result") or {}).get("reportId")) or ""
        if not report_id:
            raise YandexError("Маркет не вернул идентификатор файла с ярлыками")
        return str(report_id)

    def report_info(self, report_id: str) -> dict:
        """Состояние сборки файла: готов ли и где его взять.

        Имена полей у этого метода в присланной документации не расписаны,
        поэтому разбор терпим к нескольким написаниям — иначе панель молча
        ждала бы файл, который давно готов.
        """
        data = self.request_json("GET", f"/v2/reports/info/{report_id}")
        result = data.get("result") or {}
        status = str(result.get("status") or result.get("state") or "").upper()
        link = result.get("file") or result.get("url") or result.get("link") or ""
        return {
            "status": status,
            "sub_status": str(result.get("subStatus") or ""),
            "link": str(link),
            "raw": result,
        }

    def labels_pdf(self, order_ids: list[int | str], *, wait: int = 120) -> tuple[bytes, str]:
        """Готовый PDF с ярлыками на переданные заказы.

        Маркет собирает файл не сразу, поэтому ждём: запустили — опрашиваем —
        скачиваем. Ровно так же устроена асинхронная печать стикеров у Ozon.
        """
        report_id = self.labels_task(order_ids)
        deadline = time.time() + wait
        last = ""
        while time.time() < deadline:
            info = self.report_info(report_id)
            last = info["status"] or last
            if info["link"]:
                return self.download(info["link"]), "yandex-labels.pdf"
            if info["status"] in ("FAILED", "ERROR"):
                raise YandexError(
                    f"Маркет не собрал ярлыки: {info['sub_status'] or info['status']}"
                )
            time.sleep(2)
        raise YandexError(f"Истекло время ожидания ярлыков Маркета (последний статус {last or '—'})")

    def download(self, url: str) -> bytes:
        """Скачать готовый файл по ссылке из отчёта."""
        response = self._request("GET", url)
        if response.status_code >= 400:
            message, code = self._extract_error(response)
            raise YandexError(message, status=response.status_code, code=code)
        if not response.content:
            raise YandexError("Маркет отдал пустой файл с ярлыками")
        return response.content

    def order_labels(self, campaign_id: int | str, order_id: int | str) -> bytes:
        """Ярлыки одного заказа. Нужен полный доступ, поэтому путь запасной."""
        response = self._request(
            "GET", f"/v2/campaigns/{campaign_id}/orders/{order_id}/delivery/labels",
            params={"format": LABEL_FORMAT},
        )
        if response.status_code >= 400:
            message, code = self._extract_error(response)
            raise YandexError(message, status=response.status_code, code=code)
        return response.content

    # ------------------------------------------------------------------ проверка
    def ping(self) -> dict:
        """Ключи рабочие, если кабинет отдаёт свои заказы."""
        orders, _token = self.orders(limit=1)
        return {"orders": len(orders), "business_id": self.business_id}

    def close(self) -> None:
        self._client.close()


_clients: dict[int, YandexClient] = {}
_client_lock = threading.Lock()


def get_client(account: dict | None = None) -> YandexClient:
    """Клиент кабинета по его ключам. Кэшируется, пока ключи не поменяли."""
    from ...core import accounts

    if account is None:
        raise YandexError("Не выбран кабинет Яндекс Маркета")
    business_id, api_key, _source = accounts.credentials(account)
    if not (business_id and api_key):
        raise YandexError(
            f"У кабинета «{account.get('title')}» не заданы ключи Маркета — внесите их в «Настройках»"
        )
    account_id = int(account.get("id") or 0)
    with _client_lock:
        client = _clients.get(account_id)
        if client is None or client.business_id != business_id.strip() or client.api_key != api_key:
            client = YandexClient(business_id, api_key)
            _clients[account_id] = client
        return client


def reset_client(account_id: int | None = None) -> None:
    """Забыть клиент кабинета (или всех) — после смены ключей или удаления."""
    with _client_lock:
        if account_id is None:
            _clients.clear()
        else:
            _clients.pop(int(account_id), None)


def probe(business_id: str, api_key: str) -> None:
    """Проверить ключи до сохранения — без повторов, с коротким таймаутом."""
    check = YandexClient(business_id=business_id, api_key=api_key, max_retries=1, timeout=20)
    try:
        check.ping()
    except YandexError as exc:
        if exc.status in (401, 403):
            detail = (
                f"Маркет отклонил ключи: {exc.message}. Проверьте businessId и токен "
                "(нужен доступ «Обработка заказов и учёт товаров»)."
            )
        elif exc.status is None:
            detail = (
                f"Не удалось связаться с Маркетом: {exc.message}. Проверьте доступ в интернет с сервера; "
                "если он есть, сохраните ключи без проверки."
            )
        else:
            detail = f"Маркет ответил ошибкой: {exc.message}"
        raise KeyCheckError(detail) from exc
    finally:
        check.close()
