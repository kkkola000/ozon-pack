"""Приёмка возвратов сканером: стикер или штрихкод возврата, затем товар.

Сборщик вернулся из пункта выдачи с пакетом возвратов. Вместо того чтобы
искать строку в акте глазами, он делает два скана:

1. **стикер или штрихкод возврата** — панель находит возврат в неподтверждённых
   актах и открывает окно отметки. Код отправления или заказа находит сразу
   все его возвраты;
2. **штрихкод товара** — совпал с товаром возврата, и отметка становится
   «Принят». У возврата из нескольких штук или товаров (заказ Avito) —
   когда отсканировано всё.

Не тот товар — отметка не меняется: «Не принят» ставит человек, с
комментарием, что именно не так. Панель подмену только замечает.

Какой код что означает и что лежит в возврате, объявляет площадка
(ReturnsSource.find и scan_view). Здесь — общее: где искать, как сверять
товар и что ответить. Товар сверяется так же, как на сборке: штрихкоды
карточки и сопоставленных с ней карточек (core/linked.py), а ещё SKU и
артикул продавца.

Счёт отсканированного держит окно в браузере и присылает с каждым сканом.
Сервер ему верит: это счёт того же человека, который мог бы нажать «Принят»
и кнопкой, — подделывать его незачем. Сам же сервер решает главное: подходит
ли товар и можно ли ставить отметку.
"""
from __future__ import annotations

from . import accounts, db, linked, return_acts, return_marks, store
from .codes import barcode_variants


class Refused(Exception):
    """Скан не к чему применить: код не найден, возврат не ждёт отметки."""

    def __init__(self, message: str, status: int = 404) -> None:
        super().__init__(message)
        self.status = status


def _registry():
    from ..markets import registry

    return registry


def _sources() -> list:
    """Площадки, у которых возврат узнаётся сканом: [(market, source)]."""
    return [(market, source) for market, source in return_acts.sources() if source.find and source.scan_view]


def _row(source, account_id: int, return_id: str) -> dict | None:
    row = db.query_one(f"SELECT * FROM {source.table} WHERE account_id = ? AND id = ?", (account_id, return_id))
    return dict(row) if row else None


# ------------------------------------------------------------ товар возврата
def _look(account_id: int, sku: str | None, image: str | None) -> tuple[list[str], str | None]:
    """Штрихкоды и фото карточки: свои из каталога, затем сопоставленных карточек."""
    if not sku:
        return [], image
    own = [row["barcode"] for row in db.query(
        "SELECT barcode FROM product_barcodes WHERE account_id = ? AND sku = ? ORDER BY barcode", (account_id, sku)
    )]
    product = db.query_one("SELECT image, barcodes FROM products WHERE account_id = ? AND sku = ?", (account_id, sku))
    if product:
        own += [str(code) for code in db.json_list(product["barcodes"])]
        image = image or product["image"]
    return linked.extras(account_id, sku, image=image, barcodes=own)


def _goods(account_id: int, view: dict) -> list[dict]:
    """Товары возврата со штрихкодами для сверки. checked — есть с чем сверять."""
    goods = []
    for item in view.get("goods") or []:
        codes, image = _look(account_id, item.get("sku"), item.get("image"))
        goods.append({
            **item,
            "key": str(item["key"]),
            "need": max(1, int(item.get("need") or 1)),
            "codes": codes,
            "image": image or "",
            "checked": bool(codes),
        })
    return goods


def _hits(account_id: int, item: dict, variants: list[str], cards: set) -> bool:
    """Подходит ли скан к товару: штрихкод карточки или соседней, SKU, артикул."""
    if set(variants) & set(item["codes"]):
        return True
    sku = item.get("sku")
    if sku and linked.card(account_id, sku) in cards:
        return True
    return any(value and str(value) in variants for value in (sku, item.get("offer_id")))


def _name_of(cards: set) -> str | None:
    """Название товара, чей штрихкод отсканировали, — чтобы сказать, что взяли."""
    for account_id, sku in sorted(cards):
        row = db.query_one("SELECT name FROM products WHERE account_id = ? AND sku = ?", (account_id, sku))
        if row and row["name"]:
            return row["name"]
    return None


# ------------------------------------------------------------ шаг 1: возврат
def _card(market, source, row: dict, act: dict) -> dict:
    """Возврат для окна приёмки: карточка, отметка и товары с тем, что ожидается."""
    view = source.scan_view(row)
    goods = _goods(row["account_id"], view)
    account = accounts.get(row["account_id"]) or {}
    mark = row.get("mark") or ""
    return {
        "marketplace": market.code,
        "market": source.label,
        "id": str(row["id"]),
        "account_id": row["account_id"],
        "shop": account.get("title") or "",
        "act_id": act["id"],
        "act_title": return_acts.title(act),
        "title": view.get("title") or "Без названия",
        "number": view.get("number") or str(row["id"]),
        "facts": [list(fact) for fact in view.get("facts") or []],
        "image": next((item["image"] for item in goods if item["image"]), ""),
        "mark": mark,
        "mark_label": store.mark_label(mark),
        "mark_sign": store.RETURN_MARK_SIGNS.get(mark, ""),
        "mark_by": row.get("mark_by") or "",
        "mark_at_local": store.local_time(row.get("mark_at")) if row.get("mark_at") else "",
        "note": row.get("note") or "",
        # Штрихкоды целиком в браузер не отдаём: там нужен только пример —
        # что искать на коробке.
        "goods": [{"key": item["key"], "name": item.get("name") or "Без названия", "need": item["need"],
                   "image": item["image"], "expect": item["codes"][0] if item["codes"] else "",
                   "checked": item["checked"]} for item in goods],
    }


def lookup(where: list[dict], code: str) -> dict:
    """Что за возврат отсканировали. where — кабинеты с возвратами.

    Находятся только возвраты неподтверждённых актов: отметку ставят там. Для
    остальных — прямо, что с ними: уже в подтверждённом акте, получен, но акт
    не составлен, или ещё лежит в пункте выдачи.
    """
    code = (code or "").strip()
    found: list[tuple] = []
    for market, source in _sources():
        ids = [account["id"] for account in where if account["marketplace"] == market.code]
        for account_id, return_id in (source.find(ids, code) if ids else []):
            row = _row(source, account_id, return_id)
            if row:
                found.append((market, source, row, return_acts.get(row["act_id"]) if row.get("act_id") else None))
    if not found:
        raise Refused(f"Код «{code}» не найден: это не стикер и не штрихкод возврата. "
                      "Отсканируйте наклейку на пакете возврата или стикер отправления.")

    open_ = [(market, source, row, act) for market, source, row, act in found if act and not act.get("confirmed_at")]
    if not open_:
        raise Refused(_why_not(found[0]), status=409)

    cards = [_card(market, source, row, act) for market, source, row, act in open_]
    if len(cards) == 1:
        card = cards[0]
        return {"status": "ok", "title": "Отметка о возврате",
                "sub": f"{card['market']} · кабинет «{card['shop']}» · {card['act_title']}", "rows": cards}
    shops = sorted({card["shop"] for card in cards})
    return {"status": "ok", "title": f"Возвраты по коду {code}",
            "sub": f"{len(cards)} возвр. · " + ", ".join(f"кабинет «{shop}»" for shop in shops),
            "rows": cards}


def _why_not(entry: tuple) -> str:
    """Возврат нашёлся, но отмечать его сейчас негде — сказать почему и что делать."""
    _market, source, row, act = entry
    number = source.scan_view(row).get("number") or row["id"]
    if act:
        return (f"Возврат {number} уже в подтверждённом акте «{return_acts.title(act)}» — отметку там "
                "не меняют. Вернуть акт в работу может владелец в «Отчётах».")
    if row.get("received_at"):
        day = store.local_time(row["received_at"], "%d.%m.%Y")
        return (f"Возврат {number} получен {day}, но ещё не в акте. Составьте акт за {day} "
                "и отсканируйте снова.")
    return (f"Возврат {number} ещё не получен — он в списке «К выдаче». Нажмите «Обновить возвраты», "
            "составьте акт за сегодня и отсканируйте снова.")


# ------------------------------------------------------------ шаг 2: товар
def _open_row(where: list[dict], entry: dict) -> tuple:
    """Строка из запроса окна — только если она всё ещё ждёт отметки."""
    market = _registry().get(str(entry.get("marketplace") or ""))
    source = market.returns if market else None
    if not source or not source.scan_view:
        raise Refused("Неизвестная площадка", status=400)
    try:
        account_id = int(entry.get("account_id"))
    except (TypeError, ValueError) as exc:
        raise Refused("Не указан кабинет возврата", status=400) from exc
    if not any(account["id"] == account_id and account["marketplace"] == market.code for account in where):
        raise Refused("Кабинет возврата не найден или выключен")
    row = _row(source, account_id, str(entry.get("id") or ""))
    if not row:
        raise Refused(f"Возврат {entry.get('id')} не найден")
    act = return_acts.get(row["act_id"]) if row.get("act_id") else None
    if not act or act.get("confirmed_at"):
        raise Refused(f"Возврат {entry.get('id')} уже не ждёт отметки: акт подтверждён или удалён", status=409)
    return market, source, row, act


def _counts(raw) -> dict[str, int]:
    """Счёт отсканированного из окна: {товар: штук}. Мусор — как ноль."""
    counts = {}
    for key, value in (raw or {}).items() if isinstance(raw, dict) else ():
        try:
            counts[str(key)] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    return counts


def check_goods(where: list[dict], entries: list[dict], code: str, user: dict) -> dict:
    """Штрихкод товара к открытым в окне возвратам. Совпал и всё набрано — «Принят».

    Порядок, как на сборке: товар, к которому скан подходит и который ещё не
    набран; тот же товар, но уже набранный, — лишний скан; незнакомый код к
    товару, сверять который не с чем, — засчитываем. Иначе — не тот товар, и
    отметка не меняется.
    """
    variants = barcode_variants(code)
    cards = linked.cards_of_code(variants)
    rows = _load(where, entries)
    kind, entry, good = _pick(rows, variants, cards)
    if kind == "credit":
        return _credit(entry, good, code, user)
    if kind == "extra":
        return {"status": "warning", "action": "extra", "key": good["key"],
                "message": f"«{good['name']}» уже отсканирован ({good['need']} шт.) — лишний скан."}
    return _wrong(rows, cards, code, user)


def _load(where: list[dict], entries: list) -> list[dict]:
    """Возвраты из окна вместе с их товарами и счётом отсканированного."""
    rows = []
    for entry in entries[:50]:
        entry = entry if isinstance(entry, dict) else {}
        market, source, row, act = _open_row(where, entry)
        view = source.scan_view(row)
        rows.append({"market": market, "source": source, "row": row, "act": act, "view": view,
                     "goods": _goods(row["account_id"], view), "got": _counts(entry.get("got"))})
    return rows


def _pick(rows: list[dict], variants: list[str], cards: set) -> tuple[str, dict | None, dict | None]:
    """Куда лёг скан: ('credit' | 'extra' | 'wrong', возврат, товар)."""
    pairs = [(entry, good) for entry in rows for good in entry["goods"]]
    hits = [(entry, good) for entry, good in pairs
            if _hits(entry["row"]["account_id"], good, variants, cards)]

    def left(entry: dict, good: dict) -> bool:
        return entry["got"].get(good["key"], 0) < good["need"]

    for entry, good in hits:
        if left(entry, good):
            return "credit", entry, good
    if hits:
        return "extra", *hits[0]
    # Сверять не с чем: у товара в каталоге нет штрихкодов. Засчитываем, только
    # если код ничей: штрихкод другого известного товара — это подмена.
    if not cards:
        for entry, good in pairs:
            if not good["checked"] and left(entry, good):
                return "credit", entry, good
    return "wrong", None, None


def _credit(entry: dict, good: dict, code: str, user: dict) -> dict:
    """Засчитать штуку. Набран весь возврат — отметка «Принят», комментарий остаётся."""
    got = dict(entry["got"])
    got[good["key"]] = got.get(good["key"], 0) + 1
    row, view = entry["row"], entry["view"]
    done = all(got.get(item["key"], 0) >= item["need"] for item in entry["goods"])
    number = view.get("number") or row["id"]
    answer = {"status": "ok", "key": good["key"], "got": got, "done": done, "checked": good["checked"],
              "row": {"marketplace": entry["market"].code, "id": str(row["id"]), "account_id": row["account_id"]}}
    unchecked = "" if good["checked"] else " Штрихкода товара в каталоге нет — сверить было не с чем."
    if not done:
        left = sum(max(0, item["need"] - got.get(item["key"], 0)) for item in entry["goods"])
        return {**answer, "action": "counted",
                "message": f"«{good['name']}» засчитан. По возврату {number} осталось {left} шт.{unchecked}"}
    account = accounts.get(row["account_id"])
    mark = return_marks.save(entry["source"], account, str(row["id"]), row["act_id"], "ok",
                             row.get("note") or "", user, how=f"скан товара {code}")
    return {**answer, "action": "accepted", "mark": mark, "act": mark["act"],
            "message": f"Товар совпал — возврат {number} принят.{unchecked}"}


def _wrong(rows: list[dict], cards: set, code: str, user: dict) -> dict:
    """Не тот товар: сказать, что взяли и что ждали. Отметка не меняется."""
    wanted = ", ".join(dict.fromkeys(f"«{good['name']}»" for entry in rows for good in entry["goods"]))
    scanned = _name_of(cards)
    if scanned:
        message = (f"Отсканирован «{scanned}» ({code}), а в возврате — {wanted}. "
                   "Отметка не изменилась. Если товар подменили — нажмите «Не принят» и опишите в комментарии.")
    else:
        message = (f"Код «{code}» не подходит к товару возврата: ожидается {wanted}. Отметка не изменилась. "
                   "Отсканируйте штрихкод на самом товаре.")
    first = rows[0]["row"] if rows else {}
    db.log_event("return_scan_wrong", level="warn", account_id=first.get("account_id"), user=user,
                 barcode=code, message=f"{first.get('id')}: {message}")
    return {"status": "error", "action": "wrong_product", "message": message, "scanned": scanned or ""}
