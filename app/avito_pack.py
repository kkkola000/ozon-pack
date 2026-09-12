"""Рабочее место сборщика Avito.

У Avito штрихкодов товаров нет, поэтому порядок обратный тому, что на Ozon:

  1. сборщик печатает стикер отправления;
  2. сканирует стикер — открывается сборка этого заказа;
  3. сканирует штрихкод товара (свой, поставщика — любой, какой есть);
  4. панель записывает его в отчёт вместе с отправлением и названием товара;
  5. когда отсканированы все позиции, заказ помечается собранным.

Сверять штрихкод не с чем: справочника товаров Avito не отдаёт. Поэтому
панель не решает, «тот» это товар или нет, а фиксирует, что именно приложили
к этому отправлению. Ошибкой считается только скан стикера чужого заказа.
"""
from __future__ import annotations

import json
import re

from . import db, report, store
from .avito import STATUS_LABELS

# Заказы, которые сборщику имеет смысл собирать.
PACKABLE = "ready_to_ship"


class ScanResult(dict):
    def __init__(self, status: str, message: str, *, action: str = "noop",
                 sound: str | None = None, **extra) -> None:
        super().__init__(status=status, message=message, action=action, sound=sound or status, **extra)


# ------------------------------------------------------------------ опознание стикера
def code_variants(code: str) -> list[str]:
    """Стикер может нести номер заказа, отправления или трек — берём как есть и цифрами."""
    code = (code or "").strip()
    if not code:
        return []
    variants = [code]
    digits = re.sub(r"\D", "", code)
    if digits and digits != code:
        variants.append(digits)
    if code.lstrip("0") and code.lstrip("0") != code:
        variants.append(code.lstrip("0"))
    seen: list[str] = []
    for item in variants:
        if item and item not in seen:
            seen.append(item)
    return seen


def find_order(account_id: int, code: str) -> dict | None:
    variants = code_variants(code)
    if not variants:
        return None
    marks = ",".join("?" for _ in variants)
    row = db.query_one(
        f"""
        SELECT * FROM avito_orders
        WHERE account_id = ?
          AND (id IN ({marks}) OR marketplace_id IN ({marks})
               OR dispatch_number IN ({marks}) OR tracking_number IN ({marks}))
        LIMIT 1
        """,
        [account_id] + variants * 4,
    )
    return dict(row) if row else None


# ------------------------------------------------------------------ состояние сборки
def load_state(account: dict, user: dict) -> dict:
    empty = {"active": None, "items": [], "done": 0, "total": 0, "complete": False, "scanned": []}
    row = db.query_one(
        "SELECT * FROM pack_state WHERE user_id = ? AND account_id = ?", (user["id"], account["id"])
    )
    if not row or not row["posting_number"]:
        return empty

    order_row = db.query_one(
        "SELECT * FROM avito_orders WHERE account_id = ? AND id = ?", (account["id"], row["posting_number"])
    )
    if not order_row:
        clear_state(user)
        return empty

    scanned = json.loads(row["scanned"] or "[]")
    if isinstance(scanned, dict):          # состояние от рабочего места Ozon
        scanned = []
    order = store.avito_view(order_row)
    units = _units(account["id"], row["posting_number"])
    items = []
    for index, (item, unit_no) in enumerate(units):
        items.append({
            **item,
            "unit_no": unit_no,
            "scanned": index < len(scanned),
            "barcode": scanned[index] if index < len(scanned) else None,
        })
    done = min(len(scanned), len(units))
    return {
        "active": order,
        "items": items,
        "done": done,
        "total": len(units),
        "complete": bool(units) and done >= len(units),
        "scanned": scanned,
        "started_at": row["started_at"],
    }


def _units(account_id: int, order_id: str) -> list[tuple[dict, int]]:
    """Позиции заказа, развёрнутые по единицам: на каждую нужен один скан."""
    units: list[tuple[dict, int]] = []
    for item in store.avito_items(account_id, order_id):
        for number in range(1, max(1, int(item.get("quantity") or 1)) + 1):
            units.append((item, number))
    return units


def clear_state(user: dict, conn=None) -> None:
    sql = "DELETE FROM pack_state WHERE user_id = ?"
    (conn.execute if conn is not None else db.execute)(sql, (user["id"],))


def _save_state(conn, account: dict, user: dict, order_id: str, scanned: list[str]) -> None:
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
        (user["id"], account["id"], order_id, json.dumps(scanned, ensure_ascii=False), now, now),
    )


def _release_previous(conn, user: dict, *, keep: tuple[int, str] | None = None) -> None:
    row = conn.execute(
        "SELECT account_id, posting_number FROM pack_state WHERE user_id = ?", (user["id"],)
    ).fetchone()
    if not row or not row["posting_number"]:
        return
    previous = (row["account_id"], row["posting_number"])
    if keep is not None and previous == keep:
        return
    conn.execute(
        "UPDATE avito_orders SET claim_user_id = NULL, claim_login = NULL, claim_at = NULL "
        "WHERE account_id = ? AND id = ?",
        previous,
    )


def release(account: dict, user: dict) -> ScanResult:
    with db.write() as conn:
        _release_previous(conn, user)
        clear_state(user, conn)
        db.log_event("avito_pack_release", account_id=account["id"], user=user,
                     message="Сборка отменена", conn=conn)
    return ScanResult("ok", "Сборка отменена", action="released", state=load_state(account, user))


# ------------------------------------------------------------------ основной вход
def scan(account: dict, user: dict, code: str) -> ScanResult:
    code = (code or "").strip()
    if not code:
        return ScanResult("error", "Пустой скан", state=load_state(account, user))

    state = load_state(account, user)
    order = find_order(account["id"], code)

    if state["active"] is None:
        if order:
            return open_order(account, user, order, code)
        db.log_event("avito_scan_unknown", level="error", account_id=account["id"], user=user,
                     barcode=code, message="Стикер не опознан")
        return ScanResult(
            "error",
            f"Код «{code}» не найден среди заказов Avito. Сначала отсканируйте стикер отправления.",
            action="unknown",
            state=state,
        )

    active_id = state["active"]["id"]
    if order and order["id"] != active_id:
        with db.write() as conn:
            db.log_event("avito_scan_wrong_label", level="error", account_id=account["id"], user=user,
                         posting_number=active_id, barcode=code,
                         message=f"Стикер заказа {order.get('marketplace_id') or order['id']}", conn=conn)
            report.record_error(
                conn, account, user, "wrong_label", posting_number=active_id, barcode=code,
                name=f"стикер заказа {order.get('marketplace_id') or order['id']}",
            )
        return ScanResult(
            "error",
            f"СТОП: это стикер заказа {order.get('marketplace_id') or order['id']}, "
            f"а вы собираете {state['active'].get('marketplace_id') or active_id}.",
            action="wrong_label",
            state=state,
        )

    if order and order["id"] == active_id:
        if state["complete"]:
            return complete(account, user, active_id, code)
        left = state["total"] - state["done"]
        return ScanResult(
            "warning",
            f"Сначала отсканируйте товар: осталось {left} из {state['total']}.",
            action="incomplete",
            sound="error",
            state=state,
        )

    return _scan_item(account, user, state, code)


def open_order(account: dict, user: dict, order: dict, code: str | None = None) -> ScanResult:
    order_id = order["id"]
    number = order.get("marketplace_id") or order_id

    if order.get("local_state") == "packed":
        return ScanResult(
            "warning",
            f"Заказ {number} уже собран ({order.get('packed_by') or '—'}). Повторно собирать не нужно.",
            action="already_packed",
            sound="error",
            state=load_state(account, user),
        )
    if order.get("status") != PACKABLE:
        label = STATUS_LABELS.get(order.get("status") or "", order.get("status") or "")
        return ScanResult(
            "warning",
            f"Заказ {number} в статусе «{label}» — собирать его рано. "
            "Собираются заказы со статусом «Отправьте заказ».",
            action="wrong_status",
            sound="error",
            state=load_state(account, user),
        )
    if store.claim_is_active(order.get("claim_at")) and order.get("claim_user_id") != user["id"]:
        return ScanResult(
            "error",
            f"Заказ {number} уже собирает {order.get('claim_login')}.",
            action="locked",
            sound="error",
            state=load_state(account, user),
        )

    now = db.now_iso()
    with db.write() as conn:
        _release_previous(conn, user, keep=(account["id"], order_id))
        conn.execute(
            "UPDATE avito_orders SET claim_user_id = ?, claim_login = ?, claim_at = ? "
            "WHERE account_id = ? AND id = ?",
            (user["id"], user["login"], now, account["id"], order_id),
        )
        _save_state(conn, account, user, order_id, [])
        db.log_event("avito_pack_start", account_id=account["id"], user=user,
                     posting_number=order_id, barcode=code, message="Заказ взят в сборку", conn=conn)

    state = load_state(account, user)
    if not state["total"]:
        return ScanResult(
            "warning",
            f"Заказ {number} открыт, но состав не загружен. Обновите заказы из Avito.",
            action="opened",
            sound="error",
            state=state,
        )
    return ScanResult(
        "ok",
        f"Заказ {number}: отсканируйте штрихкоды товаров — нужно {state['total']} шт.",
        action="order_opened",
        sound="ok",
        state=state,
    )


def _scan_item(account: dict, user: dict, state: dict, code: str) -> ScanResult:
    """Штрихкод товара: сверять не с чем, поэтому записываем как есть."""
    order = state["active"]
    order_id = order["id"]

    if state["complete"]:
        with db.write() as conn:
            db.log_event("avito_scan_extra", level="warn", account_id=account["id"], user=user,
                         posting_number=order_id, barcode=code, message="Лишний скан", conn=conn)
            report.record_error(conn, account, user, "extra_product",
                                posting_number=order_id, barcode=code)
        return ScanResult(
            "warning",
            f"Все {state['total']} шт уже отсканированы. Отсканируйте стикер, чтобы закрыть заказ.",
            action="extra_product",
            sound="error",
            state=state,
        )

    index = state["done"]
    units = _units(account["id"], order_id)
    if index >= len(units):
        # Состав заказа перечитывается из базы, а счётчик сканов — из состояния
        # сборщика: синхронизация могла убрать позицию между сканами. Раньше
        # это был IndexError и белый экран прямо в руках у сборщика.
        db.log_event("avito_scan_stale", level="warn", account_id=account["id"], user=user,
                     posting_number=order_id, barcode=code, message="Состав заказа изменился")
        return ScanResult(
            "error",
            "Состав заказа изменился — откройте заказ заново (отсканируйте стикер).",
            action="stale_order",
            sound="error",
            state=state,
        )
    item, unit_no = units[index]
    scanned = list(state["scanned"]) + [code]

    with db.write() as conn:
        _save_state(conn, account, user, order_id, scanned)
        db.log_event("avito_scan_item", account_id=account["id"], user=user, posting_number=order_id,
                     barcode=code, message=f"{len(scanned)}/{state['total']}", conn=conn)
        # Отчёт: сошлась пара «штрихкод -> отправление», её и записываем.
        report.record_shipped(
            conn, account, user, order_id,
            {"sku": None, "offer_id": item.get("seller_id"), "name": item.get("title"),
             "key": f"av:{item.get('avito_id')}"},
            unit_no, code,
        )

    new_state = load_state(account, user)
    if new_state["complete"]:
        return complete(account, user, order_id, code, auto=True)
    return ScanResult(
        "ok",
        f"Записан штрихкод {code}. Собрано {new_state['done']} из {new_state['total']}.",
        action="item_scanned",
        state=new_state,
    )


def complete(account: dict, user: dict, order_id: str, code: str | None = None,
             auto: bool = False) -> ScanResult:
    now = db.now_iso()
    with db.write() as conn:
        conn.execute(
            "UPDATE avito_orders SET local_state = 'packed', packed_at = ?, packed_by = ?, "
            "claim_user_id = NULL, claim_login = NULL, claim_at = NULL "
            "WHERE account_id = ? AND id = ?",
            (now, user["login"], account["id"], order_id),
        )
        db.log_event("avito_pack_complete", account_id=account["id"], user=user,
                     posting_number=order_id, barcode=code, message="Заказ собран", conn=conn)
        clear_state(user, conn)
    row = db.query_one("SELECT marketplace_id FROM avito_orders WHERE account_id = ? AND id = ?",
                       (account["id"], order_id))
    number = (row["marketplace_id"] if row and row["marketplace_id"] else order_id)
    tail = " Осталось отправить заказ в Avito." if auto else ""
    return ScanResult(
        "ok",
        f"Готово: заказ {number} собран.{tail}",
        action="completed",
        sound="done",
        completed_order=order_id,
        state=load_state(account, user),
    )
