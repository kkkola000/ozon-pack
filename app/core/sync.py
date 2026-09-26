"""Фоновая синхронизация: панель сама ходит за данными во все кабинеты.

Что именно грузить, знает площадка — ядро зовёт market.sync(account) и по
именам площадок ничего не решает. Обходятся все включённые кабинеты: у каждого
свои ключи, свой клиент API и своя часть данных (account_id).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import accounts, db
from .config import settings

log = logging.getLogger("sync")

# Когда кабинет последний раз успешно сходил на площадку: «sync_account_at:<id>».
# Это и есть «Обновлено» на экране.
KV_ACCOUNT_SYNC = "sync_account_at"
# Когда в последний раз пробовали обновить его при открытии рабочего места —
# считая неудачи. Без этого отказавшая площадка тормозила бы каждый F5.
KV_ACCOUNT_TRIED = "sync_tried_at"

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
    result = market.sync(account, returns_too=returns)
    db.kv_set(f"{KV_ACCOUNT_SYNC}:{account['id']}", db.now_iso())
    return result


def sync_many(shops: list[dict], *, returns: bool = False) -> tuple[list[str], list[str]]:
    """«Обновить заказы» по нескольким кабинетам: (обновлённые, «кабинет: причина»).

    Отказ одного кабинета не отменяет остальных — склад общий, и из-за одной
    упавшей площадки не должны устаревать заказы других.
    """
    done, failed = [], []
    for shop in shops:
        try:
            sync_account(shop, returns=returns)
            done.append(shop["title"])
        except Exception as exc:  # noqa: BLE001 - причину показываем оператору
            failed.append(f"{shop['title']}: {exc}")
    return done, failed


def refresh(shops: list[dict]) -> dict:
    """«Обновить заказы» — ответ кнопки. ValueError — нечего обновлять,
    RuntimeError — не ответил ни один кабинет (текст — причины)."""
    if not shops:
        raise ValueError("Нет ни одного кабинета с ключами")
    done, failed = sync_many(shops)
    if not done:
        raise RuntimeError("; ".join(failed))
    message = f"Обновлено кабинетов: {len(done)}"
    if failed:
        message += f". Не ответили: {', '.join(name.split(':')[0] for name in failed)}"
    return {"status": "ok", "message": message, "updated": done, "failed": failed}


def synced_at(account_id: int) -> str | None:
    """Когда кабинет последний раз ходил на площадку. Неважно, кто его отправил."""
    return db.kv_get(f"{KV_ACCOUNT_SYNC}:{int(account_id)}") or None


def freshen(account: dict | None, *, max_age: int | None = None, returns: bool = False) -> bool:
    """Обновить кабинет, если его давно не обновляли. Для открытия рабочего места.

    Сборщик открывает «Сборку» и сразу сканирует — данные к этому моменту
    должны быть свежими, а не такими, какими их оставил прошлый обход. Но
    ходить на площадку при каждом нажатии F5 нельзя: у API есть пределы, а
    страница ждала бы ответа. Поэтому обновляем не чаще, чем раз в
    SYNC_INTERVAL, — как если бы это сделал фоновый поток.

    Возвраты не трогаем: они тяжелее, меняются медленнее и на сканирование не
    влияют. Их ведёт фоновая синхронизация и кнопка в разделе возвратов.

    Ошибка площадки страницу не роняет: рабочее место важнее свежести, и в
    худшем случае сборщик увидит прошлые данные и нажмёт «Обновить заказы».
    """
    if not account or not accounts.is_configured(account):
        return False
    limit = settings.sync_interval if max_age is None else max_age
    if not _older_than(db.kv_get(f"{KV_ACCOUNT_TRIED}:{account['id']}"), limit):
        return False
    # Отметку ставим до похода: площадка может и отказать, и тогда повторять
    # отказ на каждое открытие страницы — худшее, что можно сделать.
    db.kv_set(f"{KV_ACCOUNT_TRIED}:{account['id']}", db.now_iso())
    try:
        sync_account(account, returns=returns)
    except Exception as exc:  # noqa: BLE001 - страница должна открыться в любом случае
        log.warning("Кабинет «%s» не обновился при открытии: %s", account.get("title"), exc)
        return False
    return True


def _older_than(stamp: str | None, limit: int) -> bool:
    """Прошло ли с момента stamp больше limit секунд. Нет отметки — считаем, что да."""
    if not stamp:
        return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return True
    return age >= limit


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
