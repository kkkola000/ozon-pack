"""Акты получения возвратов.

Акт — одна поездка в пункт выдачи: приехали, забрали, проверили. Пока по
каждой строке не поставят отметку и акт не подтвердят, он висит во вкладке
«Ждёт подтверждения»; подтверждённый уходит в «Отчёты».

Что считается фактом получения — решает площадка: у Ozon это статус «Получен»
(ReceivedBySeller), у Avito — возврат пропал из пункта выдачи. Актов о
возвратах площадки не отдают, поэтому состав собирается по их данным, а сам
акт заводит человек за выбранное число.

Число, а не промежуток: акт — это поездка, а не отчётный период. За возвратами
ездят несколько раз в день, поэтому актов за одно число бывает несколько —
каждый со своим временем составления, и в каждый попадает только то, что ещё
ни в один акт не вошло.

Защита от задвоения. Строка, попавшая в акт, второй раз в акт не попадёт:
act_id ставится один раз и не снимается даже после подтверждения.

Строки акта лежат в таблицах площадок (returns у Ozon, avito_orders у Avito);
какие это таблицы, ядро узнаёт из объявлений площадок и по именам их не знает.
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


def sources() -> list:
    """Площадки с возвратами: [(market, source)]. Реестр — лениво."""
    from ..markets import registry

    return [(market, market.returns) for market in registry.all_markets() if market.returns]


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _returns_word(count: int) -> str:
    """«1 возврат», «2 возврата», «5 возвратов» — число всегда на виду."""
    tail_100, tail_10 = count % 100, count % 10
    if 11 <= tail_100 <= 14 or tail_10 == 0 or tail_10 >= 5:
        return f"{count} возвратов"
    return f"{count} возврат" + ("" if tail_10 == 1 else "а")


def _next_seq(conn, account_id: int, day: str) -> int:
    """Номер акта за это число, по всем актам кабинета за него.

    Две поездки подряд укладываются в одну минуту, и по времени такие акты в
    списке не различить. Номер сквозной для числа, а не отдельный у каждого
    вида актов: иначе за одно число оказались бы два «акта №1».
    """
    return conn.execute(
        "SELECT COUNT(*) AS c FROM return_acts WHERE account_id = ? AND received_day = ?",
        (account_id, day),
    ).fetchone()["c"] + 1


def _drop_empty_spares(conn) -> None:
    """Акт «без статуса», из которого всё разобрали, больше не нужен."""
    for _market, source in sources():
        conn.execute(
            "DELETE FROM return_acts WHERE kind = ? AND confirmed_at IS NULL "
            f"AND id NOT IN (SELECT DISTINCT act_id FROM {source.table} WHERE act_id IS NOT NULL)",
            (NO_SHEET,),
        )


def drop_empty(account_id: int) -> int:
    """Убрать неподтверждённые акты кабинета, в которых не осталось позиций.

    Возврат уходит из акта, когда площадка передвинула число получения: акт
    составлен за 18-е, а получен возврат 19-го. Забрали так всё — от акта
    остаётся пустая строка на экране, подтверждать в ней нечего.

    Подтверждённый акт не трогаем ни при каких условиях: он уже документ.
    """
    used = " UNION ".join(
        f"SELECT DISTINCT act_id FROM {source.table} WHERE act_id IS NOT NULL" for _m, source in sources()
    ) or "SELECT NULL"
    with db.write() as conn:
        return conn.execute(
            "DELETE FROM return_acts WHERE account_id = ? AND confirmed_at IS NULL "
            f"AND id NOT IN ({used})",
            (account_id,),
        ).rowcount or 0


def from_received(source, account_id: int, day: str, *, user: dict | None = None) -> dict:
    """Свести в акт полученные возвраты за указанное число.

    Каждый вызов — отдельный акт: за возвратами ездят несколько раз в день, и
    каждая поездка подписывается отдельно. В акт попадает только то, что ещё ни
    в один акт не вошло, поэтому нажать кнопку дважды подряд не страшно —
    второй акт просто не из чего собрать.

    Что именно «получено», знает площадка (source.received), как забрать строки
    в акт — тоже (source.claim). Возвращает {'status', 'message', 'act_id', 'added'}.
    """
    if not (source.received and source.claim):
        return {"status": "error", "message": "Площадка не ведёт полученные возвраты", "act_id": None, "added": 0}
    ids = source.received(account_id, day)
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
        seq = _next_seq(conn, account_id, day)
        conn.execute(
            "INSERT INTO return_acts(id, created_at, created_by, kind, account_id, received_day, day_seq) "
            "VALUES(?,?,?,?,?,?,?)",
            (act_id, now, (user or {}).get("login"), BY_DAY, account_id, day, seq),
        )
        added = source.claim(conn, act_id, account_id, ids)
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


# ------------------------------------------------------------------- чтение
def get(act_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM return_acts WHERE id = ?", (act_id,))
    return dict(row) if row else None


def rows_of(act_id: str) -> list[tuple]:
    """Строки акта по площадкам: [(market, source, rows)] — только непустые."""
    result = []
    for market, source in sources():
        rows = source.act_rows(act_id)
        if rows:
            result.append((market, source, rows))
    return result


def _summary(act: dict, sections: list[tuple]) -> dict:
    """Акт с итогами. Строки доступны и все вместе (rows), и по коду площадки (act['ozon'])."""
    rows = [row for _m, _s, part in sections for row in part]
    marked_ok = sum(1 for row in rows if row.get("mark") == "ok")
    marked_bad = sum(1 for row in rows if row.get("mark") == "bad")
    unmarked = len(rows) - marked_ok - marked_bad
    kind = act.get("kind")
    by_market = {market.code: part for market, _s, part in sections}
    return {
        **act,
        **{market.code: [] for market, _s in sources()},
        **by_market,
        "rows": rows,
        "sections": [
            {"code": market.code, "label": source.label, "template": source.act_template, "rows": part}
            for market, source, part in sections
        ],
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
    day = act.get("received_day")
    if day:
        seq = f" №{act['day_seq']}" if act.get("day_seq") else ""
        return (f"Возвраты за {_day_label(day)}, акт{seq}"
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
        # По числу получения, а не составления: акт называют днём поездки, по
        # нему его и ищут. Время составления — только чтобы различить акты
        # одного числа и удержать в порядке те, у кого числа ещё нет.
        "SELECT * FROM return_acts WHERE confirmed_at IS NULL "
        "ORDER BY COALESCE(received_day, substr(created_at, 1, 10)) DESC, created_at DESC"
    )
    result = []
    for act in acts:
        act = dict(act)
        if (account_ids is not None and act["kind"] != "all"
                and act["account_id"] not in account_ids):
            continue
        sections = rows_of(act["id"])
        if not sections:
            # Строки удалили (сменили статусы возвратов, почистили базу) —
            # показывать пустой акт незачем.
            continue
        result.append(_summary(act, sections))
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
        f"SELECT * FROM return_acts {where} "
        f"ORDER BY COALESCE(received_day, substr(created_at, 1, 10)) DESC, confirmed_at DESC LIMIT ?",
        params + [limit],
    )
    if not acts:
        return []
    ids = [row["id"] for row in acts]
    placeholders = ",".join("?" for _ in ids)
    totals: dict[str, dict] = {act_id: {"total": 0, "ok": 0, "bad": 0} for act_id in ids}
    for _market, source in sources():
        for row in db.query(
            f"SELECT act_id, COUNT(*) AS total, "
            f"SUM(mark = 'ok') AS ok, SUM(mark = 'bad') AS bad "
            f"FROM {source.table} WHERE act_id IN ({placeholders}) GROUP BY act_id",
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
    return _summary(act, rows_of(act_id))


def progress(act_id: str) -> dict | None:
    """Счётчики акта без его строк — чтобы шапка обновилась после отметки.

    Строки уже лежат на странице, заново их отдавать незачем: нужно только
    сказать, сколько осталось без отметки и можно ли подтверждать.
    """
    act = get(act_id)
    if not act:
        return None
    total = ok = bad = 0
    for _market, source in sources():
        row = db.query_one(
            f"SELECT COUNT(*) AS total, SUM(mark = 'ok') AS ok, SUM(mark = 'bad') AS bad "
            f"FROM {source.table} WHERE act_id = ?",
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
        for _market, source in sources():
            cursor = conn.execute(
                f"UPDATE {source.table} SET act_id = NULL, mark = NULL, note = NULL, "
                f"mark_at = NULL, mark_by = NULL WHERE act_id = ?",
                (act_id,),
            )
            freed += cursor.rowcount or 0
        conn.execute("DELETE FROM return_acts WHERE id = ?", (act_id,))
    # Куда возвраты денутся дальше, зависит от того, откуда акт взялся: у акта
    # за число есть само число, а «без статуса» собирается заново обновлением.
    again = (f"составьте акт за {_day_label(act['received_day'])} заново"
             if act.get("kind") == BY_DAY and act.get("received_day") else
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
