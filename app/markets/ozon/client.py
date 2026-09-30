"""Клиент Ozon Seller API.

Пути методов сверены с документацией Seller API:
  POST /v4/posting/fbs/list            — список FBS-отправлений (страницы по курсору)
  POST /v3/posting/fbs/get             — одно отправление
  POST /v4/posting/fbs/ship            — сборка отправления (в «Ожидает отгрузки»)
  POST /v3/posting/fbs/package-label/create — задание на стикеры отправлений
  POST /v2/posting/fbs/package-label/get    — готово ли задание и ссылка на PDF
  POST /v2/posting/fbs/get-by-barcode  — отправление по штрихкоду стикера: из barcodes
                                         старой этикетки или scanit новой
  POST /v3/product/list                — каталог кабинета (артикулы, архив)
  POST /v3/product/info/list           — карточки товаров (штрихкоды, фото)
  POST /v1/returns/list                — возвраты FBO и FBS
  POST /v1/return/giveout/get-pdf      — штрихкод на выдачу возвратов в пункте

Стикеры — только через задание: синхронный /v2/posting/fbs/package-label
Ozon отключает 2 ноября 2026 года, а /v2/.../create и /v1/.../get заменены
на /v3/.../create и /v2/.../get.

У новой этикетки FBS свой штрихкод — поле scanit отправления в
/v4/posting/fbs/list и /v3/posting/fbs/get. Его и читает сканер; панель
хранит его рядом с верхним и нижним штрихкодами старой этикетки.
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

from ...core.config import settings
from ..base import KeyCheckError, MarketError

log = logging.getLogger("ozon")

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4
# Столько отправлений просим за страницу /v4/posting/fbs/list — как в примере
# Ozon; дальше листаем курсором.
POSTINGS_PAGE_LIMIT = 100
# Сколько ждать готовности задания на стикеры и как часто спрашивать.
LABEL_WAIT = 60
LABEL_POLL = 2
# Откуда разрешено скачивать готовый стикер: сам Seller API или домены Ozon.
# Ссылку присылает Ozon, но ходит по ней наш сервер — поэтому не куда угодно.
LABEL_HOSTS = ("ozon.ru", "ozone.ru")
# Какое задание брать, если Ozon вернул несколько (обычная и маленькая
# этикетка): по размеру ленты для стикеров Ozon на странице «Принтеры».
SMALL_LABEL_SIZES = ("58x40",)


class OzonError(MarketError):
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


# Стикер, который панель просит у Ozon: задание на стикеры Ozon делает на обе
# этикетки, маленькую и обычную, — выбор на «Принтерах».
LABEL_FORMATS = (("small", "Маленький 58×40"), ("big", "Большой 75×120"))


def label_size() -> str | None:
    """Бумага для стикеров Ozon со страницы «Принтеры» — первой строки."""
    from ...core import printers

    return printers.size_of("ozon:label")


def default_label_format() -> str:
    """Пока стикер не выбран — как раньше: под ленту 58×40 маленький, иначе обычный."""
    return "small" if label_size() in SMALL_LABEL_SIZES else "big"


def label_format() -> str:
    """Какой стикер просить у Ozon: выбранный на «Принтерах»."""
    from ...core import printers

    return printers.label_format("ozon:label") or default_label_format()


def iso_moment(dt: datetime) -> str:
    """Момент в том виде, в каком его ждёт Ozon: до миллисекунд и с Z."""
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
        statuses: list[str] | None,
        since: datetime,
        to: datetime,
        *,
        limit: int = POSTINGS_PAGE_LIMIT,
        cursor: str = "",
    ) -> tuple[list[dict], str, bool]:
        """Страница отправлений: (отправления, курсор следующей страницы, есть ли ещё).

        /v4/posting/fbs/list листается курсором, а статусы принимает списком —
        оба рабочих статуса приходят одним обходом.
        """
        payload: dict[str, Any] = {
            "sort_dir": "asc",
            "filter": {"since": iso_moment(since), "to": iso_moment(to)},
            "limit": max(1, min(limit, POSTINGS_PAGE_LIMIT)),
            "cursor": cursor or "",
            "with": {"analytics_data": True, "barcodes": True},
        }
        if statuses:
            payload["filter"]["statuses"] = list(statuses)
        data = self.post("/v4/posting/fbs/list", payload)
        body = data.get("result") if isinstance(data.get("result"), dict) else data
        return (
            [posting for posting in (body.get("postings") or []) if isinstance(posting, dict)],
            str(body.get("cursor") or ""),
            bool(body.get("has_next")),
        )

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
        """PDF со стикерами для печати — все запрошенные или ошибка.

        Напечатать половину нельзя: сборщик наклеит, что вышло, и не заметит,
        что одного стикера нет. Поэтому любое отправление без стикера — ошибка
        с его номером и причиной от Ozon.
        """
        pdf, missing = self.package_label_batch(posting_numbers)
        if missing:
            number, reason = next(iter(missing.items()))
            more = f" (и ещё {len(missing) - 1})" if len(missing) > 1 else ""
            raise OzonError(f"Стикер {number}{more}: {reason}")
        return pdf, "label.pdf"

    def package_label_batch(self, posting_numbers: list[str]) -> tuple[bytes | None, dict[str, str]]:
        """Стикеры пачкой — (PDF или None, {номер: почему нет стикера}).

        Для выгрузки до смены: что Ozon отдал, уезжает в архив, а отправления
        без стикера остаются в замке и попадут в следующую выгрузку.
        """
        task_id = self.label_task(posting_numbers)
        return self.label_file(task_id, posting_numbers)

    def label_task(self, posting_numbers: list[str]) -> int:
        """Создать задание на стикеры: /v3/posting/fbs/package-label/create.

        Ozon может вернуть несколько заданий — на обычную и на маленькую
        этикетку. Берём ту, что выбрана на странице «Принтеры» («Стикер от
        Ozon»); не нашлось подходящего — первое.
        """
        if not posting_numbers:
            raise OzonError("Не передано ни одного отправления")
        data = self.post("/v3/posting/fbs/package-label/create", {"posting_numbers": list(posting_numbers)})
        body = data.get("result") if isinstance(data.get("result"), dict) else data
        tasks = [task for task in (body.get("tasks") or []) if isinstance(task, dict) and task.get("task_id")]
        if not tasks:
            raise OzonError("Ozon не вернул задание на стикеры")
        wanted = label_format()
        chosen = next((task for task in tasks if wanted in str(task.get("task_type") or "").lower()), tasks[0])
        return int(chosen["task_id"])

    def label_file(self, task_id: int, posting_numbers: list[str], *,
                   wait: int = LABEL_WAIT) -> tuple[bytes | None, dict[str, str]]:
        """Дождаться задания (/v2/posting/fbs/package-label/get) и скачать PDF.

        Готово — есть file_url. Ошибка задания — error. Отправления, на которые
        стикер не сделан, Ozon называет в status.unprinted_postings с причиной.
        """
        deadline = time.time() + wait
        last = ""
        while True:
            data = self.post("/v2/posting/fbs/package-label/get", {"task_id": int(task_id)})
            body = data.get("result") if isinstance(data.get("result"), dict) else data
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            status = body.get("status") if isinstance(body.get("status"), dict) else {}
            code = str(status.get("code") or "")
            last = code or last
            missing = {
                str(item.get("posting_number")): str(item.get("message") or "Ozon не сделал стикер")
                for item in (status.get("unprinted_postings") or [])
                if isinstance(item, dict) and item.get("posting_number")
            }
            if body.get("file_url"):
                return self._download_label(str(body["file_url"])), missing
            if error.get("code") or error.get("message") or code.lower() in ("error", "failed", "failure"):
                reason = error.get("message") or error.get("code") or code
                if missing and len(missing) >= len(posting_numbers):
                    # Ни одного стикера — причины по отправлениям нагляднее общей.
                    return None, missing
                raise OzonError(f"Ozon не смог сделать стикеры: {reason}", code=str(error.get("code") or "") or None)
            if time.time() >= deadline:
                raise OzonError(f"Истекло время ожидания стикеров Ozon (статус задания: {last or '—'})")
            time.sleep(LABEL_POLL)

    def _label_target(self, raw: str) -> tuple[str, bool]:
        """Куда идти за готовым стикером — (адрес, свой ли это хост).

        По этой ссылке ходит наш сервер, поэтому только Ozon: сам Seller API
        (относительный путь или тот же хост) или https на домене Ozon. Чужой
        адрес, в том числе внутренний адрес сети, отклоняется.
        """
        parts = urlsplit(raw)
        expected = urlsplit(self.base_url)
        host = (parts.hostname or "").lower()
        if not parts.scheme and not parts.netloc:
            return raw, True
        if (parts.scheme, parts.netloc) == (expected.scheme, expected.netloc):
            return raw, True
        if parts.scheme == "https" and any(host == d or host.endswith("." + d) for d in LABEL_HOSTS):
            return raw, False
        raise OzonError(f"Ozon прислал стикер по чужому адресу: {parts.scheme}://{parts.netloc}")

    def _download_label(self, raw: str) -> bytes:
        """Скачать готовый стикер. Ключи кабинета уходят только на сам Seller API."""
        url, own = self._label_target(raw)
        if own:
            response = self._client.get(url, timeout=60)
        else:
            with httpx.Client(timeout=60, follow_redirects=False) as plain:
                response = plain.get(url)
        if response.status_code >= 400:
            raise OzonError(f"Стикер не скачался: HTTP {response.status_code}", status=response.status_code)
        if response.content[:4] != b"%PDF":
            raise OzonError("Ozon прислал вместо стикера не PDF")
        return response.content

    # ------------------------------------------------------------------ товары
    def product_list(self, *, limit: int = 1000, last_id: str = "") -> tuple[list[dict], str, int]:
        """Каталог кабинета: артикулы и признак архива, страницами по last_id.

        Просим всё (visibility=ALL) и отсеиваем архив у себя. Фильтр площадки
        сюда не годится: «видимые» — это товары в продаже и с остатком, а набор
        можно собирать и из того, что временно кончилось.
        """
        payload = {"filter": {"visibility": "ALL"}, "limit": limit, "last_id": last_id}
        data = self.post("/v3/product/list", payload)
        body = data.get("result") if isinstance(data.get("result"), dict) else data
        items = [item for item in (body.get("items") or []) if isinstance(item, dict)]
        # result.total Ozon отключает 23 ноября 2026 — число теперь в total_items.
        total = body.get("total_items") if body.get("total_items") is not None else body.get("total")
        return items, str(body.get("last_id") or ""), int(total or 0)

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
    from ...core import accounts

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


def probe(client_id: str, api_key: str) -> None:
    """Проверить ключи до сохранения: опечатка не должна оставить склад без данных.

    Без повторов и с коротким таймаутом: оператор ждёт ответа здесь и сейчас.
    """
    check = OzonClient(client_id=client_id, api_key=api_key, max_retries=1, timeout=20)
    try:
        check.ping()
    except OzonError as exc:
        if exc.status in (401, 403):
            detail = f"Ozon отклонил ключи: {exc.message}. Проверьте Client-Id и Api-Key в личном кабинете."
        elif exc.status is None:
            detail = (
                f"Не удалось связаться с Ozon: {exc.message}. Проверьте доступ в интернет с сервера; "
                "если он есть, сохраните ключи без проверки."
            )
        else:
            detail = f"Ozon ответил ошибкой: {exc.message}"
        raise KeyCheckError(detail) from exc
    finally:
        check.close()
