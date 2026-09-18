"""Акты получения возвратов.

Акт — одна поездка в пункт выдачи: приехали, забрали, проверили. Пока по
каждой строке не поставят отметку и акт не подтвердят, он висит во вкладке
«Ждёт подтверждения»; подтверждённый уходит в «Отчёты».

Что считается фактом получения. Возврат перешёл в статус «Получен»
(ReceivedBySeller) — значит, он уже у нас. Актов о возвратах Ozon не отдаёт:
своего документа, по которому можно было бы собрать состав, у площадки нет,
поэтому состав собирается по статусам.

Акт составляет человек: выбирает число и нажимает «Составить акт». Панель не
знает, когда поездка закончилась, — возвраты переходят в «Получен» по одному,
растянуто во времени, и любой срок, через который «акт считается закрытым»,
был бы выдумкой.

Число, а не промежуток: акт — это поездка, а не отчётный период. За возвратами
ездят несколько раз в день, поэтому актов за одно число бывает несколько —
каждый со своим временем составления, и в каждый попадает только то, что ещё
ни в один акт не вошло.

Защита от задвоения. Возврат, попавший в акт, второй раз в акт не попадёт:
act_id ставится один раз и не снимается даже после подтверждения. Момент
получения (received_at) тоже пишется однократно, поэтому возврат, который Ozon
отдаёт «полученным» неделю подряд, остаётся в своём акте.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from . import db, store

# Возврат пропал из выдачи, а статус «Получен» по нему не приходил: такие
# собираем в акт за день, иначе они исчезли бы с экрана молча — то есть ровно
# так, как было до актов.
NO_SHEET = "nosheet"
# Акт за указанное число: полученные возвраты свёл в акт человек.
BY_DAY = "byday"
# Виды актов прежних версий. Новые такими не создаются, но старые ещё лежат в
# базе и должны нормально показываться и подтверждаться.
RECEIVED = "received"
UPLOADED = "upload"
FROM_GIVEOUT = "ozon"


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _returns_word(count: int) -> str:
    """«1 возврат», «2 возврата», «5 возвратов» — число всегда на виду."""
    tail_100, tail_10 = count % 100, count % 10
    if 11 <= tail_100 <= 14 or tail_10 == 0 or tail_10 >= 5:
        return f"{count} возвратов"
    return f"{count} возврат" + ("" if tail_10 == 1 else "а")


# --------------------------------------------------------------- сбор актов
# Полученный возврат, который ещё можно забрать в акт. Одно условие и на
# предпросмотр, и на саму сборку: иначе кнопка обещала бы одно, а акт собирал
# бы другое.
_FREE_RECEIVED = (
    "account_id = ? AND received_at IS NOT NULL "
    "AND (act_id IS NULL OR act_id IN (SELECT id FROM return_acts "
    "WHERE kind = ? AND confirmed_at IS NULL))"
)


def received_returns(account_id: int, day: str) -> list[str]:
    """Полученные возвраты кабинета, которые ещё ни в один акт не попали.

    Это и есть защита от задвоения: возврат с act_id сюда не попадает ни при
    каком повторе — ни когда Ozon снова отдаёт его «полученным», ни когда то же
    число выбрали второй раз.

    Исключение — акт «без статуса»: туда возврат попал по догадке о пропаже, и
    пришедший «Получен» эту догадку заменяет. Иначе на один возврат оказалось
    бы два акта, то есть две отметки на одну работу.

    Число обязательно: акт — это поездка за конкретный день, и всё, что панель
    умеет, — отдать состав по названному числу.
    """
    return [
        row["id"] for row in db.query(
            f"SELECT id FROM returns WHERE {_FREE_RECEIVED} AND received_day = ? "
            f"ORDER BY received_at, id",
            (account_id, NO_SHEET, day),
        )
    ]


def free_days(account_id: int) -> list[dict]:
    """Числа, за которые есть полученные возвраты без акта, — свежие сверху.

    Без этого списка возврат теряется. Число берётся у площадки — это момент,
    когда она сменила статус на «Получен», — и совпадать с днём поездки оно не
    обязано: статус мог смениться под полночь или задним числом. Такой возврат
    ушёл из «К выдаче», ни в один акт не попал, а календарь наугад не
    перебирают: получен — и нигде.
    """
    return [
        {
            "day": row["received_day"],
            "count": row["c"],
            "label": _day_label(row["received_day"]),
            "word": _returns_word(row["c"]),
        }
        for row in db.query(
            f"SELECT received_day, COUNT(*) AS c FROM returns WHERE {_FREE_RECEIVED} "
            f"AND received_day IS NOT NULL GROUP BY received_day ORDER BY received_day DESC",
            (account_id, NO_SHEET),
        )
    ]


def from_received(account_id: int, day: str, *, user: dict | None = None) -> dict:
    """Свести в акт полученные возвраты за указанное число.

    Каждый вызов — отдельный акт: за возвратами ездят несколько раз в день, и
    каждая поездка подписывается отдельно. В акт попадает только то, что ещё ни
    в один акт не вошло, поэтому нажать кнопку дважды подряд не страшно —
    второй акт просто не из чего собрать.

    Возвращает {'status', 'message', 'act_id', 'added'}.
    """
    ids = received_returns(account_id, day)
    if not ids:
        return {
            "status": "warning",
            "message": f"За {_day_label(day)} полученных возвратов без акта нет — "
                       "либо их ещё не забрали, либо они уже в акте.",
            "act_id": None,
            "added": 0,
        }

    act_id = _new_id()
    now = db.now_iso()
    with db.write() as conn:
        # Номер акта за это число. Две поездки подряд могут уложиться в одну
        # минуту, и по времени такие акты в списке не различить.
        seq = conn.execute(
            "SELECT COUNT(*) AS c FROM return_acts WHERE kind = ? AND account_id = ? AND received_day = ?",
            (BY_DAY, account_id, day),
        ).fetchone()["c"] + 1
        conn.execute(
            "INSERT INTO return_acts(id, created_at, created_by, kind, account_id, received_day, day_seq) "
            "VALUES(?,?,?,?,?,?,?)",
            (act_id, now, (user or {}).get("login"), BY_DAY, account_id, day, seq),
        )
        added = _claim(conn, act_id, account_id, ids)
        if not added:
            # Возвраты разобрали между выборкой и записью: акт за то же число
            # составили в соседней вкладке. Пустой акт оставлять нельзя — его
            # потом не удалить.
            conn.execute("DELETE FROM return_acts WHERE id = ?", (act_id,))
            return {"status": "warning", "act_id": None, "added": 0,
                    "message": "Эти возвраты только что попали в другой акт"}
        _drop_empty_spares(conn)

    act = detail(act_id)
    db.log_event(
        "return_act_received", account_id=account_id, user=user,
        message=f"{act['title']}: {_returns_word(added)}",
    )
    return {
        "status": "ok",
        "act_id": act_id,
        "added": added,
        "message": f"{act['title']}: {_returns_word(added)}",
    }


def _claim(conn, act_id: str, account_id: int, return_ids: list[str]) -> int:
    """Забрать возвраты в акт. Возвращает, сколько реально переехало.

    Берём свободные и те, что лежат в неподтверждённом акте «без статуса»:
    факт получения от площадки точнее нашей догадки о пропаже. Возврат из
    любого другого акта не трогаем — это и есть защита от второго акта на ту
    же работу.
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
    """Акт «без статуса», из которого всё разобрали, больше не нужен."""
    conn.execute(
        "DELETE FROM return_acts WHERE kind = ? AND confirmed_at IS NULL "
        "AND id NOT IN (SELECT DISTINCT act_id FROM returns WHERE act_id IS NOT NULL)",
        (NO_SHEET,),
    )


def collect_orphans(account_id: int, ids: list[str], *, table: str = "returns") -> str | None:
    """Возврат пропал из выдачи, а «Получен» по нему не приходил.

    Такое бывает, когда площадка проводит возврат мимо этого статуса или он
    приходит с задержкой. Собираем такие в акт за день: без него строка
    исчезла бы с экрана незаметно, вместе с непоставленной отметкой. Придёт
    «Получен» — возврат переедет в акт получения.
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
        _drop_empty_spares(conn)
    return act_id


# ------------------------------------------------------------------- чтение
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
    kind = act.get("kind")
    return {
        **act,
        "rows": rows,
        "ozon": ozon,
        "avito": avito,
        "total": len(rows),
        "marked_ok": marked_ok,
        "marked_bad": marked_bad,
        "unmarked": unmarked,
        "percent": round((len(rows) - unmarked) / len(rows) * 100) if rows else 0,
        "can_confirm": bool(rows) and unmarked == 0,
        "created_local": store.local_time(act.get("created_at")),
        "confirmed_local": store.local_time(act.get("confirmed_at")),
        "title": _title(act),
        "source_label": _source_label(act),
        "no_sheet": kind == NO_SHEET,
        "by_day": kind == BY_DAY,
        "auto": kind == RECEIVED,
    }


def _title(act: dict) -> str:
    """Подпись акта: число получения, номер за это число и время составления.

    Номер обязателен: за одно число актов бывает несколько — ездят по разу за
    партию. Времени мало, две поездки подряд укладываются в одну минуту, и
    тогда два акта в списке не различить, а подписывают их отдельно.
    """
    if act.get("kind") == BY_DAY and act.get("received_day"):
        seq = f" №{act['day_seq']}" if act.get("day_seq") else ""
        return (f"Возвраты за {_day_label(act['received_day'])}, акт{seq}"
                f" от {store.local_time(act.get('created_at'), '%H:%M')}")
    return f"Возвраты за {store.local_time(act.get('created_at'))}"


def _day_label(day: str | None) -> str:
    try:
        return datetime.strptime(str(day), "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(day or "—")


def _source_label(act: dict) -> str:
    """Откуда акт взялся — это видно в списке и на бумаге."""
    kind = act.get("kind")
    if kind == RECEIVED:
        return "возвраты перешли в статус «Получен»"
    if kind == BY_DAY:
        return "составил " + (act.get("created_by") or "—")
    if kind == NO_SHEET:
        return "пропали из выдачи, статус «Получен» не приходил"
    if kind == UPLOADED:
        return f"загружен файлом: {act.get('giveout_id') or '—'}"
    if kind == FROM_GIVEOUT:
        return f"акт выдачи Ozon {act.get('giveout_id') or '—'}"
    return ""


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


def confirmed(account_id: int | None = None, limit: int = 200) -> list[dict]:
    """Подтверждённые акты для раздела «Отчёты» — свежие сверху.

    Строки не тянем: в списке они не нужны, а акт с сотней позиций на каждую
    строку списка — это сотня лишних запросов. Итоги считаем одним запросом.
    """
    where = "WHERE confirmed_at IS NOT NULL"
    params: list = []
    if account_id:
        where += " AND (account_id = ? OR kind = 'all')"
        params.append(account_id)
    acts = db.query(
        f"SELECT * FROM return_acts {where} ORDER BY confirmed_at DESC LIMIT ?",
        params + [limit],
    )
    if not acts:
        return []
    ids = [row["id"] for row in acts]
    placeholders = ",".join("?" for _ in ids)
    totals: dict[str, dict] = {act_id: {"total": 0, "ok": 0, "bad": 0} for act_id in ids}
    for table in ("returns", "avito_orders"):
        for row in db.query(
            f"SELECT act_id, COUNT(*) AS total, "
            f"SUM(mark = 'ok') AS ok, SUM(mark = 'bad') AS bad "
            f"FROM {table} WHERE act_id IN ({placeholders}) GROUP BY act_id",
            ids,
        ):
            counts = totals[row["act_id"]]
            counts["total"] += row["total"] or 0
            counts["ok"] += row["ok"] or 0
            counts["bad"] += row["bad"] or 0
    result = []
    for act in acts:
        act = dict(act)
        counts = totals[act["id"]]
        result.append({
            **act,
            "total": counts["total"],
            "marked_ok": counts["ok"],
            "marked_bad": counts["bad"],
            "title": _title(act),
            "source_label": _source_label(act),
            "created_local": store.local_time(act.get("created_at")),
            "confirmed_local": store.local_time(act.get("confirmed_at"), "%d.%m.%Y %H:%M"),
        })
    return result


def detail(act_id: str) -> dict | None:
    act = get(act_id)
    if not act:
        return None
    ozon, avito = rows_of(act_id)
    return _summary(act, ozon, avito)


def progress(act_id: str) -> dict | None:
    """Счётчики акта без его строк — чтобы шапка обновилась после отметки.

    Строки уже лежат на странице, заново их отдавать незачем: нужно только
    сказать, сколько осталось без отметки и можно ли подтверждать.
    """
    act = get(act_id)
    if not act:
        return None
    total = ok = bad = 0
    for table in ("returns", "avito_orders"):
        row = db.query_one(
            f"SELECT COUNT(*) AS total, SUM(mark = 'ok') AS ok, SUM(mark = 'bad') AS bad "
            f"FROM {table} WHERE act_id = ?",
            (act_id,),
        )
        total += row["total"] or 0
        ok += row["ok"] or 0
        bad += row["bad"] or 0
    unmarked = total - ok - bad
    return {
        "id": act_id,
        "total": total,
        "marked_ok": ok,
        "marked_bad": bad,
        "unmarked": unmarked,
        "percent": round((total - unmarked) / total * 100) if total else 0,
        "can_confirm": total > 0 and unmarked == 0 and not act.get("confirmed_at"),
    }


def confirm(act_id: str, user: dict) -> dict:
    """Подтвердить акт. Возвращает {'status', 'message'}.

    Подтвердить можно только полностью отмеченный акт: иначе половина строк
    закроется без решения и никто об этом не узнает. Подтверждённый акт уходит
    в «Отчёты» и обратно не возвращается.
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
    return {"status": "ok", "message": f"{act['title']}: акт подтверждён, ищите его в «Отчётах»"}


def unconfirm(act_id: str, user: dict) -> dict:
    """Вернуть подтверждённый акт в работу. Отметки остаются на местах.

    Подтвердили рано — так бывает: нашлась ещё коробка, или решение по
    возврату оказалось другим. Акт уходит из «Отчётов» обратно во вкладку
    «Ждёт подтверждения», отметки целы — править нужно одну строку, а не
    перепринимать всю поездку.
    """
    act = get(act_id)
    if not act:
        return {"status": "error", "message": "Акт не найден"}
    title = _title(act)
    if not act.get("confirmed_at"):
        return {"status": "warning", "message": f"{title}: акт и так не подтверждён"}
    db.execute(
        "UPDATE return_acts SET confirmed_at = NULL, confirmed_by = NULL WHERE id = ?", (act_id,)
    )
    db.log_event(
        "return_act_unconfirm", account_id=act.get("account_id"), user=user,
        message=f"{title}: подтверждение снято, подтверждал {act.get('confirmed_by') or '—'}",
    )
    return {"status": "ok", "message": f"{title}: акт вернулся в «Ждёт подтверждения»"}


def remove(act_id: str, user: dict) -> dict:
    """Удалить акт: возвраты освобождаются, отметки с них снимаются.

    Это «принять заново с нуля» — в отличие от снятия подтверждения, где
    отметки остаются. Возвраты снова попадут в «Составить акт» за то же
    число: момент получения — факт от площадки, его мы не трогаем.

    Отметки снимаются намеренно. Акт, собранный из уже отмеченных строк,
    подтверждается сразу, и принимать в нём нечего — а просили именно
    принять заново. В журнале отметки остаются: каждая записана отдельным
    событием, и кто что решил в прошлый раз, видно.
    """
    act = get(act_id)
    if not act:
        return {"status": "error", "message": "Акт не найден"}
    title = _title(act)
    freed = 0
    with db.write() as conn:
        for table in ("returns", "avito_orders"):
            cursor = conn.execute(
                f"UPDATE {table} SET act_id = NULL, mark = NULL, note = NULL, "
                f"mark_at = NULL, mark_by = NULL WHERE act_id = ?",
                (act_id,),
            )
            freed += cursor.rowcount or 0
        conn.execute("DELETE FROM return_acts WHERE id = ?", (act_id,))
    # Куда возвраты денутся дальше, зависит от того, откуда акт взялся: у акта
    # за число есть само число, а «без статуса» собирается заново обновлением.
    again = (f"составьте акт за {_day_label(act['received_day'])} заново"
             if act.get("received_day") else
             "они вернутся в список при ближайшем обновлении")
    db.log_event(
        "return_act_delete", account_id=act.get("account_id"), user=user,
        message=f"{title}: акт удалён, освобождено {_returns_word(freed)}, отметки сняты",
    )
    return {
        "status": "ok",
        "freed": freed,
        "message": f"{title}: акт удалён, {_returns_word(freed)} свободны — {again}",
    }
