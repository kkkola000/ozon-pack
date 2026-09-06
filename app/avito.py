"""Клиент Avito API для бизнеса (раздел «Управление заказами»).

Пути сверены с публичной схемой Avito Business API (openapi 3.0, api.avito.ru):
  POST /token                                          — access token, 24 часа
  GET  /core/v1/accounts/self                          — кто мы (проверка ключей)
  GET  /order-management/1/orders                      — список заказов
  POST /order-management/1/order/applyTransition       — confirm / perform / reject
  POST /order-management/1/orders/labels               — задача на генерацию этикеток
  GET  /order-management/1/orders/labels/{id}/download — готовый PDF

Авторизация — client_credentials: client_id и client_secret из личного кабинета
меняются на токен, который живёт сутки. Токен кэшируется в памяти клиента.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("avito")

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4

# Статусы заказов Avito (см. раздел «Получение заказов»).
STATUS_ON_CONFIRMATION = "on_confirmation"
STATUS_READY_TO_SHIP = "ready_to_ship"
STATUS_IN_TRANSIT = "in_transit"
STATUS_CANCELED = "canceled"
STATUS_DELIVERED = "delivered"
STATUS_ON_RETURN = "on_return"
STATUS_IN_DISPUTE = "in_dispute"
STATUS_CLOSED = "closed"

# Сборщика касаются только два: подтвердить и отправить.
WORK_STATUSES = (STATUS_ON_CONFIRMATION, STATUS_READY_TO_SHIP)
# Плюс возвраты: заказ на обратном пути, его надо забрать из пункта выдачи.
RETURN_STATUSES = (STATUS_ON_RETURN,)
# Всё, что панель вообще запрашивает у Avito.
SYNC_STATUSES = WORK_STATUSES + RETURN_STATUSES

STATUS_LABELS = {
    STATUS_ON_CONFIRMATION: "Подтвердите заказ",
    STATUS_READY_TO_SHIP: "Отправьте заказ",
    STATUS_IN_TRANSIT: "В пути",
    STATUS_CANCELED: "Отменён",
    STATUS_DELIVERED: "Доставлен",
    STATUS_ON_RETURN: "На возврате",
    STATUS_IN_DISPUTE: "Спор",
    STATUS_CLOSED: "Закрыт",
}

# returnPolicy.returnStatus из модели заказа.
#
# Внимание: в схеме Avito это значение описано как ready_to_pickup, а боевой
# API отдаёт ready_for_pickup. Расхождение настоящее, поэтому принимаем оба
# написания — иначе возвраты, которые можно забрать, молча отбрасываются.
RETURN_READY = "ready_to_pickup"
RETURN_READY_ALT = "ready_for_pickup"
RETURN_READY_VALUES = frozenset({RETURN_READY, RETURN_READY_ALT})
RETURN_IN_TRANSIT = "in_transit"
RETURN_SELF = "self_return"

RETURN_STATUS_LABELS = {
    RETURN_READY: "Заберите заказ",
    RETURN_READY_ALT: "Заберите заказ",
    RETURN_IN_TRANSIT: "Возврат в пути",
    RETURN_SELF: "Возврат забираете сами",
}


def is_ready_for_pickup(return_status: str | None) -> bool:
    """Возврат доехал до пункта выдачи и его можно забрать."""
    return (return_status or "") in RETURN_READY_VALUES

SERVICE_LABELS = {
    "pvz": "ПВЗ",
    "dbs": "Доставка партнёром продавца",
    "rdbs": "Курьер продавца",
    "courier": "Курьер Яндекса",
    "cnc": "Самовывоз",
    "postamat": "Постамат",
}

# Переходы, которые делает панель. Остальные действия Avito (отмена, маркировка,
# трек-номер, интервалы курьера) в интерфейс не выводятся — сборщику они не нужны.
TRANSITION_CONFIRM = "confirm"
TRANSITION_PERFORM = "perform"


class AvitoError(RuntimeError):
    """Ошибка обращения к Avito API."""

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


class AvitoClient:
    """Синхронный клиент: приложение работает в пуле потоков, async не нужен."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        base_url: str | None = None,
        *,
        max_retries: int = MAX_RETRIES,
        timeout: int | None = None,
    ):
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self.base_url = (base_url or settings.avito_base_url).rstrip("/")
        self.max_retries = max(1, max_retries)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout or settings.avito_timeout),
            headers={"Accept": "application/json"},
        )
        self._lock = threading.Lock()
        self._token = ""
        self._token_until = 0.0

    # ------------------------------------------------------------------ авторизация
    def token(self, *, force: bool = False) -> str:
        """Access token с запасом по времени: за минуту до конца берём новый."""
        with self._lock:
            if not force and self._token and time.time() < self._token_until:
                return self._token
            response = self._client.post(
                "/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code >= 400:
                message, code = self._extract_error(response)
                raise AvitoError(f"Avito не выдал токен: {message}", status=response.status_code, code=code)
            try:
                data = response.json()
            except ValueError as exc:
                raise AvitoError(f"Некорректный ответ Avito на запрос токена: {exc}") from exc
            token = data.get("access_token")
            if not token:
                raise AvitoError("В ответе Avito нет access_token")
            self._token = token
            self._token_until = time.time() + max(60, float(data.get("expires_in") or 86400)) - 60
            return self._token

    # ------------------------------------------------------------------ низкий уровень
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        retried_auth = False
        for attempt in range(self.max_retries):
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {self.token()}"
            try:
                response = self._client.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as exc:  # сеть/таймаут
                last_error = exc
                time.sleep(min(2 ** attempt, 10))
                continue
            # Токен живёт сутки, но мог быть отозван — один раз берём новый.
            if response.status_code == 401 and not retried_auth:
                retried_auth = True
                self.token(force=True)
                continue
            if response.status_code in RETRY_STATUSES and attempt < self.max_retries - 1:
                delay = float(response.headers.get("Retry-After") or min(2 ** attempt, 10))
                log.warning("Avito %s -> %s, повтор через %.0fс", path, response.status_code, delay)
                time.sleep(delay)
                continue
            return response
        raise AvitoError(f"Сеть недоступна: {last_error}")

    def request_json(self, method: str, path: str, **kwargs: Any) -> dict:
        response = self._request(method, path, **kwargs)
        if response.status_code >= 400:
            message, code = self._extract_error(response)
            raise AvitoError(message, status=response.status_code, code=code, payload=response.text[:2000])
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise AvitoError(f"Некорректный ответ Avito: {exc}", status=response.status_code) from exc
        return data if isinstance(data, dict) else {"result": data}

    @staticmethod
    def _extract_error(response: httpx.Response) -> tuple[str, str | None]:
        try:
            data = response.json()
        except ValueError:
            return response.text[:300] or f"HTTP {response.status_code}", None
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        message = (
            error.get("message")
            or data.get("message")
            or data.get("error_description")
            or (data.get("error") if isinstance(data.get("error"), str) else None)
            or str(data)[:300]
        )
        code = str(error.get("code") or data.get("code") or "") or None
        return message, code

    # ------------------------------------------------------------------ заказы
    def orders(
        self,
        *,
        statuses: list[str] | None = None,
        ids: list[str] | None = None,
        date_from: datetime | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> tuple[list[dict], bool]:
        """Страница заказов. limit у Avito ограничен двадцатью."""
        params: list[tuple[str, Any]] = [("page", page), ("limit", min(max(limit, 1), 20))]
        for status in statuses or []:
            params.append(("statuses", status))
        for order_id in ids or []:
            params.append(("ids", str(order_id)))
        if date_from is not None:
            params.append(("dateFrom", int(date_from.timestamp())))
        data = self.request_json("GET", "/order-management/1/orders", params=params)
        return list(data.get("orders") or []), bool(data.get("hasMore"))

    def orders_all(
        self,
        *,
        statuses: list[str] | None = None,
        date_from: datetime | None = None,
        max_pages: int = 50,
    ) -> list[dict]:
        collected: list[dict] = []
        for page in range(1, max_pages + 1):
            chunk, has_more = self.orders(statuses=statuses, date_from=date_from, page=page, limit=20)
            collected.extend(chunk)
            if not has_more or not chunk:
                break
        return collected

    def order(self, order_id: str) -> dict | None:
        """Один заказ по идентификатору — чтобы перечитать его после действия."""
        found, _has_more = self.orders(ids=[str(order_id)], limit=1)
        return found[0] if found else None

    def apply_transition(self, order_id: str, transition: str) -> bool:
        """confirm — подтвердить заказ, perform — подтвердить отправку (RDBS)."""
        data = self.request_json(
            "POST",
            "/order-management/1/order/applyTransition",
            json={"orderId": str(order_id), "transition": transition},
        )
        success = data.get("success")
        # Пустой ответ Avito на успешный переход тоже встречается.
        return True if success is None else bool(success)

    def label_task(self, marketplace_ids: list[str]) -> str:
        """Задача на генерацию этикеток. Avito ждёт номера из сервиса сделок."""
        data = self.request_json(
            "POST",
            "/order-management/1/orders/labels",
            json={"orderIDs": [str(i) for i in marketplace_ids]},
        )
        task_id = data.get("taskID") or data.get("taskId")
        if not task_id:
            raise AvitoError("Avito не вернул задание на генерацию этикетки")
        return str(task_id)

    def label_pdf(self, marketplace_ids: list[str], *, wait: int = 60) -> tuple[bytes, str]:
        """Оригинальный PDF-файл этикетки от Avito, без нашего вмешательства."""
        task_id = self.label_task(marketplace_ids)
        deadline = time.time() + wait
        last_status = 0
        while time.time() < deadline:
            response = self._request("GET", f"/order-management/1/orders/labels/{task_id}/download")
            if response.status_code < 400 and response.content[:4] == b"%PDF":
                return response.content, "avito-label.pdf"
            if response.status_code < 400 and response.content:
                # Файл ещё не готов — Avito отвечает JSON-описанием задачи.
                time.sleep(2)
                continue
            if response.status_code in (404, 425, 202):
                last_status = response.status_code
                time.sleep(2)
                continue
            message, code = self._extract_error(response)
            raise AvitoError(message, status=response.status_code, code=code)
        raise AvitoError(f"Истекло время ожидания этикетки Avito (последний ответ {last_status or '—'})")

    def self_info(self) -> dict:
        return self.request_json("GET", "/core/v1/accounts/self")

    def ping(self) -> dict:
        info = self.self_info()
        return {"ok": True, "id": info.get("id"), "name": info.get("name"), "email": info.get("email")}

    def close(self) -> None:
        self._client.close()



_clients: dict[int, AvitoClient] = {}
_client_lock = threading.Lock()


def get_client(account: dict | None = None) -> AvitoClient:
    """Клиент кабинета по его ключам. Кэшируется, пока ключи не поменяли."""
    from . import accounts

    if account is None:
        raise AvitoError("Не выбран кабинет Avito")
    client_id, secret, _source = accounts.credentials(account)
    if not (client_id and secret):
        raise AvitoError(
            f"У кабинета «{account.get('title')}» не заданы ключи Avito — внесите их в «Настройках»"
        )
    account_id = int(account.get("id") or 0)
    with _client_lock:
        client = _clients.get(account_id)
        if client is None:
            client = AvitoClient(client_id=client_id, client_secret=secret)
            _clients[account_id] = client
        return client


def reset_client(account_id: int | None = None) -> None:
    """Пересоздать клиент после смены ключей (или все сразу)."""
    with _client_lock:
        targets = [account_id] if account_id is not None else list(_clients)
        for key in targets:
            client = _clients.pop(key, None)
            if client is not None:
                client.close()
