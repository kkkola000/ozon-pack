"""Акты получения возвратов.

Акт — это один напечатанный лист возвратов: с ним съездили в пункт выдачи,
забрали товар и проверили его. Пока по каждой строке акта не поставят отметку
и акт не подтвердят, он висит во вкладке «Ждёт подтверждения».

Зачем это нужно. Забранный возврат Ozon перестаёт отдавать как «В пункте
выдачи», и раньше строка просто пропадала с экрана — ровно в тот момент, когда
сборщик заканчивал проверку и шёл записать результат. Акт держит строку до
подтверждения, и отметку есть куда поставить.

Акт закрепляется за строкой **первой** печатью и больше не меняется: иначе
повторная печать листа переписывала бы прошлые поездки задним числом.
"""
from __future__ import annotations

import uuid

from . import db, store

# Таблицы строк, которые попадают в акт. Ключ — площадка, как в /api/returns/mark.
ROW_TABLES = {"ozon": "returns", "avito": "avito_orders"}

# Возврат забрали, а листа на него не печатали: такие собираем в акт за день,
# иначе они пропадут молча — то есть ровно так, как было до актов.
NO_SHEET = "nosheet"


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def create(user: dict, *, kind: str = "account", account_id: int | None = None, conn=None) -> str:
    """Завести акт. Возвращает его id."""
    act_id = _new_id()
    sql = (
        "INSERT INTO return_acts(id, created_at, created_by, kind, account_id) VALUES(?,?,?,?,?)"
    )
    params = (act_id, db.now_iso(), user.get("login"), kind, account_id)
    if conn is not None:
        conn.execute(sql, params)
    else:
        db.execute(sql, params)
    return act_id


def attach(act_id: str, table: str, rows: list[dict], *, conn=None) -> int:
    """Привязать к акту строки, у которых акта ещё нет.

    Уже привязанные не трогаем: возврат остаётся в том акте, по которому за ним
    поехали в первый раз.
    """
    rows = [row for row in rows if not row.get("act_id")]
    if not rows:
        return 0
    pairs = ",".join("(?,?)" for _ in rows)
    params: list = [act_id]
    for row in rows:
        params += [row["account_id"], row["id"]]
    sql = f"UPDATE {table} SET act_id = ? WHERE act_id IS NULL AND (account_id, id) IN ({pairs})"
    cursor = conn.execute(sql, params) if conn is not None else db.execute(sql, params)
    return cursor.rowcount or 0


def open_sheet_act(user: dict, ozon_rows: list[dict], avito_rows: list[dict], *,
                   kind: str, account_id: int | None) -> str | None:
    """Печать листа заводит акт — но только если в нём есть что-то новое.

    Перепечатали тот же лист, ничего не добавив, — нового акта не появляется:
    пустые акты во вкладке только мешали бы отличать поездки друг от друга.
    """
    act_id = _new_id()
    with db.write() as conn:
        taken = (attach(act_id, "returns", ozon_rows, conn=conn)
                 + attach(act_id, "avito_orders", avito_rows, conn=conn))
        if not taken:
            return None
        conn.execute(
            "INSERT INTO return_acts(id, created_at, created_by, kind, account_id) VALUES(?,?,?,?,?)",
            (act_id, db.now_iso(), user.get("login"), kind, account_id),
        )
    return act_id


def collect_orphans(account_id: int, ids: list[str], *, table: str = "returns") -> str | None:
    """Возвраты забрали, а листа на них не печатали — собрать в акт за день.

    Такое бывает, когда за возвратами съездили без листа или напечатали его до
    обновления версии. Без акта они исчезли бы с экрана незаметно.
    """
    if not ids:
        return None
    today = db.now_iso()[:10]
    with db.write() as conn:
        row = conn.execute(
            "SELECT id FROM return_acts WHERE kind = ? AND account_id = ? "
            "AND confirmed_at IS NULL AND substr(created_at, 1, 10) = ?",
            (NO_SHEET, account_id, today),
        ).fetchone()
        act_id = row["id"] if row else _new_id()
        if not row:
            conn.execute(
                "INSERT INTO return_acts(id, created_at, created_by, kind, account_id) VALUES(?,?,?,?,?)",
                (act_id, db.now_iso(), None, NO_SHEET, account_id),
            )
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE {table} SET act_id = ? WHERE act_id IS NULL AND account_id = ? "
            f"AND id IN ({placeholders})",
            [act_id, account_id] + ids,
        )
    return act_id


def get(act_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM return_acts WHERE id = ?", (act_id,))
    return dict(row) if row else None


def rows_of(act_id: str) -> tuple[list[dict], list[dict]]:
    """Строки акта: возвраты Ozon и заказы Avito."""
    ozon = [
        store.return_view(row) for row in db.query(
            "SELECT r.*, a.title AS account_title FROM returns r "
            "LEFT JOIN accounts a ON a.id = r.account_id "
            "WHERE r.act_id = ? ORDER BY (r.place_name IS NULL), r.place_name, a.title, r.product_name",
            (act_id,),
        )
    ]
    avito = [
        store.avito_view(row) for row in db.query(
            "SELECT o.*, a.title AS account_title FROM avito_orders o "
            "LEFT JOIN accounts a ON a.id = o.account_id WHERE o.act_id = ? ORDER BY a.title, o.id",
            (act_id,),
        )
    ]
    return ozon, avito


def _summary(act: dict, ozon: list[dict], avito: list[dict]) -> dict:
    rows = ozon + avito
    marked_ok = sum(1 for row in rows if row.get("mark") == "ok")
    marked_bad = sum(1 for row in rows if row.get("mark") == "bad")
    unmarked = len(rows) - marked_ok - marked_bad
    # Возврат «получен», когда площадка перестала отдавать его к выдаче. Пока
    # ни один не получен — за возвратами ещё не съездили.
    received = sum(
        1 for row in rows
        if (row.get("is_ready") == 0 if "is_ready" in row else row.get("status") != "on_return")
    )
    return {
        **act,
        "rows": rows,
        "ozon": ozon,
        "avito": avito,
        "total": len(rows),
        "marked_ok": marked_ok,
        "marked_bad": marked_bad,
        "unmarked": unmarked,
        "received": received,
        "percent": round((len(rows) - unmarked) / len(rows) * 100) if rows else 0,
        "can_confirm": bool(rows) and unmarked == 0,
        "created_local": store.local_time(act.get("created_at")),
        "confirmed_local": store.local_time(act.get("confirmed_at")),
        "title": f"Возвраты за {store.local_time(act.get('created_at'))}",
        "no_sheet": act.get("kind") == NO_SHEET,
    }


def pending(account_ids: list[int] | None = None) -> list[dict]:
    """Неподтверждённые акты, свежие сверху, вместе со своими строками.

    Акты «по всем кабинетам» показываются в любом кабинете: лист был общий,
    и подтверждать его логично там же, где на него смотрят.
    """
    acts = db.query(
        "SELECT * FROM return_acts WHERE confirmed_at IS NULL ORDER BY created_at DESC"
    )
    result = []
    for act in acts:
        act = dict(act)
        if (account_ids is not None and act["kind"] != "all"
                and act["account_id"] not in account_ids):
            continue
        ozon, avito = rows_of(act["id"])
        if not ozon and not avito:
            # Строки удалили (сменили статусы возвратов, почистили базу) —
            # показывать пустой акт незачем.
            continue
        result.append(_summary(act, ozon, avito))
    return result


def pending_count(account_ids: list[int] | None = None) -> int:
    return len(pending(account_ids))


def detail(act_id: str) -> dict | None:
    act = get(act_id)
    if not act:
        return None
    ozon, avito = rows_of(act_id)
    return _summary(act, ozon, avito)


def confirm(act_id: str, user: dict) -> dict:
    """Подтвердить акт. Возвращает {'status', 'message'}.

    Подтвердить можно только полностью отмеченный акт: иначе половина строк
    закроется без решения и никто об этом не узнает.
    """
    act = detail(act_id)
    if not act:
        return {"status": "error", "message": "Акт не найден"}
    if act["confirmed_at"]:
        return {"status": "warning", "message": f"Акт уже подтвердил {act['confirmed_by'] or '—'}"}
    if act["unmarked"]:
        return {
            "status": "error",
            "message": f"Сначала отметьте все возвраты акта — без отметки осталось {act['unmarked']}.",
        }
    db.execute(
        "UPDATE return_acts SET confirmed_at = ?, confirmed_by = ? WHERE id = ?",
        (db.now_iso(), user["login"], act_id),
    )
    db.log_event(
        "return_act_confirm", account_id=act.get("account_id"), user=user,
        message=f"{act['title']}: {act['total']} поз., принято {act['marked_ok']}, "
                f"не принято {act['marked_bad']}",
    )
    return {"status": "ok", "message": f"{act['title']}: акт подтверждён"}
