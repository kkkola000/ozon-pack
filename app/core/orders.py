"""Общий список заказов: что лежит в работе во всех кабинетах сразу.

Это очередь склада, а не выписка по кабинету: сборщик стоит у одного стола и
берёт заказы Ozon, Avito и Маркета подряд. Раньше для этого переключали
кабинеты по очереди и смотрели «Заказы» каждого.

Порядок сквозной — сначала просроченное, потом горящее, внутри у кого срок
ближе. Магазин в сортировке не участвует: собирают не «сначала свой кабинет»,
а сначала то, что горит. На этот же порядок опирается сборка, когда один и тот
же товар нужен в нескольких кабинетах: берётся первый по списку.

Список собирается из того же объявления, что и раздел «Заказы»
(OrdersBoard): статус склада, срок, номер и товары строкой — одним запросом
на площадку. Поэтому «в работе» здесь и «Ожидает сборки / отгрузки» в
«Заказах» — одно и то же, а собранный — «Собран» в обоих местах. Площадок по
именам тут нет.
"""
from __future__ import annotations

from . import db
from .store import local_time, urgency

# Порядок срочности: сначала то, что уже просрочено, потом то, что горит.
URGENCY_ORDER = {"overdue": 0, "urgent": 1, "soon": 2, "ok": 3, "none": 4}


# ------------------------------------------------------------------ для площадок
def marks(values) -> str:
    """Знаки вопроса под IN (...): «?,?,?» по числу кабинетов."""
    return ",".join("?" for _ in values)


def goods_column(items_table: str, *, on: str, name: str = "name") -> str:
    """Подзапрос «товары строкой»: «Кофе зерновой ×2 · Чайник электрический».

    У каждой площадки своя таблица позиций и своё имя колонки с названием, но
    склеиваются они одинаково — иначе в общем списке одна строка выглядела бы
    не так, как соседняя.
    """
    return (
        f"(SELECT GROUP_CONCAT(CASE WHEN i.quantity > 1 "
        f"THEN i.{name} || ' ×' || i.quantity ELSE i.{name} END, ' · ') "
        f"FROM {items_table} i WHERE i.account_id = o.account_id AND {on}) AS goods"
    )


def row(account_id: int, number, *, goods: str | None, quantity, deadline: str | None,
        status_label: str, in_work: bool) -> dict:
    """Строка общего списка. Срок и срочность считаются здесь — одинаково для всех.

    «В работе» — то, с чем сборщику ещё что-то делать. Собранное из списка не
    исчезает, но по умолчанию скрыто галочкой.
    """
    return {
        "account_id": account_id,
        "number": number,
        "goods": goods or "",
        "quantity": quantity or 0,
        "deadline": deadline,
        "deadline_local": local_time(deadline),
        "urgency": urgency(deadline),
        "status_label": status_label,
        "in_work": in_work,
    }


def everywhere(limit: int = 300) -> list[dict]:
    """Заказы всех включённых кабинетов — одной очередью, срочное сверху.

    Возвращает строки для таблицы: к полям площадки добавлены название
    магазина и площадка. Сортировка сквозная: сначала просроченное, потом то,
    что горит, внутри — у кого срок ближе. Магазин в этом порядке не участвует
    намеренно: сборка объединена, кабинет в шапке ничего не решает, и делить
    список на «мою работу» и «чужую» больше не по чему. Заодно это и делает
    осмысленным правило «одинаковый товар — берём первый по списку».
    """
    from . import board

    live = board.shops()
    names = {account["id"]: account["title"] for account in live}
    rows: list[dict] = []
    for market, declared, ids in board.groups(live):
        deadline = declared.deadline_sql
        found = db.query(
            f"SELECT o.account_id AS account_id, {declared.number_sql} AS number, "
            f"{deadline} AS deadline, o.items_count AS quantity, {declared.status_sql} AS board, "
            f"{declared.goods_sql} "
            f"FROM {declared.table} o WHERE o.account_id IN ({marks(ids)}) "
            f"AND ({declared.status_sql}) IS NOT NULL "
            f"ORDER BY ({deadline}) IS NULL, {deadline} LIMIT ?",
            [*ids, limit],
        )
        for item in found:
            rows.append({
                **row(item["account_id"], item["number"], goods=item["goods"], quantity=item["quantity"],
                      deadline=item["deadline"], status_label=board.STATUS_TITLES[item["board"]],
                      in_work=item["board"] != "packed"),
                "shop": names.get(item["account_id"]) or "—",
                "market": market.code,
                "market_title": market.title,
            })

    rows.sort(key=lambda row: (
        URGENCY_ORDER.get(row["urgency"], 9),
        row["deadline"] or "",
        row["shop"].lower(),
        str(row["number"]),
    ))
    return rows
