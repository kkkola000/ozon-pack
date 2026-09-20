"""Фоновая синхронизация: отправления, товары и возвраты Ozon, заказы Avito.

Синхронизация идёт по всем включённым кабинетам: у каждого свои ключи, свой
клиент API и своя часть данных в общих таблицах (account_id).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import accounts, db
from .config import settings

log = logging.getLogger("sync")

PAGE_LIMIT = 500
RETURNS_PAGE_LIMIT = 500
RETURNS_MAX_PAGES = 40


def _iso_window(days_back: int, days_forward: int) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days_back), now + timedelta(days=days_forward)


def _account(account: dict | None) -> dict | None:
    return account if account is not None else accounts.default_account()


def sync_account(account: dict, *, returns: bool = True) -> dict:
    """Один кабинет: что и как грузить, знает его площадка."""
    from ..markets import registry

    market = registry.get(account["marketplace"])
    if market is None:
        raise RuntimeError(f"Кабинет «{account.get('title')}»: неизвестная площадка {account['marketplace']!r}")
    return market.sync(account, returns_too=returns)


def sync_all(*, returns: bool = True) -> dict:
    """Все включённые кабинеты. Ошибка одного не останавливает остальные."""
    result: dict = {}
    errors: list[str] = []
    # Без ключей запрашивать нечего — и придумывать данные панель не станет.
    active = [a for a in accounts.all_accounts(active_only=True) if accounts.is_configured(a)]
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
                # Поток переживает любую ошибку, но молчать о ней нельзя: сбой
                # в самой записи статуса иначе исчезает бесследно.
                log.exception("Проход синхронизации сорвался")
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
        # Кабинеты без ключей синхронизировать нечем — о них говорим отдельно.
        "unconfigured": [a["title"] for a in active if not accounts.is_configured(a)],
    }
