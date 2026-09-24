"""Общий список заказов: что лежит в работе во всех кабинетах сразу.

На рабочем месте сборщик видит очередь только текущего кабинета — и это верно:
собирает он по одному. Но знать, что происходит в соседнем магазине, ему нужно
постоянно: там может гореть срок, пока здесь пусто. Раньше для этого
переключали кабинеты по очереди и смотрели «Заказы» каждого.

Список собирается из объявлений площадок: каждая отдаёт свои заказы уже
приведёнными к общему виду (Market.orders_feed), а здесь они только
складываются в одну стопку и сортируются. Площадок по именам тут нет.
"""
from __future__ import annotations

from . import accounts
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


def _registry():
    """Реестр площадок — лениво: ядро не тянет их при загрузке."""
    from ..markets import registry

    return registry


def everywhere(current: dict | None = None, limit: int = 300) -> list[dict]:
    """Заказы всех включённых кабинетов, свой магазин — первым.

    Возвращает строки для таблицы: к полям площадки добавлены название
    магазина, площадка и признак «это текущий кабинет». Сортировка: свои
    заказы сверху, дальше магазины по алфавиту, внутри магазина — у кого
    срок ближе. Так список читается как «вот моя работа, а вот чужая».
    """
    active = accounts.all_accounts(active_only=True)
    by_id = {account["id"]: account for account in active}
    current_id = (current or {}).get("id")

    rows: list[dict] = []
    for market in _registry().all_markets():
        if not market.orders_feed:
            continue
        ids = [account["id"] for account in active if account["marketplace"] == market.code]
        if not ids:
            continue
        for row in market.orders_feed(ids, limit=limit):
            account = by_id.get(row["account_id"]) or {}
            rows.append({
                **row,
                "shop": account.get("title") or "—",
                "market": market.code,
                "market_title": market.title,
                "own": row["account_id"] == current_id,
            })

    rows.sort(key=lambda row: (
        not row["own"],
        row["shop"].lower(),
        URGENCY_ORDER.get(row["urgency"], 9),
        row["deadline"] or "",
        str(row["number"]),
    ))
    return rows
