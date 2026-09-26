"""Разовые правки базы для заказов Маркета — каждая помечает себя в kv.

Порядок вызовов в migrate() — тот же, в каком правки появлялись в панели.
"""
from __future__ import annotations

import logging
import sqlite3

from ...core.db import _table_exists, now_iso
from . import store

log = logging.getLogger("yandex.migrations")

KV_EARLY_LABELS_FORGOTTEN = "yandex_early_labels_forgotten"


def _forget_early_labels(conn: sqlite3.Connection) -> None:
    """Снять отметку «ярлык скачан» у заказов «Ожидает отгрузки», не собранных.

    До 1.51.1 выгрузка брала ярлыки Маркета и в «Ожидает сборки». Заказ,
    перешедший потом в «Ожидает отгрузки», числился выгруженным, и сборка не
    запиралась — а ярлык на «Сборку» нужен скачанный в «Ожидает отгрузки».
    Когда именно его скачали, по базе не понять, поэтому один раз просим
    скачать ярлыки таких заказов заново: лишний файл лучше пропавшей наклейки.
    Дальше отметку сбрасывает сама синхронизация — при переходе заказа в
    «Ожидает отгрузки» (store.upsert_yandex_order).
    """
    if not _table_exists(conn, "yandex_orders"):
        return
    if conn.execute("SELECT 1 FROM kv WHERE key = ?", (KV_EARLY_LABELS_FORGOTTEN,)).fetchone():
        return
    reset = conn.execute(
        "UPDATE yandex_orders SET label_saved_at = NULL "
        "WHERE substatus = ? AND local_state != 'packed' AND label_saved_at IS NOT NULL",
        (store.READY_TO_SHIP,),
    ).rowcount or 0
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KV_EARLY_LABELS_FORGOTTEN, now_iso()),
    )
    if reset:
        log.info("Маркет: ярлыки %d заказов «Ожидает отгрузки» нужно скачать заново", reset)


def migrate(conn: sqlite3.Connection) -> None:
    """Все разовые правки, в историческом порядке."""
    _forget_early_labels(conn)
