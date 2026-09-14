"""Загрузка каталога кабинета целиком.

Обычная синхронизация тянет карточки только тех товаров, что встретились в
заказах и возвратах: сборщику больше и не нужно. Но набор собирают из того, что
лежит на складе, а не из того, что вчера заказали, — и для раздела «Товары»
каталог нужен полный.

Поэтому отдельная кнопка, а не фоновая задача: полный обход — это тысячи
карточек и десятки запросов к Ozon, гонять его каждые несколько минут незачем.
Каталог меняется редко, и человек знает, когда завёл новый товар.

Архив не сохраняем как живой товар: архивный товар не продаётся, и в списке
выбора он только мешает. Но и строку не удаляем — её штрихкоды могут
понадобиться, если архивный товар остался в несобранном заказе.

Идёт обход в отдельном потоке: запрос из браузера столько не ждёт, а обрывать
загрузку на середине нельзя — каталог останется наполовину старым.
"""
from __future__ import annotations

import json
import logging
import threading

from . import db, ozon, store

log = logging.getLogger("catalog")

# Ozon отдаёт список страницами по last_id. Предел на всякий случай: без него
# ошибка в курсоре крутила бы обход бесконечно.
PAGE_LIMIT = 1000
MAX_PAGES = 200
# Карточки запрашиваются пачками — столько артикулов за раз принимает
# /v3/product/info/list.
INFO_CHUNK = 100

KV_JOB = "catalog_job"


def _is_archived(item: dict) -> bool:
    """Архивный ли товар. Имя поля у Ozon в разных методах разное.

    Неизвестное значение считаем «не архив»: спрятать живой товар хуже, чем
    показать архивный — из-за первого набор не соберёшь.
    """
    for key in ("archived", "is_archived"):
        value = item.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"):
            return True
    return False


def offers_of(client, *, on_page=None) -> tuple[list[str], int]:
    """Артикулы живых товаров кабинета и сколько отсеяно архивных."""
    offers: list[str] = []
    archived = 0
    last_id = ""
    for _page in range(MAX_PAGES):
        items, last_id, _total = client.product_list(limit=PAGE_LIMIT, last_id=last_id)
        if not items:
            break
        for item in items:
            if _is_archived(item):
                archived += 1
                continue
            offer = str(item.get("offer_id") or "").strip()
            if offer:
                offers.append(offer)
        if on_page:
            on_page(len(offers))
        if not last_id:
            break
    return offers, archived


def refresh(account: dict, *, progress=None) -> dict:
    """Перечитать каталог кабинета целиком. Возвращает, что получилось.

    progress(done, total) — чтобы кнопка показывала, сколько уже прошло: обход
    большого каталога идёт минуту и дольше, и молчащая кнопка выглядит как
    зависшая панель.
    """
    account_id = account["id"]
    client = ozon.get_client(account)
    offers, archived_listed = offers_of(client, on_page=lambda found: progress and progress(0, found))
    total = len(offers)
    if progress:
        progress(0, total)

    saved = 0
    live: list[str] = []
    for start in range(0, total, INFO_CHUNK):
        chunk = offers[start : start + INFO_CHUNK]
        items = client.product_info(offer_ids=chunk)
        fresh = [item for item in items if not _is_archived(item)]
        if fresh:
            with db.write() as conn:
                saved += store.upsert_products(conn, account_id, fresh)
            live += [key for key in (store.product_key(item) for item in fresh) if key]
        if progress:
            progress(min(start + INFO_CHUNK, total), total)

    archived_here = _mark_archived(account_id, live)

    result = {
        "offers": total,
        "saved": saved,
        "archived_skipped": archived_listed,
        "archived_marked": archived_here,
        "live": db.query_one(
            "SELECT COUNT(*) AS c FROM products WHERE account_id = ? AND archived = 0", (account_id,)
        )["c"],
    }
    db.log_event(
        "catalog_refresh", account_id=account_id,
        message=f"каталог: {result['live']} товаров, архив пропущен: {archived_listed}",
    )
    return result


def _mark_archived(account_id: int, live: list[str]) -> int:
    """Развести живое и архивное по итогам обхода.

    Сверяем по списку SKU, а не по времени записи: время у панели в секундах, и
    два обхода подряд для него неразличимы. Список кладём во временную таблицу —
    тысячи SKU в IN упёрлись бы в предел числа параметров SQLite.

    Пропавшее не удаляем, а помечаем архивом: по штрихкоду такого товара могут
    собирать заказ, который уже в работе.
    """
    if not live:
        # Обход не принёс ни одной карточки — это похоже на сбой, а не на
        # опустевший склад. Отправлять весь каталог в архив на таком основании
        # нельзя: раздел опустеет, и наборы будет не из чего собирать.
        return 0
    with db.write() as conn:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS catalog_live(sku TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM catalog_live")
        conn.executemany("INSERT OR IGNORE INTO catalog_live(sku) VALUES(?)", [(sku,) for sku in live])
        archived = conn.execute(
            "UPDATE products SET archived = 1 WHERE account_id = ? AND archived = 0 "
            "AND sku NOT IN (SELECT sku FROM catalog_live)",
            (account_id,),
        ).rowcount or 0
        # И наоборот: товар вернули из архива — он снова виден в разделе.
        conn.execute(
            "UPDATE products SET archived = 0 WHERE account_id = ? AND archived = 1 "
            "AND sku IN (SELECT sku FROM catalog_live)",
            (account_id,),
        )
        conn.execute("DELETE FROM catalog_live")
    return archived


# ------------------------------------------------------------------ фоновая задача
_lock = threading.Lock()
_running: set[int] = set()


def job_key(account_id: int) -> str:
    return f"{KV_JOB}:{account_id}"


def job_status(account_id: int) -> dict:
    try:
        saved = json.loads(db.kv_get(job_key(account_id)) or "{}")
    except ValueError:
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    saved.setdefault("status", "never")
    saved["running"] = account_id in _running
    return saved


def _save_job(account_id: int, **fields) -> None:
    db.kv_set(job_key(account_id), json.dumps(fields, ensure_ascii=False))


def start(account: dict, user: dict | None = None) -> dict:
    """Запустить обход каталога в фоне. Второй раз подряд — не запускать.

    Два обхода одного кабинета разом только мешали бы друг другу: пишут они в
    одни и те же строки, а Ozon за частые запросы отвечает отказом.
    """
    account_id = account["id"]
    with _lock:
        if account_id in _running:
            return {"status": "running", "message": "Каталог уже обновляется"}
        _running.add(account_id)

    _save_job(account_id, status="running", done=0, total=0,
              started_at=db.now_iso(), by=(user or {}).get("login"))

    def work() -> None:  # pragma: no cover - проверяется через refresh()
        try:
            result = refresh(
                account,
                progress=lambda done, total: _save_job(
                    account_id, status="running", done=done, total=total,
                    started_at=db.now_iso(), by=(user or {}).get("login"),
                ),
            )
            _save_job(account_id, status="ok", finished_at=db.now_iso(),
                      by=(user or {}).get("login"), **result)
        except Exception as exc:  # noqa: BLE001 - причину показываем человеку
            log.warning("Каталог кабинета %s не обновился: %s", account_id, exc)
            _save_job(account_id, status="error", finished_at=db.now_iso(),
                      by=(user or {}).get("login"), error=str(exc))
        finally:
            with _lock:
                _running.discard(account_id)

    threading.Thread(target=work, name=f"catalog-{account_id}", daemon=True).start()
    return {"status": "started", "message": "Обновляем каталог — это может занять минуту"}
