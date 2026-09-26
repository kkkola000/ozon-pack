"""Раздел «Заказы»: заказы всех кабинетов — один список с двумя фильтрами.

Раньше у каждой площадки были свои «Заказы» — «Заказы FBS», «Заказы Avito»,
«Заказы Маркета», — и показывали они кабинет, выбранный в шапке. Чтобы увидеть,
что лежит в работе, кабинеты переключали по очереди. Теперь раздел один:

* **кабинеты** сверху — «Все заказы» или один кабинет, как на «Сборке»;
* **статус склада** под ними — «Ожидает сборки», «Ожидает отгрузки», «Собран».

Статусы у площадок свои, и в какой из трёх попадает заказ, объявляет сама
площадка (OrdersBoard.status_sql). Здесь её заказы только считаются,
выбираются, складываются в одну стопку и сортируются. Площадок по именам тут
нет, кабинета в шапке — тоже: переключатель скоро уйдёт, а раздел от него уже
не зависит.

Числа на фильтрах перекрёстные: на кабинете — сколько у него заказов в
выбранном статусе, на статусе — сколько их в выбранном кабинете. Поиск сужает
и то и другое: так видно, где нашлось.
"""
from __future__ import annotations

from . import accounts, db
from .store import local_time

# «Все заказы»: фильтр кабинета не выбран.
ALL = "all"

# Три статуса склада — одинаковые для всех площадок.
STATUSES = (
    ("packaging", "Ожидает сборки"),
    ("deliver", "Ожидает отгрузки"),
    ("packed", "Собран"),
)
STATUS_TITLES = dict(STATUSES)
FIRST = STATUSES[0][0]

# Сколько строк показывать. Числа на фильтрах считаются по всем.
LIMIT = 500


def _registry():
    """Реестр площадок — лениво: ядро не тянет их при загрузке."""
    from ..markets import registry

    return registry


def board_of(account: dict):
    """Что площадка кабинета объявила о своих заказах. None — раздела у неё нет."""
    market = _registry().get(account.get("marketplace"))
    return market.orders if market else None


# ------------------------------------------------------------------ фильтры
def shops(picked: str = ALL) -> list[dict]:
    """Кабинеты под фильтром: включённые, с ключами и с разделом заказов.

    Порядок — как в «Настройках»: в нём же идут чипы.
    """
    live = [account for account in accounts.all_accounts(active_only=True)
            if accounts.is_configured(account) and board_of(account) is not None]
    if picked == ALL:
        return live
    return [account for account in live if str(account["id"]) == str(picked)]


def filter_of(value) -> str:
    """Кабинет из адреса. Незнакомый, выключенный или без ключей — «Все заказы»."""
    wanted = str(value or ALL).strip()
    if wanted != ALL and shops(wanted):
        return wanted
    return ALL


def status_of(value) -> str:
    """Статус из адреса. Незнакомый — первый: кривая ссылка не даёт пустой экран."""
    wanted = str(value or "").strip()
    return wanted if wanted in STATUS_TITLES else FIRST


def shop(account_id) -> dict:
    """Кабинет для действия над заказом. Его называет запрос, а не шапка.

    LookupError — такого кабинета нет, он выключен или без ключей.
    """
    try:
        wanted = int(account_id)
    except (TypeError, ValueError):
        raise LookupError("Не указан кабинет заказа") from None
    found = next((account for account in shops(ALL) if account["id"] == wanted), None)
    if found is None:
        raise LookupError("Кабинет не найден или выключен")
    return found


# ------------------------------------------------------------------ запросы
def _groups(where: list[dict]) -> list[tuple]:
    """Кабинеты по площадкам: [(площадка, объявление, [номера кабинетов])]."""
    out = []
    for market in _registry().all_markets():
        if market.orders is None:
            continue
        ids = [account["id"] for account in where if account["marketplace"] == market.code]
        if ids:
            out.append((market, market.orders, ids))
    return out


def _search(board, search: str) -> tuple[str, list]:
    text = (search or "").strip()
    if not text:
        return "", []
    return f" AND {board.search_sql}", [f"%{text}%"] * board.search_sql.count("?")


def _marks(ids: list[int]) -> str:
    return ",".join("?" for _ in ids)


def counts(where: list[dict], search: str = "") -> dict[tuple[int, str], int]:
    """Сколько заказов у каждого кабинета в каждом статусе: {(кабинет, статус): n}."""
    out: dict[tuple[int, str], int] = {}
    for _market, board, ids in _groups(where):
        extra, params = _search(board, search)
        rows = db.query(
            f"SELECT o.account_id AS account_id, {board.status_sql} AS board, COUNT(*) AS c "
            f"FROM {board.table} o WHERE o.account_id IN ({_marks(ids)}){extra} "
            f"GROUP BY o.account_id, board",
            [*ids, *params],
        )
        for row in rows:
            if row["board"] in STATUS_TITLES:
                out[(row["account_id"], row["board"])] = row["c"]
    return out


def rows(where: list[dict], status: str, search: str = "", limit: int = LIMIT) -> list[dict]:
    """Заказы кабинетов в одном статусе — одной стопкой.

    В работе — сначала то, у чего срок ближе (без срока — в конце). Собранные —
    сначала свежие: это очередь на отгрузку, и последнее собранное ищут первым.
    Магазин в порядке не участвует: сборка общая.
    """
    names = {account["id"]: account.get("title") or "—" for account in where}
    out: list[dict] = []
    for market, board, ids in _groups(where):
        extra, params = _search(board, search)
        order = ("o.packed_at DESC" if status == "packed"
                 else f"({board.deadline_sql}) IS NULL, {board.deadline_sql}")
        found = db.query(
            f"SELECT o.*, {board.status_sql} AS board FROM {board.table} o "
            f"WHERE o.account_id IN ({_marks(ids)}) AND ({board.status_sql}) = ?{extra} "
            f"ORDER BY {order} LIMIT ?",
            [*ids, status, *params, limit],
        )
        for row in found:
            card = board.card(row)
            card.update({
                "account_id": row["account_id"],
                "shop": names.get(row["account_id"], "—"),
                "market": market.code,
                "market_title": market.title,
                "status": status,
                "label_word": board.label,
                "printed_word": board.printed,
                "max_labels": board.max_labels,
            })
            card.setdefault("deadline_local", local_time(card.get("deadline")))
            out.append(card)

    if status == "packed":
        out.sort(key=lambda card: card.get("packed_at") or "", reverse=True)
    else:
        out.sort(key=lambda card: (
            not card.get("deadline"), card.get("deadline") or "", card["shop"].lower(), str(card["number"]),
        ))
    return out[:limit]


# ------------------------------------------------------------------ страница
def page(picked: str, status: str, search: str = "") -> dict:
    """Всё для страницы: чипы кабинетов, статусы с числами, строки, действия."""
    everyone = shops(ALL)
    where = shops(picked)
    table = counts(everyone, search)

    def total(ids, key) -> int:
        return sum(table.get((account_id, key), 0) for account_id in ids)

    chips = [{
        "id": ALL, "title": "Все заказы", "market": None, "market_title": "",
        "count": total([account["id"] for account in everyone], status), "active": picked == ALL,
    }]
    for account in everyone:
        market = _registry().get(account["marketplace"])
        chips.append({
            "id": str(account["id"]), "title": account["title"],
            "market": market.code, "market_title": market.title,
            "count": total([account["id"]], status), "active": picked == str(account["id"]),
        })
    ids = [account["id"] for account in where]
    tabs = [{"key": key, "title": title, "count": total(ids, key), "active": key == status}
            for key, title in STATUSES]

    shown = rows(where, status, search)
    # Кнопки над списком — действия площадок, чьи заказы сейчас на экране.
    actions = []
    for market, board, _ids in _groups(where):
        for action in board.actions:
            if any(card["market"] == market.code and action.key in card.get("actions", ()) for card in shown):
                actions.append({"id": f"{market.code}:{action.key}", "title": action.title,
                                "ask": action.ask, "market_title": market.title})
    # Подписи кнопок в строках — у всех площадок сразу: строки бывают любые.
    shorts = {f"{market.code}:{action.key}": action.short
              for market in _registry().all_markets() if market.orders
              for action in market.orders.actions}
    return {
        "chips": chips,
        "tabs": tabs,
        "shorts": shorts,
        "orders": shown,
        "total": next(tab["count"] for tab in tabs if tab["active"]),
        "actions": actions,
        "where": where,
    }


def badges() -> tuple[tuple[int, str, str], ...]:
    """Значки на пункте «Заказы» в шапке — по всем кабинетам, а не по шапке."""
    table = counts(shops(ALL))
    return (
        (sum(n for (_id, key), n in table.items() if key == "packaging"), "warn", STATUS_TITLES["packaging"]),
        (sum(n for (_id, key), n in table.items() if key == "deliver"), "accent", STATUS_TITLES["deliver"]),
    )


# ------------------------------------------------------------------ отметки
def _find(account: dict, number: str):
    board = board_of(account)
    if board is None:
        raise LookupError("У этой площадки заказов в панели нет")
    row = db.query_one(
        f"SELECT * FROM {board.table} WHERE account_id = ? AND {board.key} = ?", (account["id"], number)
    )
    if row is None:
        raise LookupError(f"Заказ {number} не найден в кабинете «{account['title']}»")
    return board, row


def unmark(account: dict, user: dict, number: str, *, event: str, shown: str | None = None) -> str:
    """Снять отметку «Собран»: заказ снова в работе, бронь снята. Одинаково у всех.

    shown — как номер назвать человеку, если он не тот, что в базе (у Avito
    это номер сделки). Отменённый заказ остаётся отменённым.
    """
    board, _row = _find(account, number)
    db.execute(
        f"UPDATE {board.table} SET local_state = CASE WHEN status = 'cancelled' THEN 'cancelled' ELSE 'new' END, "
        f"packed_at = NULL, packed_by = NULL, claim_user_id = NULL, claim_login = NULL, claim_at = NULL "
        f"WHERE account_id = ? AND {board.key} = ?",
        (account["id"], number),
    )
    db.log_event(event, level="warn", account_id=account["id"], user=user,
                 posting_number=shown or number, message="Сброшена отметка сборки")
    return f"{shown or number}: отметка сборки снята"


def mark_printed(account: dict, user: dict, numbers: list[str], *, event: str, message: str,
                 shown: dict[str, str] | None = None) -> None:
    """Отметить наклейки напечатанными: время и счётчик — в таблице площадки, строка — в журнал.

    Выгрузка архива наклеек сюда не ходит: это скачивание, у неё своя
    отметка — label_saved_at, по ней открывается замок на сборку.
    """
    board = board_of(account)
    now = db.now_iso()
    with db.write() as conn:
        for number in numbers:
            conn.execute(
                f"UPDATE {board.table} SET printed_at = ?, print_count = print_count + 1 "
                f"WHERE account_id = ? AND {board.key} = ?",
                (now, account["id"], number),
            )
            db.log_event(event, account_id=account["id"], user=user,
                         posting_number=(shown or {}).get(number, number), message=message, conn=conn)


# ------------------------------------------------------------------ действия
def action_of(account: dict, key: str):
    """Действие площадки кабинета по ключу. LookupError — такого у неё нет."""
    board = board_of(account)
    found = next((action for action in (board.actions if board else ()) if action.key == key), None)
    if found is None:
        raise LookupError("У этой площадки такого действия нет")
    return found
