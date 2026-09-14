"""Акты получения возвратов.

Акт — одна поездка в пункт выдачи: приехали, забрали, проверили. Пока по
каждой строке не поставят отметку и акт не подтвердят, он висит во вкладке
«Ждёт подтверждения»; подтверждённый уходит в «Отчёты».

Что считается фактом получения. Возврат перешёл в статус «Получен»
(ReceivedBySeller) — значит, он уже у нас. Панель замечает это при обновлении
возвратов и сводит все такие возвраты в один акт за текущие дату и время.
Актов о возвратах Ozon не отдаёт: своего документа, по которому можно было бы
собрать состав, у площадки нет, поэтому состав собирается по статусам.

Запасной путь — загрузить полученные возвраты за указанное число: обновление
могло не работать, версия панели могла быть старой, статус мог прийти позже.
Одно число, а не промежуток: акт — это поездка, а не отчётный период.

Защита от повторной загрузки. Возврат, попавший в акт, второй раз в акт не
попадёт: act_id ставится один раз и не снимается даже после подтверждения.
Момент получения (received_at) тоже пишется однократно, поэтому возврат,
который Ozon отдаёт «полученным» неделю подряд, остаётся в своём акте.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from . import db, store

# Возврат пропал из выдачи, а статус «Получен» по нему не приходил: такие
# собираем в акт за день, иначе они исчезли бы с экрана молча — то есть ровно
# так, как было до актов.
NO_SHEET = "nosheet"
# Акт собрался сам: возвраты перешли в статус «Получен».
RECEIVED = "received"
# Акт за указанное число: полученные возвраты загрузил администратор.
BY_DAY = "byday"
# Виды актов прежних версий. Новые такими не создаются, но старые ещё лежат в
# базе и должны нормально показываться и подтверждаться.
UPLOADED = "upload"
FROM_GIVEOUT = "ozon"

# Сколько акт «собирается сам» остаётся открытым для новых полученных возвратов.
# Обновление идёт раз в несколько минут, и одна поездка иначе разошлась бы по
# нескольким актам: часть возвратов площадка проводит позже остальных. Две
# поездки в один день разделены больше чем этим сроком.
MERGE_WINDOW = timedelta(hours=2)


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


# --------------------------------------------------------------- сбор актов
# Полученный возврат, который ещё можно забрать в акт. Условие одно на выборку
# состава и на подсказку с числами: иначе кнопка обещала бы одно, а акт
# собирал бы другое.
_FREE_RECEIVED = (
    "account_id = ? AND received_at IS NOT NULL "
    "AND (act_id IS NULL OR act_id IN (SELECT id FROM return_acts "
    "WHERE kind = ? AND confirmed_at IS NULL))"
)


def received_returns(account_id: int, day: str | None = None) -> list[str]:
    """Полученные возвраты кабинета, которые ещё ни в один акт не попали.

    Это и есть защита от повторной загрузки: возврат с act_id сюда не попадает
    ни при каком повторе — ни когда Ozon снова отдаёт его «полученным», ни
    когда то же число загрузили второй раз.

    Исключение — акт «без статуса»: туда возврат попал по догадке о пропаже, и
    пришедший «Получен» эту догадку заменяет. Иначе на один возврат оказалось
    бы два акта, то есть две отметки на одну работу.
    """
    sql = f"SELECT id FROM returns WHERE {_FREE_RECEIVED}"
    params: list = [account_id, NO_SHEET]
    if day:
        sql += " AND received_day = ?"
        params.append(day)
    sql += " ORDER BY received_at, id"
    return [row["id"] for row in db.query(sql, params)]


def received_days(account_id: int) -> list[dict]:
    """Числа, за которые есть незакрытые полученные возвраты, — свежие сверху."""
    return [
        dict(row) for row in db.query(
            f"SELECT received_day AS day, COUNT(*) AS count FROM returns "
            f"WHERE {_FREE_RECEIVED} AND received_day IS NOT NULL "
            f"GROUP BY received_day ORDER BY received_day DESC",
            (account_id, NO_SHEET),
        )
    ]


def from_received(account_id: int, *, user: dict | None = None,
                  day: str | None = None) -> dict:
    """Свести полученные возвраты в один акт.

    Без day — всё, что панель зарегистрировала полученным и ещё не закрыла
    актом; так работает обновление возвратов. С day — полученное за указанное
    число, так работает загрузка администратором.

    Возвращает {'status', 'message', 'act_id', 'added', 'merged'}. Ошибкой
    отсутствие возвратов не считается: обновление идёт постоянно, и «ничего
    нового» — обычное его состояние.
    """
    ids = received_returns(account_id, day)
    if not ids:
        return {
            "status": "warning",
            "message": (f"За {_day_label(day)} полученных возвратов нет — либо их ещё не "
                        "забрали, либо они уже в акте.") if day
                       else "Новых полученных возвратов нет.",
            "act_id": None,
            "added": 0,
            "merged": False,
        }

    with db.write() as conn:
        act_id, merged = _open_act(conn, account_id, day=day, user=user)
        added = _claim(conn, act_id, account_id, ids)
        if not added:
            # Возвраты разобрали между выборкой и записью — параллельное
            # обновление. Пустой акт оставлять нельзя: его нельзя удалить.
            if not merged:
                conn.execute("DELETE FROM return_acts WHERE id = ?", (act_id,))
            return {"status": "warning", "message": "Полученные возвраты уже разнесены по актам",
                    "act_id": None, "added": 0, "merged": merged}
        _drop_empty_spares(conn)

    act = detail(act_id)
    db.log_event(
        "return_act_received", account_id=account_id, user=user,
        message=f"{act['title']}: {'добавлено' if merged else 'акт создан'} {added} возвратов"
                + (f" за {day}" if day else ""),
    )
    return {
        "status": "ok",
        "act_id": act_id,
        "added": added,
        "merged": merged,
        "message": (f"{act['title']}: добавлено возвратов {added}, всего в акте {act['total']}"
                    if merged else f"{act['title']}: акт на {added} возвратов"),
    }


def _open_act(conn, account_id: int, *, day: str | None, user: dict | None) -> tuple[str, bool]:
    """Куда класть полученные возвраты: открытый акт или новый.

    Акт за число один: загрузили то же число второй раз — возвраты доедут в тот
    же акт, а не разойдутся по двум. Акт, который собрался сам, остаётся
    открытым MERGE_WINDOW: одна поездка не должна разваливаться на части.
    """
    if day:
        row = conn.execute(
            "SELECT id FROM return_acts WHERE kind = ? AND account_id = ? AND received_day = ? "
            "AND confirmed_at IS NULL",
            (BY_DAY, account_id, day),
        ).fetchone()
        if row:
            return row["id"], True
        act_id = _new_id()
        conn.execute(
            "INSERT INTO return_acts(id, created_at, created_by, kind, account_id, received_day) "
            "VALUES(?,?,?,?,?,?)",
            (act_id, db.now_iso(), (user or {}).get("login"), BY_DAY, account_id, day),
        )
        return act_id, False

    fresh = (datetime.now(timezone.utc) - MERGE_WINDOW).strftime("%Y-%m-%dT%H:%M:%S")
    row = conn.execute(
        "SELECT id FROM return_acts WHERE kind = ? AND account_id = ? AND confirmed_at IS NULL "
        "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
        (RECEIVED, account_id, fresh),
    ).fetchone()
    if row:
        return row["id"], True
    act_id = _new_id()
    now = db.now_iso()
    conn.execute(
        "INSERT INTO return_acts(id, created_at, created_by, kind, account_id, received_day) "
        "VALUES(?,?,?,?,?,?)",
        (act_id, now, (user or {}).get("login"), RECEIVED, account_id, store.local_day(now)),
    )
    return act_id, False


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
    """Подпись акта. Акт за число называем числом: его выбрал человек."""
    if act.get("kind") == BY_DAY and act.get("received_day"):
        return "Возвраты, полученные " + _day_label(act["received_day"])
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
        return f"загружены за {_day_label(act.get('received_day'))}" + (
            f", {act['created_by']}" if act.get("created_by") else "")
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
