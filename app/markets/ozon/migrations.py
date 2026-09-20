"""Разовые правки базы для возвратов Ozon — история, которая должна пережить обновление.

Каждая правка помечает себя в kv и второй раз не запускается. Порядок
вызовов в migrate() — тот же, в каком они появлялись в панели.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

from ...core.db import _parsed_moment, _table_exists, now_iso
from ...core.store import local_day

log = logging.getLogger("ozon.migrations")


KV_AUTO_ACTS_CLEANED = "auto_return_acts_cleaned"


KV_DAYS_FIXED = "received_days_repaired"


KV_CHANGED_FILLED = "status_changed_backfilled"


KV_SPARES_RELEASED = "unreceived_released_from_acts"


KV_ARRIVED_FILLED = "arrived_moment_backfilled"


# Число, каким его выбирают в календаре. Всё, что на это не похоже, в акт не
# попадёт ни при каком выборе даты.
_DAY_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _drop_auto_return_acts(conn: sqlite3.Connection) -> None:
    """Убрать акты возвратов, которые версия 1.17 составляла сама.

    В 1.17 акт собирался при обновлении, как только возврат переходил в статус
    «Получен». Вместе с этим статусом Ozon отдаёт весь архив полученных
    возвратов, а момент получения панель ставила свой — «сейчас». В итоге
    первое же обновление сваливало в один акт всю историю: на живом складе
    вышел акт на 2446 позиций за одну минуту.

    С 1.18 акт составляет человек за выбранное число, и такие акты не
    создаются. Оставшиеся от 1.17 убираем: подтверждать разом тысячи возвратов
    никто не станет, а висящий акт закрывает собой настоящие.

    Отметки сборщика при этом не теряются — они лежат в строках возвратов, а не
    в акте. Строку с отметкой из акта не освобождаем: по ней работа шла, и
    решать её судьбу должен человек. Подтверждённые акты не трогаем вовсе:
    работа по ним закрыта.
    """
    if not _table_exists(conn, "return_acts"):
        return
    done = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_AUTO_ACTS_CLEANED,)).fetchone()
    if done:
        return
    rows = conn.execute(
        "SELECT id FROM return_acts WHERE kind = 'received' AND confirmed_at IS NULL"
    ).fetchall()
    for row in rows:
        # Возврат без отметки возвращается в работу, и вместе с ним сбрасывается
        # выдуманный момент получения: следующее обновление возьмёт настоящий у
        # площадки, и возврат встанет на своё число, а не на день обновления.
        conn.execute(
            "UPDATE returns SET act_id = NULL, received_at = NULL, received_day = NULL "
            "WHERE act_id = ? AND mark IS NULL AND note IS NULL",
            (row["id"],),
        )
        left = conn.execute(
            "SELECT COUNT(*) AS c FROM returns WHERE act_id = ?", (row["id"],)
        ).fetchone()["c"]
        if not left:
            conn.execute("DELETE FROM return_acts WHERE id = ?", (row["id"],))
    if rows:
        log.info("Убрано автоматических актов возвратов: %d", len(rows))
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KV_AUTO_ACTS_CLEANED, now_iso()),
    )


def _repair_received_days(conn: sqlite3.Connection) -> None:
    """Пересчитать число получения у возвратов, которые ещё ждут акта.

    Число считалось из `visual.change_moment` — это последняя смена статуса.
    Она бывает позже самого получения и уже в других сутках, а бывает и в
    виде, который не разобрать: тогда в `received_day` оседала сама строка, а
    такого «числа» нет ни в одном календаре. Возврат при этом уже ушёл из
    «К выдаче»: получен — и в акт не попадает ни при каком выборе даты. Ровно
    так один возврат из семи и пропал.

    Теперь момент берётся из `final_moment` — «прибыл на фулфилмент или выдан
    продавцу». Пересчитываем по нему всё, что ещё не в акте, — возвраты одной
    поездки снова сходятся на одном числе. Если момент не разобрать совсем,
    снимаем и его, и число: следующее обновление проставит их заново.

    Строки с актом не трогаем: там работа уже идёт, и переносить её в другой
    акт нельзя — это была бы вторая отметка на ту же работу.
    """
    if not _table_exists(conn, "returns"):
        return
    done = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_DAYS_FIXED,)).fetchone()
    if done:
        return

    fixed = 0
    rows = conn.execute(
        "SELECT id, account_id, received_at, received_day, final_moment FROM returns "
        "WHERE received_at IS NOT NULL AND act_id IS NULL"
    ).fetchall()
    for row in rows:
        moment = _parsed_moment(row["final_moment"]) or _parsed_moment(row["received_at"])
        day = local_day(moment) if moment else ""
        if not _DAY_SHAPE.fullmatch(day):
            conn.execute(
                "UPDATE returns SET received_at = NULL, received_day = NULL "
                "WHERE account_id = ? AND id = ?",
                (row["account_id"], row["id"]),
            )
            fixed += 1
            continue
        if moment == row["received_at"] and day == row["received_day"]:
            continue
        conn.execute(
            "UPDATE returns SET received_at = ?, received_day = ? WHERE account_id = ? AND id = ?",
            (moment, day, row["account_id"], row["id"]),
        )
        fixed += 1
    if fixed:
        log.info("Пересчитано чисел получения у возвратов: %d", fixed)
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KV_DAYS_FIXED, now_iso()),
    )


def _fill_status_changed(conn: sqlite3.Connection) -> None:
    """Заполнить момент смены статуса у строк, записанных до этой колонки.

    Он лежит в сохранённом ответе площадки (`raw`), просто раньше не выносился
    отдельным полем. Достаём оттуда, а заодно проставляем число актам «без
    статуса»: их заголовок берёт его вместо времени составления, иначе
    «Возвраты за 16.09 11:42» читается как «получены 16.09 в 11:42», хотя
    11:42 — это лишь когда панель завела акт.
    """
    if not _table_exists(conn, "returns"):
        return
    done = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_CHANGED_FILLED,)).fetchone()
    if done:
        return

    filled = 0
    for row in conn.execute(
        "SELECT account_id, id, raw FROM returns WHERE status_changed_at IS NULL AND raw IS NOT NULL"
    ).fetchall():
        try:
            visual = (json.loads(row["raw"]) or {}).get("visual") or {}
        except (TypeError, ValueError):
            continue
        moment = _parsed_moment(visual.get("change_moment"))
        if not moment:
            continue
        conn.execute(
            "UPDATE returns SET status_changed_at = ? WHERE account_id = ? AND id = ?",
            (moment, row["account_id"], row["id"]),
        )
        filled += 1

    stamped = 0
    if _table_exists(conn, "return_acts"):
        for act in conn.execute(
            "SELECT id, account_id FROM return_acts WHERE kind = 'nosheet' AND received_day IS NULL"
        ).fetchall():
            moment = conn.execute(
                "SELECT MAX(status_changed_at) AS moment FROM returns WHERE act_id = ?", (act["id"],)
            ).fetchone()["moment"]
            day = local_day(moment) if moment else ""
            if len(day) != 10:
                continue
            # Номер за число идёт вместе с числом: без него заголовок теряет «№N».
            seq = conn.execute(
                "SELECT COUNT(*) AS c FROM return_acts WHERE account_id = ? AND received_day = ?",
                (act["account_id"], day),
            ).fetchone()["c"] + 1
            conn.execute(
                "UPDATE return_acts SET received_day = ?, day_seq = ? WHERE id = ?",
                (day, seq, act["id"]),
            )
            stamped += 1
    if filled or stamped:
        log.info("Момент смены статуса заполнен у %d возвратов, дат у актов: %d", filled, stamped)
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KV_CHANGED_FILLED, now_iso()),
    )


def _fill_arrived(conn: sqlite3.Connection) -> None:
    """Достать из сохранённого ответа дату готовности к выдаче и число получения.

    Колонка arrived_at появилась позже, а данные для неё уже лежали в `raw`.
    Заодно правим число получения: оно писалось один раз и залипало. Статус
    сменился 18-го, а выдали 19-го — возврат так и оставался за 18-м, сколько
    ни обновляй. Берём final_moment: это сам момент выдачи.

    Строки из подтверждённых актов не трогаем — там работа закрыта.
    """
    if not _table_exists(conn, "returns"):
        return
    done = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_ARRIVED_FILLED,)).fetchone()
    if done:
        return

    arrived = fixed = 0
    # Строки без сохранённого ответа тоже нужны: даты готовности у них не
    # будет, а залипшее число получения починить надо и им.
    for row in conn.execute(
        "SELECT account_id, id, raw, final_moment, received_at, received_day, act_id FROM returns"
    ).fetchall():
        try:
            body = json.loads(row["raw"] or "{}") or {}
        except (TypeError, ValueError):
            body = {}
        moment = _parsed_moment(((body.get("storage") or {}).get("arrived_moment")))
        if moment:
            conn.execute(
                "UPDATE returns SET arrived_at = ? WHERE account_id = ? AND id = ?",
                (moment, row["account_id"], row["id"]),
            )
            arrived += 1

        if not row["received_at"]:
            continue
        handover = _parsed_moment(row["final_moment"])
        if not handover or handover == row["received_at"]:
            continue
        locked = conn.execute(
            "SELECT 1 FROM return_acts WHERE id = ? AND confirmed_at IS NOT NULL", (row["act_id"],)
        ).fetchone() if row["act_id"] else None
        if locked:
            continue
        day = local_day(handover)
        if len(day) != 10:
            continue
        conn.execute(
            "UPDATE returns SET received_at = ?, received_day = ? WHERE account_id = ? AND id = ?",
            (handover, day, row["account_id"], row["id"]),
        )
        fixed += 1
        # Число переехало — из акта за прежнее возврат надо отпустить, иначе
        # в акт за своё число он не попадёт: место уже занято. Отмеченный
        # остаётся: отметку ставил сборщик, держа возврат в руках.
        conn.execute(
            "UPDATE returns SET act_id = NULL WHERE account_id = ? AND id = ? AND mark IS NULL "
            "AND act_id IN (SELECT id FROM return_acts WHERE confirmed_at IS NULL "
            "AND received_day IS NOT NULL AND received_day <> ?)",
            (row["account_id"], row["id"], day),
        )
    if fixed:
        # Акт, из которого так забрали всё, остаётся пустой строкой на экране.
        conn.execute(
            "DELETE FROM return_acts WHERE confirmed_at IS NULL "
            "AND id NOT IN (SELECT DISTINCT act_id FROM returns WHERE act_id IS NOT NULL)"
        )
    if arrived or fixed:
        log.info("Дата готовности к выдаче заполнена у %d возвратов, чисел получения поправлено: %d",
                 arrived, fixed)
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KV_ARRIVED_FILLED, now_iso()),
    )


def _release_unreceived(conn: sqlite3.Connection) -> None:
    """Убрать из неподтверждённых актов то, что ещё не получено.

    Версия 1.26.1 сметала в акт «без статуса» любой возврат, ушедший из выдачи
    без статуса «Получен», — чтобы строка не исчезла с экрана молча. Но так в
    акт приёмки попадал и возврат, который ещё едет к продавцу: удалишь акт, а
    обновление кладёт его обратно. Принимать то, чего нет на складе, нельзя.

    Освобождаем только строки без момента получения и без отметки сборщика:
    по отмеченным работа уже шла, их судьбу решает человек. Подтверждённые
    акты не трогаем вовсе — там работа закрыта.
    """
    if not (_table_exists(conn, "returns") and _table_exists(conn, "return_acts")):
        return
    done = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_SPARES_RELEASED,)).fetchone()
    if done:
        return
    freed = conn.execute(
        "UPDATE returns SET act_id = NULL WHERE received_at IS NULL "
        "AND mark IS NULL AND note IS NULL AND act_id IN "
        "(SELECT id FROM return_acts WHERE kind = 'nosheet' AND confirmed_at IS NULL)"
    ).rowcount or 0
    dropped = conn.execute(
        "DELETE FROM return_acts WHERE kind = 'nosheet' AND confirmed_at IS NULL "
        "AND id NOT IN (SELECT DISTINCT act_id FROM returns WHERE act_id IS NOT NULL)"
    ).rowcount or 0
    if freed or dropped:
        log.info("Из актов «без статуса» освобождено возвратов: %d, убрано пустых актов: %d",
                 freed, dropped)
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KV_SPARES_RELEASED, now_iso()),
    )


def migrate(conn: sqlite3.Connection) -> None:
    """Все разовые правки, в историческом порядке."""
    _drop_auto_return_acts(conn)
    _repair_received_days(conn)
    _fill_status_changed(conn)
    _release_unreceived(conn)
    _fill_arrived(conn)
