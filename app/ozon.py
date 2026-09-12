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
  POST /v1/return/giveout/get-pdf      — акт/штрихкод на выдачу возвратов
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

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

    def __init__(
        self,
        client_id: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        max_retries: int = MAX_RETRIES,
        timeout: int | None = None,
    ):
        self.client_id = client_id or settings.ozon_client_id
        self.api_key = api_key or settings.ozon_api_key
        self.base_url = (base_url or settings.ozon_base_url).rstrip("/")
        self.max_retries = max(1, max_retries)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout or settings.ozon_timeout),
            headers={
                "Client-Id": self.client_id,
                "Api-Key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    # ------------------------------------------------------------------ низкий уровень
    def _request(self, path: str, payload: dict | None = None) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self._client.post(path, content=json.dumps(payload or {}, ensure_ascii=False).encode())
            except httpx.HTTPError as exc:  # сеть/таймаут
                last_error = exc
                time.sleep(min(2 ** attempt, 10))
                continue
            if response.status_code in RETRY_STATUSES and attempt < self.max_retries - 1:
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

    def _same_host_url(self, raw: str) -> str:
        """Адрес готового стикера из ответа Ozon — только на тот же хост.

        По этому адресу панель ходит сама, со своего сервера. Если бы в file_url
        пришёл чужой адрес, запрос ушёл бы туда же — в том числе на внутренний
        адрес сети, куда снаружи хода нет. Относительный путь пропускаем: его
        httpx достроит до base_url.
        """
        parts = urlsplit(raw)
        if not parts.scheme and not parts.netloc:
            return raw
        expected = urlsplit(self.base_url)
        if (parts.scheme, parts.netloc) != (expected.scheme, expected.netloc):
            raise OzonError(f"Ozon прислал стикер по чужому адресу: {parts.scheme}://{parts.netloc}")
        return raw

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
                file_url = self._same_host_url(state["file_url"])
                file_response = self._client.get(file_url, timeout=60)
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



_clients: dict[int, OzonClient] = {}
_client_lock = threading.Lock()


def get_client(account: dict | None = None) -> OzonClient:
    """Клиент кабинета по его ключам. Кэшируется, пока ключи не поменяли."""
    from . import accounts

    if account is None:
        account = accounts.default_account()
    if account is None:
        raise OzonError("Не добавлен ни один кабинет Ozon")
    client_id, api_key, _source = accounts.credentials(account)
    if not (client_id and api_key):
        raise OzonError(
            f"У кабинета «{account.get('title')}» не заданы ключи Ozon — внесите их в «Настройках»"
        )
    account_id = int(account.get("id") or 0)
    with _client_lock:
        client = _clients.get(account_id)
        if client is None:
            client = OzonClient(client_id=client_id, api_key=api_key)
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
