"""Фоновая синхронизация: отправления, товары и возвраты Ozon, заказы Avito.

Синхронизация идёт по всем включённым кабинетам: у каждого свои ключи, свой
клиент API и своя часть данных в общих таблицах (account_id).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import accounts, avito, db, store
from .avito import AvitoError
from .config import settings
from .ozon import OzonError
from .ozon import get_client as get_ozon_client

log = logging.getLogger("sync")

PAGE_LIMIT = 500
RETURNS_PAGE_LIMIT = 500
RETURNS_MAX_PAGES = 40


def _iso_window(days_back: int, days_forward: int) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days_back), now + timedelta(days=days_forward)


def _account(account: dict | None) -> dict | None:
    return account if account is not None else accounts.default_account()


def sync_postings(account: dict | None = None) -> dict:
    """Забрать отправления в рабочих статусах и освежить те, что из них ушли."""
    account = _account(account)
    if account is None:
        return {"saved": 0, "refreshed": 0}
    account_id = account["id"]
    client = get_ozon_client(account)
    since, to = _iso_window(settings.sync_days_back, settings.sync_days_forward)
    seen: set[str] = set()
    saved = 0

    for status in store.WORK_STATUSES:
        offset = 0
        while True:
            postings, has_next = client.posting_list(status, since, to, limit=PAGE_LIMIT, offset=offset)
            if postings:
                with db.write() as conn:
                    for raw in postings:
                        store.upsert_posting(conn, account_id, raw)
                        seen.add(raw["posting_number"])
                        saved += 1
            if not has_next or not postings:
                break
            offset += PAGE_LIMIT

    # Отправление могло уехать в «Доставляется» или отмениться — узнаём точный статус.
    stale = db.query(
        "SELECT posting_number FROM postings WHERE account_id = ? AND status IN (?, ?)",
        (account_id, *store.WORK_STATUSES),
    )
    refreshed = 0
    for row in stale:
        number = row["posting_number"]
        if number in seen:
            continue
        try:
            raw = client.posting_get(number)
        except OzonError as exc:
            log.warning("Не удалось обновить %s: %s", number, exc)
            continue
        if raw:
            with db.write() as conn:
                store.upsert_posting(conn, account_id, raw)
            refreshed += 1
        else:
            db.execute(
                "UPDATE postings SET status = 'unknown', updated_at = ? WHERE account_id = ? AND posting_number = ?",
                (db.now_iso(), account_id, number),
            )
    return {"saved": saved, "refreshed": refreshed}


def sync_products(account: dict | None = None, limit: int = 500) -> dict:
    """Подтянуть карточки товаров (штрихкоды и фото) для новых SKU."""
    account = _account(account)
    if account is None:
        return {"products": 0}
    account_id = account["id"]
    rows = db.query(
        """
        SELECT DISTINCT i.sku FROM posting_items i
        LEFT JOIN products p ON p.sku = i.sku AND p.account_id = i.account_id
        WHERE i.account_id = ? AND p.sku IS NULL
        UNION
        SELECT DISTINCT r.sku FROM returns r
        LEFT JOIN products p2 ON p2.sku = r.sku AND p2.account_id = r.account_id
        WHERE r.account_id = ? AND p2.sku IS NULL AND r.sku IS NOT NULL
        LIMIT ?
        """,
        (account_id, account_id, limit),
    )
    skus = [row["sku"] for row in rows if row["sku"]]
    if not skus:
        return {"products": 0}

    client = get_ozon_client(account)
    total = 0
    for start in range(0, len(skus), 100):
        chunk = skus[start : start + 100]
        try:
            items = client.product_info(skus=chunk)
        except OzonError as exc:
            log.warning("Карточки товаров недоступны: %s", exc)
            break
        if items:
            with db.write() as conn:
                total += store.upsert_products(conn, account_id, items)
        # SKU без карточки (например, товар архивирован) — чтобы не спрашивать бесконечно.
        found = {str(item.get("sku") or item.get("id")) for item in items}
        missing = [sku for sku in chunk if sku not in found]
        if missing:
            with db.write() as conn:
                for sku in missing:
                    name = db.query_one(
                        "SELECT name, offer_id FROM posting_items WHERE account_id = ? AND sku = ? LIMIT 1",
                        (account_id, sku),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO products(account_id, sku, offer_id, name, barcodes, updated_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (account_id, sku, (name["offer_id"] if name else None),
                         (name["name"] if name else None), "[]", db.now_iso()),
                    )
    return {"products": total}


def sync_returns(account: dict | None = None, *, full: bool = False, statuses: list[str] | None = None) -> dict:
    """Возвраты FBO и FBS: /v1/returns/list.

    Забираем только те статусы, в которых возврат реально можно получить
    (по умолчанию ArrivedAtReturnPlace — «В пункте выдачи»). Фильтр уходит в
    запрос, но на него не полагаемся: всё, что пришло с другим статусом,
    отбрасывается на нашей стороне. Иначе достаточно одной перемены в API,
    чтобы сборщик снова увидел лишнее.
    """
    from .options import get_returns_statuses

    account = _account(account)
    if account is None:
        return {"returns": 0}
    account_id = account["id"]
    client = get_ozon_client(account)
    wanted = list(statuses or get_returns_statuses())
    wanted_set = set(wanted)
    saved = 0
    skipped = 0
    seen: set[str] = set()
    histogram: dict[str, int] = {}
    complete = True

    def remember(raw: dict) -> None:
        """Учитываем, что именно вернул Ozon, — histogram виден в интерфейсе."""
        nonlocal skipped
        sys_name = (((raw.get("visual") or {}).get("status") or {}).get("sys_name")) or "—"
        histogram[sys_name] = histogram.get(sys_name, 0) + 1
        if sys_name not in wanted_set:
            skipped += 1

    def store_page(returns: list[dict]) -> int:
        nonlocal saved
        keep = []
        for raw in returns:
            remember(raw)
            sys_name = (((raw.get("visual") or {}).get("status") or {}).get("sys_name")) or ""
            if sys_name in wanted_set:
                keep.append(raw)
        if keep:
            with db.write() as conn:
                for raw in keep:
                    seen.add(store.upsert_return(conn, account_id, raw))
                    saved += 1
        return len(keep)

    if full:
        # Полный обход без фильтра — чтобы увидеть, что вообще есть в Ozon.
        last_id = 0
        for _page in range(RETURNS_MAX_PAGES):
            try:
                returns, has_next = client.returns_list(limit=RETURNS_PAGE_LIMIT, last_id=last_id)
            except OzonError as exc:
                log.warning("Возвраты недоступны: %s", exc)
                complete = False
                break
            if not returns:
                break
            store_page(returns)
            last_id = returns[-1].get("id") or 0
            if not has_next or not last_id:
                break
    else:
        for status in wanted:
            last_id = 0
            for _page in range(RETURNS_MAX_PAGES):
                try:
                    # В фильтре /v1/returns/list допускается только одно поле,
                    # поэтому статусы запрашиваем по очереди.
                    returns, has_next = client.returns_list(
                        limit=RETURNS_PAGE_LIMIT,
                        last_id=last_id,
                        filter_={"visual_status_name": status},
                    )
                except OzonError as exc:
                    log.warning("Возвраты в статусе %s недоступны: %s", status, exc)
                    complete = False
                    break
                if not returns:
                    break
                store_page(returns)
                last_id = returns[-1].get("id") or 0
                if not has_next or not last_id:
                    break

    gone = 0
    removed = 0
    if complete:
        # Возврат забрали или он уехал дальше — Ozon его в этих статусах больше
        # не отдаёт. Сверку делаем только после полностью успешного обхода,
        # иначе сетевая ошибка очистила бы список выдачи.
        stale = [
            row["id"]
            for row in db.query("SELECT id FROM returns WHERE account_id = ? AND is_ready = 1", (account_id,))
            if row["id"] not in seen
        ]
        if stale:
            placeholders = ",".join("?" for _ in stale)
            db.execute(
                f"UPDATE returns SET is_ready = 0, updated_at = ? WHERE account_id = ? AND id IN ({placeholders})",
                [db.now_iso(), account_id] + stale,
            )
            gone = len(stale)
        # Записи в ненужных статусах, оставшиеся от прошлых версий или прошлых
        # настроек, убираем совсем — кроме тех, что отмечены как забранные.
        placeholders = ",".join("?" for _ in wanted) or "''"
        removed = db.execute(
            "DELETE FROM returns WHERE account_id = ? AND taken_at IS NULL "
            f"AND (status_sys IS NULL OR status_sys NOT IN ({placeholders}))",
            [account_id] + wanted,
        ).rowcount or 0

    db.kv_set("returns_last_statuses", json.dumps(histogram, ensure_ascii=False))
    db.kv_set("returns_last_wanted", ",".join(wanted))
    result = {"returns": saved}
    if skipped:
        result["returns_skipped"] = skipped
    if gone:
        result["returns_gone"] = gone
    if removed:
        result["returns_removed"] = removed
    return result


def sync_avito(account: dict | None = None) -> dict:
    """Заказы Avito, с которыми сборщику надо что-то сделать.

    Это три статуса: «ожидает подтверждения», «ждёт отправки» и «на возврате».
    Остальные не запрашиваются и не хранятся: заказ, уехавший в «в пути» или
    «доставлен», из панели просто исчезает.
    """
    account = _account(account)
    if account is None:
        return {"avito": 0}
    account_id = account["id"]
    client = avito.get_client(account)
    date_from = datetime.now(timezone.utc) - timedelta(days=settings.avito_days_back)

    seen: set[str] = set()
    saved = 0
    try:
        orders = client.orders_all(statuses=list(avito.SYNC_STATUSES), date_from=date_from)
    except AvitoError as exc:
        log.warning("Заказы Avito недоступны: %s", exc)
        raise
    if orders:
        with db.write() as conn:
            for raw in orders:
                seen.add(store.upsert_avito_order(conn, account_id, raw))
                saved += 1

    # Заказ ушёл из рабочих статусов — Avito его больше не отдаёт, убираем и мы.
    stale = [
        row["id"]
        for row in db.query("SELECT id FROM avito_orders WHERE account_id = ?", (account_id,))
        if row["id"] not in seen
    ]
    if stale:
        placeholders = ",".join("?" for _ in stale)
        with db.write() as conn:
            conn.execute(
                f"DELETE FROM avito_order_items WHERE account_id = ? AND order_id IN ({placeholders})",
                [account_id] + stale,
            )
            conn.execute(
                f"DELETE FROM avito_orders WHERE account_id = ? AND id IN ({placeholders})",
                [account_id] + stale,
            )
    result = {"avito": saved}
    ready = db.query_one(
        "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = ? AND return_status = ?",
        (account_id, avito.STATUS_ON_RETURN, avito.RETURN_READY),
    )["c"]
    if ready:
        result["avito_returns"] = ready
    if stale:
        result["avito_gone"] = len(stale)
    return result


def sync_account(account: dict, *, returns: bool = True) -> dict:
    """Один кабинет: набор методов зависит от площадки."""
    if account["marketplace"] == "avito":
        return sync_avito(account)
    result: dict = {}
    result.update(sync_postings(account))
    result.update(sync_products(account))
    if returns:
        result.update(sync_returns(account))
    return result


def sync_all(*, returns: bool = True) -> dict:
    """Все включённые кабинеты. Ошибка одного не останавливает остальные."""
    result: dict = {}
    errors: list[str] = []
    active = accounts.all_accounts(active_only=True)
    for account in active:
        try:
            part = sync_account(account, returns=returns)
        except Exception as exc:  # noqa: BLE001 - кабинет мог остаться без ключей
            log.warning("Кабинет «%s» не синхронизирован: %s", account["title"], exc)
            errors.append(f"{account['title']}: {exc}")
            continue
        for key, value in part.items():
            if isinstance(value, int) and isinstance(result.get(key), int):
                result[key] += value
            else:
                result[key] = value
    if len(active) > 1:
        result["accounts"] = len(active)
    if errors:
        result["errors"] = errors
        # Все кабинеты упали — это уже отказ синхронизации, а не частный сбой.
        if len(errors) == len(active):
            raise RuntimeError("; ".join(errors))
    return result


def run_once(*, returns: bool = True, account: dict | None = None) -> dict:
    """Один проход синхронизации с записью статуса в kv.

    Без account обходит все включённые кабинеты (так работает фоновый поток);
    с account обновляет только его — это кнопка «Обновить» в интерфейсе.
    """
    started = time.time()
    try:
        result = sync_account(account, returns=returns) if account else sync_all(returns=returns)
    except Exception as exc:  # noqa: BLE001 - статус нужен в UI целиком
        log.exception("Синхронизация упала")
        db.kv_set("sync_last_error", f"{db.now_iso()}: {exc}")
        db.kv_set("sync_last_status", "error")
        raise
    db.kv_set("sync_last_ok", db.now_iso())
    db.kv_set("sync_last_status", "ok")
    db.kv_set("sync_last_error", "")
    db.kv_set("sync_last_result", str(result))
    db.kv_set("sync_last_duration", f"{time.time() - started:.1f}")
    return result


class SyncWorker(threading.Thread):
    """Отдельный поток: отправления часто, возвраты реже."""

    daemon = True

    def __init__(self) -> None:
        super().__init__(name="ozon-sync")
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_returns = 0.0

    def request_sync(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:  # pragma: no cover - фоновой поток
        while not self._stop.is_set():
            with_returns = time.time() - self._last_returns > settings.sync_returns_interval
            try:
                run_once(returns=with_returns)
                if with_returns:
                    self._last_returns = time.time()
            except Exception:  # noqa: BLE001 - поток не должен умирать
                pass
            self._wake.wait(timeout=settings.sync_interval)
            self._wake.clear()


_worker: SyncWorker | None = None


def start_worker() -> SyncWorker | None:
    global _worker
    if not settings.sync_enabled:
        log.info("Фоновая синхронизация отключена (SYNC_ENABLED=0)")
        return None
    if _worker is None:
        _worker = SyncWorker()
        _worker.start()
    return _worker


def get_worker() -> SyncWorker | None:
    return _worker


def status() -> dict:
    active = accounts.all_accounts(active_only=True)
    return {
        "last_ok": db.kv_get("sync_last_ok"),
        "last_status": db.kv_get("sync_last_status", "never"),
        "last_error": db.kv_get("sync_last_error"),
        "last_result": db.kv_get("sync_last_result"),
        "duration": db.kv_get("sync_last_duration"),
        "interval": settings.sync_interval,
        "enabled": settings.sync_enabled,
        # Демо, пока ни один кабинет не подключён боевыми ключами.
        "demo": all(accounts.is_demo(a) for a in active) if active else True,
    }
