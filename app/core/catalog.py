"""Загрузка каталога кабинета целиком.

Обычная синхронизация тянет карточки только тех товаров, что встретились в
заказах и возвратах: сборщику больше и не нужно. Но набор собирают из того, что
лежит на складе, а не из того, что вчера заказали, — и для раздела «Товары»
каталог нужен полный.

Поэтому отдельная кнопка, а не фоновая задача: полный обход — это тысячи
карточек и десятки запросов к площадке, гонять его каждые несколько минут
незачем. Каталог меняется редко, и человек знает, когда завёл новый товар.

Архив не сохраняем как живой товар: архивный товар не продаётся, и в списке
выбора он только мешает. Но и строку не удаляем — её штрихкоды могут
понадобиться, если архивный товар остался в несобранном заказе.

Как устроен обход — дело площадки: Ozon сначала отдаёт список артикулов и
только потом карточки пачками, Маркет отдаёт всё сразу страницами. Ядро про это
не знает: оно перебирает пачки карточек, сохраняет их и сводит итог.

Идёт обход в отдельном потоке: запрос из браузера столько не ждёт, а обрывать
загрузку на середине нельзя — каталог останется наполовину старым.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable

from . import db
from .store import _text


log = logging.getLogger("catalog")

KV_JOB = "catalog_job"


def card_key(card: dict) -> str:
    """SKU карточки так, как его сохраняет панель."""
    return str(card.get("sku") or "").strip()


def save(conn: sqlite3.Connection, account_id: int, cards: Iterable[dict]) -> int:
    """Сохранить пачку карточек: имя, фото и штрихкоды для сканирования.

    Карточка приходит от площадки уже в общем виде — {sku, offer_id, name,
    image, barcodes}: разбирать ответ площадки здесь нечего, этим занимается её
    собственный модуль.
    """
    count = 0
    for card in cards:
        sku = card_key(card)
        if not sku:
            continue
        barcodes = [str(code).strip() for code in (card.get("barcodes") or []) if str(code).strip()]
        conn.execute(
            """
            INSERT INTO products(account_id, sku, offer_id, name, image, barcodes, updated_at) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(account_id, sku) DO UPDATE SET offer_id = excluded.offer_id, name = excluded.name,
                image = excluded.image, barcodes = excluded.barcodes, updated_at = excluded.updated_at
            """,
            (account_id, sku, _text(card.get("offer_id")), _text(card.get("name")), _text(card.get("image")),
             json.dumps(barcodes, ensure_ascii=False), db.now_iso()),
        )
        for barcode in barcodes:
            conn.execute(
                "INSERT INTO product_barcodes(account_id, barcode, sku) VALUES(?, ?, ?) "
                "ON CONFLICT(account_id, barcode) DO UPDATE SET sku = excluded.sku",
                (account_id, barcode, sku),
            )
        count += 1
    return count


def _source(account: dict):
    """Кто наполняет каталог этого кабинета. Площадка без каталога — понятная ошибка."""
    from ..markets import registry

    market = registry.get(account.get("marketplace"))
    if market is None or market.catalog is None:
        title = market.title if market else account.get("marketplace")
        raise RuntimeError(f"Площадка «{title}» каталог товаров не отдаёт")
    return market.catalog


def refresh(account: dict, *, progress=None) -> dict:
    """Перечитать каталог кабинета целиком. Возвращает, что получилось.

    progress(done, total) — чтобы кнопка показывала, сколько уже прошло: обход
    большого каталога идёт минуту и дольше, и молчащая кнопка выглядит как
    зависшая панель. Сколько всего карточек, площадка может и не знать заранее —
    тогда total равен нулю, и кнопка просто считает пройденные.
    """
    account_id = account["id"]
    source = _source(account)

    saved = 0
    done = 0
    total = 0
    archived_listed = 0
    live: list[str] = []
    for page in source.pages(account):
        total = page.total or total
        archived_listed += page.skipped
        if page.items:
            with db.write() as conn:
                saved += save(conn, account_id, page.items)
            live += [key for key in (card_key(card) for card in page.items) if key]
            done += len(page.items)
        if progress:
            progress(done, total)

    archived_here = _mark_archived(account_id, live)

    result = {
        "offers": total or done,
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


def offer_barcodes(offer_id: str | None) -> list[str]:
    """Штрихкоды товара по артикулу — из каталога любого кабинета панели.

    Кабинет не ограничиваем намеренно: каталог наполняется из Ozon, а заказ
    пришёл из Маркета. Артикул у продавца один на все площадки, и искать
    штрихкод только в своём кабинете значило бы не найти его никогда.
    """
    if not offer_id:
        return []
    rows = db.query(
        "SELECT DISTINCT b.barcode FROM product_barcodes b "
        "JOIN products p ON p.account_id = b.account_id AND p.sku = b.sku "
        "WHERE p.offer_id = ? ORDER BY b.barcode",
        (offer_id,),
    )
    return [row["barcode"] for row in rows]
