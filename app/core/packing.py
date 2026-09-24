"""Сборка: в каком кабинете искать отсканированный код.

Раньше это решал кабинет в шапке. Стоишь в Ozon — сканируется Ozon, и этикетка
Avito в руках давала «такого отправления нет», хотя заказ есть и горит. На
складе кабинета не существует: там один стол, одна коробка и один сканер.

Теперь решает фильтр площадок на «Сборке»:

* **«Все заказы»** — сборка идёт по всем кабинетам. Отсканировали этикетку
  Avito, стоя в Ozon, — панель сама найдёт кабинет и откроет заказ. Кабинет в
  шапке при этом не меняется: он больше ни за что тут не отвечает.
* **выбрана площадка** — сборка в её границах. Наклейка чужого кабинета не
  откроется, и это правильный ответ, а не поломка.

Три правила, в таком порядке:

1. **Начатая сборка сильнее фильтра.** Коробка уже в руках — что бы ни стояло
   в фильтре, скан уходит в её кабинет. Иначе сборщик, переключив фильтр,
   потерял бы наполовину собранный заказ: начатая сборка у человека одна.
2. **Чужая наклейка посреди сборки — стоп.** Перед тем как отдать код в
   открытый кабинет, спрашиваем остальные: не их ли это наклейка. Если их —
   говорим чья, и ничего не трогаем. Товар при этом не в счёт: один и тот же
   товар продаётся в нескольких магазинах, и его скан — обычная позиция.
3. **Сборки нет — ищем владельца кода** среди кабинетов под фильтром. Наклейка
   принадлежит одному заказу, тут спора нет. Товар может быть нужен сразу в
   нескольких кабинетах — тогда берём тот заказ, что стоит первым в общем
   списке, то есть с самым близким сроком отгрузки.

   Открыть по товару нечего ни в одном кабинете — скан уходит туда, где этот
   товар есть в заказе: только тот кабинет честно скажет «ещё в статусе
   «Ожидает сборки»» или «уже собран». Никто код не узнал — ответ «не найден»
   даёт первый кабинет под фильтром, а не тот, что открыт в шапке.

**Кабинет в шапке здесь не участвует ни в чём.** Один и тот же скан даёт один
и тот же ответ, какой бы кабинет ни был выбран, — это проверяется тестами.

Площадок по именам здесь нет: что значит «этот код мой», знает только сама
площадка — она объявляет это в `Workspace.owner`.
"""
from __future__ import annotations

from . import accounts, db, orders as core_orders

# «Все заказы»: фильтр не выбран, сборка идёт по всем кабинетам.
ALL = "all"


def _registry():
    from ..markets import registry

    return registry


def _market(account: dict):
    return _registry().get(account.get("marketplace"))


def _workspace(account: dict):
    market = _market(account)
    return market.workspace if market else None


def workspace_of(account: dict):
    """Рабочее место кабинета — то, что площадка о нём объявила."""
    return _workspace(account)


# ------------------------------------------------------------------ кабинеты
def shops(picked: str = ALL) -> list[dict]:
    """Кабинеты под фильтром: все настроенные или только выбранной площадки."""
    live = [account for account in accounts.all_accounts(active_only=True)
            if accounts.is_configured(account)]
    if picked == ALL:
        return live
    return [account for account in live if account["marketplace"] == picked]


def started(user: dict) -> dict | None:
    """Кабинет начатой сборки. Она у человека одна — так устроен pack_state.

    Фильтр здесь не спрашиваем намеренно: незакрытая коробка важнее любого
    фильтра, и потерять её из-за переключения чипа нельзя.
    """
    row = db.query_one(
        "SELECT account_id FROM pack_state WHERE user_id = ? AND posting_number IS NOT NULL "
        "AND posting_number != ''",
        (user["id"],),
    )
    if not row or not row["account_id"]:
        return None
    account = accounts.get(int(row["account_id"]))
    return account if account and _workspace(account) else None


def shop_of(code: str, order_id: str, user: dict) -> dict | None:
    """Кабинет этой площадки, в котором лежит заказ. None — такого нет.

    Нужно для печати наклейки с рабочего места: заказ там может быть из любого
    кабинета, а какого именно — знает только площадка.
    """
    for shop in shops(code):
        workspace = _workspace(shop)
        if workspace is None:
            continue
        answer = workspace.owner(shop, user, order_id)
        if answer and answer[0] == "label":
            return shop
    return None


def _home(where: list[dict]) -> dict | None:
    """Кабинет, которому достанется код, не узнанный никем.

    Чей-то он всё равно должен быть: код нужно записать в журнал и в отчёт об
    ошибках. Берём первый под фильтром — по порядку кабинетов в «Настройках».
    Не тот, что открыт в шапке: иначе один и тот же скан отвечал бы по-разному
    в зависимости от того, что выбрано наверху, а шапка тут ни при чём.
    """
    return next((shop for shop in where if _workspace(shop)), None)


# ------------------------------------------------------------------ чей это код
def _claims(where: list[dict], user: dict, code: str) -> list[tuple[dict, str, str]]:
    """Кто узнал код: [(кабинет, «label»/«product»/«known», номер заказа)]."""
    found = []
    for shop in where:
        workspace = _workspace(shop)
        if workspace is None:
            continue
        answer = workspace.owner(shop, user, code)
        if answer:
            found.append((shop, answer[0], str(answer[1] or "")))
    return found


def _first_in_list(claims: list[tuple[dict, str, str]]) -> dict:
    """Из нескольких кабинетов — тот, чей заказ стоит первым в общем списке.

    Список отсортирован по срочности и сроку отгрузки, поэтому «первый по
    списку» — это и есть «горит раньше остальных». Заказа нет в списке (его
    унесла синхронизация между двумя запросами) — такой кабинет уходит в конец.
    """
    if len(claims) == 1:
        return claims[0][0]
    rank = {}
    for position, order in enumerate(core_orders.everywhere()):
        rank.setdefault((order["account_id"], str(order["number"])), position)
    ranked = sorted(claims, key=lambda claim: rank.get((claim[0]["id"], claim[2]), 10**6))
    return ranked[0][0]


def owner_of(where: list[dict], user: dict, code: str) -> dict | None:
    """Кабинет, которому принадлежит код. None — не узнал никто.

    Порядок: наклейка → товар, по которому есть что открыть → товар, по
    которому открыть нечего. Последнее важно: «ещё в статусе «Ожидает сборки»»
    может сказать только кабинет, где этот заказ лежит. Отдай скан другому —
    и сборщик услышит «не нужен ни в одном отправлении» про товар, который
    ждёт его в соседнем магазине.
    """
    claims = _claims(where, user, code)
    if not claims:
        return None
    labels = [claim for claim in claims if claim[1] == "label"]
    if labels:
        # Наклейка принадлежит одному заказу: спорить не о чем.
        return labels[0][0]
    products = [claim for claim in claims if claim[1] == "product"]
    if products:
        return _first_in_list(products)
    # Открыть нечего нигде. Сначала те, у кого товар лежит в заказе, потом те,
    # у кого он только в каталоге.
    with_order = [claim for claim in claims if claim[2]]
    return _first_in_list(with_order or claims)


def _stranger(user: dict, open_shop: dict, code: str):
    """Наклейка чужого кабинета посреди сборки. None — код не чужая наклейка.

    Спрашиваем все настроенные кабинеты, а не только те, что под фильтром:
    этикетка соседнего магазина в руках — это ошибка склада независимо от того,
    что выбрано в чипе, и сказать о ней надо прямо.
    """
    from . import report

    others = [shop for shop in shops(ALL) if shop["id"] != open_shop["id"]]
    claim = next((item for item in _claims(others, user, code) if item[1] == "label"), None)
    if claim is None:
        return None

    shop, _kind, number = claim
    state = _workspace(open_shop).load_state(open_shop, user)
    active = state.get("active") or {}
    mine = active.get("posting_number") or active.get("id") or "—"
    with db.write() as conn:
        db.log_event(
            "scan_wrong_shop", level="error", account_id=open_shop["id"], user=user,
            posting_number=str(mine), barcode=code,
            message=f"Наклейка заказа {number} кабинета «{shop['title']}»", conn=conn,
        )
        report.record_error(
            conn, open_shop, user, "wrong_label", posting_number=str(mine), barcode=code,
            name=f"наклейка заказа {number} ({shop['title']})",
        )
    return {
        "status": "error",
        "message": f"СТОП: это наклейка заказа {number} магазина «{shop['title']}», "
                   f"а вы собираете {mine}.",
        "action": "wrong_shop",
        "sound": "error",
        # Подписываем кабинетом открытой сборки, а не того, чью наклейку
        # взяли: карточка на экране остаётся её, и рисовать её той же площадкой.
        "state": _sign(state, open_shop),
        **_sign({}, open_shop),
    }


# ------------------------------------------------------------------ действия
def _sign(state: dict, shop: dict) -> dict:
    """Подписать состояние кабинетом: по нему браузер выбирает, чем рисовать.

    Карточку открытой сборки рисует площадка её заказа, а заказ может быть из
    любого кабинета — угадать по шапке нельзя, и подпись нужна в самом
    состоянии, а не только рядом с ним.
    """
    market = _market(shop)
    return {**state, "market": market.code if market else None, "shop": shop.get("title")}


def _run(shop: dict, call, *args) -> dict:
    """Позвать площадку и подписать ответ кабинетом — по нему рисуется карточка."""
    result = dict(call(shop, *args))
    result.update(_sign({}, shop))
    if isinstance(result.get("state"), dict):
        result["state"] = _sign(result["state"], shop)
    return result


def scan(where: list[dict], user: dict, code: str) -> dict:
    """Один скан рабочего места. Кабинет выбирается по коду, а не по шапке."""
    code = (code or "").strip()
    open_shop = started(user)
    if open_shop is not None:
        stranger = _stranger(user, open_shop, code)
        if stranger is not None:
            return stranger
        return _run(open_shop, _workspace(open_shop).scan, user, code)

    target = owner_of(where, user, code) or _home(where)
    if target is None:
        return {
            "status": "error",
            "message": "Нет ни одного кабинета с ключами — заведите его в «Настройках».",
            "action": "no_account",
            "state": {"active": None, "items": [], "done": 0, "total": 0, "complete": False},
            "market": None,
        }
    return _run(target, _workspace(target).scan, user, code)


def release(user: dict) -> dict:
    """Отменить начатую сборку — в том кабинете, где она открыта."""
    open_shop = started(user)
    if open_shop is None:
        return {
            "status": "warning", "message": "Сборка не начата", "action": "released",
            "state": {"active": None, "items": [], "done": 0, "total": 0, "complete": False},
            "market": None,
        }
    return _run(open_shop, _workspace(open_shop).release, user)


def complete(user: dict, reason: str = "ручное завершение") -> dict:
    """«Завершить без скана наклейки». Отказ — ValueError с текстом для оператора."""
    open_shop = started(user)
    if open_shop is None:
        raise ValueError("Сборка не начата")
    workspace = _workspace(open_shop)
    if workspace.complete is None:
        raise ValueError("У этой площадки заказ закрывает последняя единица товара")
    return _run(open_shop, workspace.complete, user, reason)


def state(user: dict) -> dict:
    """Состояние сборщика: начатая сборка, в каком бы кабинете она ни была."""
    open_shop = started(user)
    if open_shop is None:
        return {"active": None, "items": [], "done": 0, "total": 0, "complete": False, "market": None}
    return _sign(_workspace(open_shop).load_state(open_shop, user), open_shop)


# ------------------------------------------------------------------ плитки очереди
# Плитки под фильтром «Все заказы»: общие для всех площадок. Считать в них
# «Ожидает сборки» Ozon вместе с «Подтвердите заказ» Avito нельзя — это разные
# вещи, поэтому здесь только то, что одинаково на любом складе.
ALL_TILES = (
    ("c-work", "in_work", "В работе", ""),
    ("c-urgent", "urgent", "Горит сегодня", "warn"),
    ("c-packed", "packed_today", "Собрано сегодня", "ok"),
)
URGENT = ("overdue", "urgent", "soon")


def tiles(picked: str, where: list[dict]) -> tuple[tuple, dict]:
    """Плитки очереди и числа к ним — по фильтру, а не по кабинету в шапке.

    Выбрана площадка — её плитки, сложенные по всем её кабинетам. «Все заказы»
    — общие: в работе, горит сегодня, собрано сегодня.
    """
    if picked != ALL:
        workspace = next((_workspace(shop) for shop in where if _workspace(shop)), None)
        if workspace is None:
            return (), {}
        numbers: dict[str, int] = {}
        for shop in where:
            for key, value in _workspace(shop).count_queue(shop).items():
                numbers[key] = numbers.get(key, 0) + int(value or 0)
        return workspace.counters, numbers

    ids = {shop["id"] for shop in where}
    rows = [row for row in core_orders.everywhere() if row["account_id"] in ids]
    in_work = [row for row in rows if row["in_work"]]
    packed = 0
    for shop in where:
        workspace = _workspace(shop)
        if workspace is not None:
            packed += int(workspace.count_queue(shop).get("packed_today") or 0)
    return ALL_TILES, {
        "in_work": len(in_work),
        "urgent": len([row for row in in_work if row["urgency"] in URGENT]),
        "packed_today": packed,
    }
