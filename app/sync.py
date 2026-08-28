"""Фоновая синхронизация с Ozon: отправления, товары, возвраты."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import db, store
from .config import settings
from .credentials import is_demo
from .ozon import OzonError, get_client

log = logging.getLogger("sync")

PAGE_LIMIT = 500
RETURNS_PAGE_LIMIT = 500
RETURNS_MAX_PAGES = 40


def _iso_window(days_back: int, days_forward: int) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days_back), now + timedelta(days=days_forward)


def sync_postings() -> dict:
    """Забрать отправления в рабочих статусах и освежить те, что из них ушли."""
    client = get_client()
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
                        store.upsert_posting(conn, raw)
                        seen.add(raw["posting_number"])
                        saved += 1
            if not has_next or not postings:
                break
            offset += PAGE_LIMIT

    # Отправление могло уехать в «Доставляется» или отмениться — узнаём точный статус.
    stale = db.query(
        "SELECT posting_number FROM postings WHERE status IN (?, ?)",
        store.WORK_STATUSES,
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
                store.upsert_posting(conn, raw)
            refreshed += 1
        else:
            db.execute("UPDATE postings SET status = 'unknown', updated_at = ? WHERE posting_number = ?", (db.now_iso(), number))
    return {"saved": saved, "refreshed": refreshed}


def sync_products(limit: int = 500) -> dict:
    """Подтянуть карточки товаров (штрихкоды и фото) для новых SKU."""
    rows = db.query(
        """
        SELECT DISTINCT i.sku FROM posting_items i
        LEFT JOIN products p ON p.sku = i.sku
        WHERE p.sku IS NULL
        UNION
        SELECT DISTINCT r.sku FROM returns r
        LEFT JOIN products p2 ON p2.sku = r.sku
        WHERE p2.sku IS NULL AND r.sku IS NOT NULL
        LIMIT ?
        """,
        (limit,),
    )
    skus = [row["sku"] for row in rows if row["sku"]]
    if not skus:
        return {"products": 0}

    client = get_client()
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
                total += store.upsert_products(conn, items)
        # SKU без карточки (например, товар архивирован) — чтобы не спрашивать бесконечно.
        found = {str(item.get("sku") or item.get("id")) for item in items}
        missing = [sku for sku in chunk if sku not in found]
        if missing:
            with db.write() as conn:
                for sku in missing:
                    name = db.query_one("SELECT name, offer_id FROM posting_items WHERE sku = ? LIMIT 1", (sku,))
                    conn.execute(
                        "INSERT OR IGNORE INTO products(sku, offer_id, name, barcodes, updated_at) VALUES(?,?,?,?,?)",
                        (sku, (name["offer_id"] if name else None), (name["name"] if name else None), "[]", db.now_iso()),
                    )
    return {"products": total}


def sync_returns(*, full: bool = False, statuses: list[str] | None = None) -> dict:
    """Возвраты FBO и FBS: /v1/returns/list.

    Забираем только те статусы, в которых возврат реально можно получить
    (по умолчанию ArrivedAtReturnPlace — «В пункте выдачи»). Фильтр уходит в
    запрос, но на него не полагаемся: всё, что пришло с другим статусом,
    отбрасывается на нашей стороне. Иначе достаточно одной перемены в API,
    чтобы сборщик снова увидел лишнее.
    """
    from .options import get_returns_statuses

    client = get_client()
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
                    seen.add(store.upsert_return(conn, raw))
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
        stale = [row["id"] for row in db.query("SELECT id FROM returns WHERE is_ready = 1") if row["id"] not in seen]
        if stale:
            placeholders = ",".join("?" for _ in stale)
            db.execute(
                f"UPDATE returns SET is_ready = 0, updated_at = ? WHERE id IN ({placeholders})",
                [db.now_iso()] + stale,
            )
            gone = len(stale)
        # Записи в ненужных статусах, оставшиеся от прошлых версий или прошлых
        # настроек, убираем совсем — кроме тех, что отмечены как забранные.
        placeholders = ",".join("?" for _ in wanted) or "''"
        removed = db.execute(
            f"DELETE FROM returns WHERE taken_at IS NULL AND (status_sys IS NULL OR status_sys NOT IN ({placeholders}))",
            wanted,
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


def sync_all(*, returns: bool = True) -> dict:
    result: dict = {}
    result.update(sync_postings())
    result.update(sync_products())
    if returns:
        result.update(sync_returns())
    return result


def run_once(*, returns: bool = True) -> dict:
    """Один проход синхронизации с записью статуса в kv."""
    started = time.time()
    try:
        result = sync_all(returns=returns)
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
    return {
        "last_ok": db.kv_get("sync_last_ok"),
        "last_status": db.kv_get("sync_last_status", "never"),
        "last_error": db.kv_get("sync_last_error"),
        "last_result": db.kv_get("sync_last_result"),
        "duration": db.kv_get("sync_last_duration"),
        "interval": settings.sync_interval,
        "enabled": settings.sync_enabled,
        "demo": is_demo(),
    }
