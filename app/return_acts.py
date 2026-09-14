"""Акты получения возвратов.

Акт — одна выдача в пункте: приехали, забрали, проверили. Пока по каждой
строке не поставят отметку и акт не подтвердят, он висит во вкладке «Ждёт
подтверждения».

Составляет акт площадка. Ozon отдаёт его методами /v1/return/giveout/* со
своим составом и временем, и панель раскладывает этот акт на свои возвраты по
штрихкоду (app/giveouts.py). Забирают в пункте всё разом — и FBS, и FBO, —
поэтому один акт закрывает поездку целиком.

Зачем это нужно. Забранный возврат Ozon перестаёт отдавать как «В пункте
выдачи», и строка пропадала с экрана ровно тогда, когда сборщик заканчивал
проверку и шёл записать результат. Акт держит её до подтверждения.

Запасной путь. Возврат пропал из выдачи, а акта площадки на него ещё нет —
такой попадает в акт «без листа» за день, чтобы не исчез молча. Появится акт
Ozon с этим возвратом — строка переедет в него: документ площадки точнее, а
два акта на один возврат означали бы две отметки на одну работу.
"""
from __future__ import annotations

import uuid

from . import db, store

# Возврат забрали, а акта площадки на него ещё нет: такие собираем в акт за
# день, иначе они пропадут молча — то есть ровно так, как было до актов.
NO_SHEET = "nosheet"
# Акт, загруженный администратором файлом: у кабинета может не быть метода
# выдачи, а закрывать поездку всё равно надо.
UPLOADED = "upload"


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def from_giveout(account_id: int, giveout_id: str, *, created_at: str | None,
                 status: str | None, return_ids: list[str]) -> str | None:
    """Собрать акт из акта выдачи Ozon.

    Это основной путь: площадка сама сообщает, что и когда отдала, — состав и
    время берём у неё, а не выводим из того, что возврат пропал из выдачи.

    Возврат мог уже попасть в акт «без листа» (панель заметила пропажу раньше,
    чем появился акт площадки). Такой переносим сюда: акт площадки точнее, а
    два акта на один возврат — это две отметки на одну работу.
    """
    if not return_ids:
        return None
    with db.write() as conn:
        row = conn.execute("SELECT id FROM return_acts WHERE giveout_id = ?", (giveout_id,)).fetchone()
        act_id = row["id"] if row else _new_id()
        taken = _claim(conn, act_id, account_id, return_ids)
        if row:
            conn.execute(
                "UPDATE return_acts SET giveout_status = ?, created_at = COALESCE(?, created_at) WHERE id = ?",
                (status, created_at, act_id),
            )
        elif taken:
            conn.execute(
                "INSERT INTO return_acts(id, created_at, created_by, kind, account_id, giveout_id, giveout_status) "
                "VALUES(?,?,?,?,?,?,?)",
                (act_id, created_at or db.now_iso(), None, "ozon", account_id, giveout_id, status),
            )
        else:
            # Все возвраты акта уже разнесены по подтверждённым актам: заводить
            # пустой акт незачем.
            return None
        _drop_empty_spares(conn)
    return act_id


def _claim(conn, act_id: str, account_id: int, return_ids: list[str]) -> int:
    """Забрать возвраты в акт. Возвращает, сколько реально переехало.

    Берём свободные и те, что лежат в неподтверждённом акте «без листа»:
    документ площадки точнее нашей догадки. Возврат из подтверждённого акта не
    трогаем — работа по нему закрыта.
    """
    placeholders = ",".join("?" for _ in return_ids)
    cursor = conn.execute(
        f"""
        UPDATE returns SET act_id = ?
        WHERE account_id = ? AND id IN ({placeholders})
          AND (act_id IS NULL
               OR act_id IN (SELECT id FROM return_acts
                             WHERE kind = ? AND confirmed_at IS NULL))
        """,
        [act_id, account_id] + return_ids + [NO_SHEET],
    )
    return cursor.rowcount or 0


def _drop_empty_spares(conn) -> None:
    """Акт «без листа», из которого всё разобрали, больше не нужен."""
    conn.execute(
        "DELETE FROM return_acts WHERE kind = ? AND confirmed_at IS NULL "
        "AND id NOT IN (SELECT DISTINCT act_id FROM returns WHERE act_id IS NOT NULL)",
        (NO_SHEET,),
    )


def from_upload(account_id: int, user: dict, *, source: str, return_ids: list[str]) -> str | None:
    """Акт, загруженный администратором файлом.

    Нужен, когда метода выдачи у кабинета нет или он отвечает отказом: акт есть
    на бумаге, а через API его не видно. Возврат, уже попавший в акт «без
    листа», переносим сюда — как и при акте из API.
    """
    if not return_ids:
        return None
    act_id = _new_id()
    with db.write() as conn:
        taken = _claim(conn, act_id, account_id, return_ids)
        if not taken:
            # Все возвраты файла уже разнесены — второй акт на ту же поездку
            # означал бы второй комплект отметок на одну работу.
            return None
        conn.execute(
            "INSERT INTO return_acts(id, created_at, created_by, kind, account_id, giveout_id) "
            "VALUES(?,?,?,?,?,?)",
            (act_id, db.now_iso(), user.get("login"), UPLOADED, account_id, source[:200] or None),
        )
        _drop_empty_spares(conn)
    db.log_event(
        "return_act_upload", account_id=account_id, user=user,
        message=f"{source}: {taken} возвратов",
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
        "from_ozon": act.get("kind") == "ozon",
        "uploaded": act.get("kind") == UPLOADED,
        "source": act.get("giveout_id") or "",
        "giveout_label": _giveout_label(act),
    }


def _giveout_label(act: dict) -> str:
    """Подпись акта площадки: по ней акт сверяют с документом Ozon."""
    if act.get("kind") == UPLOADED:
        return f"загружен файлом: {act.get('giveout_id') or '—'}"
    if act.get("kind") != "ozon":
        return ""
    from . import giveouts

    status = giveouts.status_label(act.get("giveout_status") or "")
    number = act.get("giveout_id") or ""
    return f"акт Ozon {number}" + (f" · {status}" if status else "")


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
