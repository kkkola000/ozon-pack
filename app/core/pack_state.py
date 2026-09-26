"""Начатая сборка человека и бронь заказа — одинаково у всех площадок.

Строка pack_state одна на сборщика: какой заказ он держит (кабинет + номер) и
что уже отсканировано. Раньше каждая площадка писала эти четыре действия
сама, и они разошлись: Ozon и Avito снимали бронь только в своей таблице, а
Маркет — во всех трёх, перечисляя чужие таблицы по имени. Сборка теперь
сквозная, и прежний заказ может оказаться любой площадки, — поэтому бронь
снимается в таблице площадки того кабинета, чей заказ человек держал. Где
эта таблица и как в ней называется номер, площадка объявляет в OrdersBoard.
"""
from __future__ import annotations

import json

from . import accounts, db


def _registry():
    from ..markets import registry

    return registry


def clear(user: dict, conn=None) -> None:
    """Забыть начатую сборку человека."""
    sql = "DELETE FROM pack_state WHERE user_id = ?"
    (conn.execute if conn is not None else db.execute)(sql, (user["id"],))


def save(conn, account: dict, user: dict, number: str, scanned) -> None:
    """Запомнить, что человек собирает и что уже отсканировано.

    Время начала сохраняется, пока заказ тот же: оно нужно журналу — сколько
    собирали, — а не каждому скану.
    """
    now = db.now_iso()
    conn.execute(
        """
        INSERT INTO pack_state(user_id, account_id, posting_number, scanned, started_at, updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET account_id = excluded.account_id,
            posting_number = excluded.posting_number, scanned = excluded.scanned,
            updated_at = excluded.updated_at,
            started_at = CASE WHEN pack_state.posting_number = excluded.posting_number
                              THEN pack_state.started_at ELSE excluded.started_at END
        """,
        (user["id"], account["id"], number, json.dumps(scanned, ensure_ascii=False), now, now),
    )


def unclaim(conn, account_id: int, number: str) -> None:
    """Снять бронь с заказа — в таблице площадки его кабинета."""
    account = accounts.get(account_id)
    market = _registry().get(account["marketplace"]) if account else None
    board = market.orders if market else None
    if board is None:
        return
    conn.execute(
        f"UPDATE {board.table} SET claim_user_id = NULL, claim_login = NULL, claim_at = NULL "
        f"WHERE account_id = ? AND {board.key} = ?",
        (account_id, number),
    )


def release_previous(conn, user: dict, *, keep: tuple[int, str] | None = None) -> tuple[int, str] | None:
    """Снять бронь с заказа, который человек держал раньше. keep — его не трогать.

    Возвращает (кабинет, номер) отпущенного заказа или None.
    """
    row = conn.execute(
        "SELECT account_id, posting_number FROM pack_state WHERE user_id = ?", (user["id"],)
    ).fetchone()
    if not row or not row["posting_number"]:
        return None
    previous = (row["account_id"], row["posting_number"])
    if keep is not None and previous == keep:
        return None
    unclaim(conn, *previous)
    return previous


def release(user: dict, *, event: str, reason: str = "manual") -> tuple[int, str] | None:
    """«Отменить сборку»: отпустить заказ и забыть состояние. В журнал — если было что."""
    with db.write() as conn:
        released = release_previous(conn, user)
        if released:
            db.log_event(
                event, account_id=released[0], user=user, posting_number=released[1],
                message=f"Сборка отменена ({reason})", conn=conn,
            )
        clear(user, conn)
    return released
