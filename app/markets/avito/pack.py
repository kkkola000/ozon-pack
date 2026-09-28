"""Рабочее место сборщика Avito.

У Avito штрихкодов товаров нет, поэтому порядок обратный тому, что на Ozon:

  1. сборщик печатает стикер отправления;
  2. сканирует стикер — открывается сборка этого заказа;
  3. сканирует штрихкод товара (свой, поставщика — любой, какой есть);
  4. панель записывает его в отчёт вместе с отправлением и названием товара;
  5. когда отсканированы все позиции, заказ помечается собранным.

Своих штрихкодов у Avito нет: в каталоге панели объявление — это номер и
название. Поэтому сверка есть только там, где её дало сопоставление
(core/linked.py):

* объявление **сопоставлено** с карточкой другой площадки — годятся только
  штрихкоды этой группы, чужой товар — «СТОП»;
* у объявления есть **набор** (свой или сопоставленной карточки) — единицу
  собирают по частям, как у Ozon;
* **не сопоставлено** — сверять не с чем: панель записывает, что приложили к
  заказу, и не решает, «тот» это товар или нет.

Ошибкой всегда считается и скан стикера чужого заказа.
"""
from __future__ import annotations

import json
import re
from collections import Counter

from ...core import board as core_board
from ...core import labels as core_labels
from ...core import db, linked, pack_state, product_sets, report
from ..ozon.pack import barcode_variants
from .client import STATUS_LABELS
from . import store
from ...core import store as core_store

# Заказы, которые сборщику имеет смысл собирать.
PACKABLE = "ready_to_ship"


class ScanResult(dict):
    def __init__(self, status: str, message: str, *, action: str = "noop",
                 sound: str | None = None, **extra) -> None:
        super().__init__(status=status, message=message, action=action, sound=sound or status, **extra)


# ------------------------------------------------------------------ опознание стикера
def code_variants(code: str) -> list[str]:
    """Стикер может нести номер заказа, отправления или трек — берём как есть и цифрами."""
    code = (code or "").strip()
    if not code:
        return []
    variants = [code]
    digits = re.sub(r"\D", "", code)
    if digits and digits != code:
        variants.append(digits)
    if code.lstrip("0") and code.lstrip("0") != code:
        variants.append(code.lstrip("0"))
    seen: list[str] = []
    for item in variants:
        if item and item not in seen:
            seen.append(item)
    return seen


def find_order(account_id: int, code: str) -> dict | None:
    variants = code_variants(code)
    if not variants:
        return None
    marks = ",".join("?" for _ in variants)
    row = db.query_one(
        f"""
        SELECT * FROM avito_orders
        WHERE account_id = ?
          AND (id IN ({marks}) OR marketplace_id IN ({marks})
               OR dispatch_number IN ({marks}) OR tracking_number IN ({marks}))
        LIMIT 1
        """,
        [account_id] + variants * 4,
    )
    return dict(row) if row else None


def labels(account: dict, user: dict, ids: list[str], *, mark: bool = True) -> tuple[bytes, str]:
    """Этикетки заказов — файл Avito как есть. mark — отметить печать.

    Avito знает заказ по номеру сделки, а ключ у нас свой — отсюда подстановка.
    Выгрузка архива до смены печать не отмечает: у неё своя отметка «скачано».
    """
    from . import client as avito

    marks = ",".join("?" for _ in ids)
    shown = {row["id"]: (row["marketplace_id"] or row["id"]) for row in db.query(
        f"SELECT id, marketplace_id FROM avito_orders WHERE account_id = ? AND id IN ({marks})",
        [account["id"]] + list(ids),
    )} if ids else {}
    missing = [key for key in ids if key not in shown]
    if missing:
        raise LookupError(f"Заказ {missing[0]} не найден в кабинете «{account['title']}»")
    pdf, filename = avito.get_client(account).label_pdf([shown[key] for key in ids])
    if mark:
        core_board.mark_printed(account, user, ids, event="avito_label_print",
                                message="Этикетка отправлена на печать", shown=shown)
    return pdf, filename


def owner(account: dict, user: dict, code: str) -> tuple[str, str] | None:  # noqa: ARG001 - подпись общая
    """Чей это код: («label», номер заказа). None — код тут не наш.

    У Avito это всегда этикетка: справочника штрихкодов площадка не отдаёт, и
    по товару заказ не найти — сборка тут и начинается со скана этикетки.
    Спрашивается до скана и ничего не меняет.
    """
    order = find_order(account["id"], code)
    return ("label", str(order["id"])) if order else None


# ------------------------------------------------------------------ состояние сборки
def load_state(account: dict, user: dict) -> dict:
    empty = {"active": None, "items": [], "done": 0, "total": 0, "complete": False, "scanned": []}
    row = db.query_one(
        "SELECT * FROM pack_state WHERE user_id = ? AND account_id = ?", (user["id"], account["id"])
    )
    if not row or not row["posting_number"]:
        return empty

    order_row = db.query_one(
        "SELECT * FROM avito_orders WHERE account_id = ? AND id = ?", (account["id"], row["posting_number"])
    )
    if not order_row:
        pack_state.clear(user)
        return empty

    entries = _entries(json.loads(row["scanned"] or "[]"))
    order = store.avito_view(order_row)
    units = _units(account["id"], row["posting_number"])
    sets = product_sets.parts_for(account["id"], [card_sku(item) for item, _no in units])
    looks: dict[str, tuple[list[str], str | None]] = {}
    articles: dict[str, str | None] = {}
    items = []
    for index, (item, unit_no) in enumerate(units):
        sku = card_sku(item)
        if sku not in looks:
            looks[sku] = linked.extras(account["id"], sku, image=item.get("image"))
            # Артикул — основной карточки группы: у объявления его может не быть.
            articles[sku] = linked.article(account["id"], sku, item.get("seller_id"))
        view = _unit_view(index, item, unit_no, entries, sets.get(sku), looks[sku])
        items.append({**view, "article": articles[sku]})
    done = sum(1 for item in items if item["scanned"])
    return {
        "active": order,
        "items": items,
        "done": done,
        "total": len(units),
        "complete": bool(units) and done >= len(units),
        "scanned": entries,
        "started_at": row["started_at"],
    }


def card_sku(item: dict) -> str:
    """Карточка позиции в каталоге панели: у Avito это номер объявления."""
    return str(item.get("avito_id") or "")


def _entries(raw) -> list[dict]:
    """Сканы заказа: {"u": единица, "code": штрихкод} и у набора ещё "part".

    Раньше это был просто список штрихкодов по порядку единиц — читаем и его:
    сборка, начатая до обновления, не должна потеряться.
    """
    if not isinstance(raw, list):          # состояние от рабочего места Ozon
        return []
    entries = []
    for index, entry in enumerate(raw):
        if isinstance(entry, str):
            entries.append({"u": index, "code": entry})
        elif isinstance(entry, dict) and isinstance(entry.get("u"), int):
            entries.append(entry)
    return entries


def _unit_view(index: int, item: dict, unit_no: int, entries: list[dict],
               parts: list[dict] | None, look: tuple[list[str], str | None]) -> dict:
    """Единица товара для экрана и для сверки.

    checked — есть ли с чем сверять скан: штрихкоды сопоставленных карточек
    или состав набора. Нет — единица принимает любой штрихкод, как раньше.
    """
    mine = [entry for entry in entries if entry["u"] == index]
    direct = next((entry["code"] for entry in mine if not entry.get("part")), None)
    barcodes, image = look
    view = {**item, "index": index, "unit_no": unit_no, "image": image, "barcodes": barcodes,
            "barcode": direct, "checked": bool(barcodes) or bool(parts)}
    if parts:
        counts = Counter(product_sets.part_slot(str(index), entry["part"]) for entry in mine if entry.get("part"))
        got, extra = product_sets.progress(str(index), 1, 1 if direct else 0, parts, counts,
                                           name=item.get("title"))
        view.update(extra)
        view["scanned"] = got >= 1
    else:
        view["scanned"] = direct is not None
    view["ok"] = view["scanned"]
    return view


def _units(account_id: int, order_id: str) -> list[tuple[dict, int]]:
    """Позиции заказа, развёрнутые по единицам: на каждую нужен один скан."""
    units: list[tuple[dict, int]] = []
    for item in store.avito_items(account_id, order_id):
        for number in range(1, max(1, int(item.get("quantity") or 1)) + 1):
            units.append((item, number))
    return units


def release(account: dict, user: dict, *, reason: str = "manual") -> ScanResult:
    """Отпустить начатый заказ, ничего не завершая."""
    pack_state.release(user, event="avito_pack_release", reason=reason)
    return ScanResult("ok", "Сборка отменена", action="released", state=load_state(account, user))


# ------------------------------------------------------------------ основной вход
def scan(account: dict, user: dict, code: str) -> ScanResult:
    code = (code or "").strip()
    if not code:
        return ScanResult("error", "Пустой скан", state=load_state(account, user))

    state = load_state(account, user)
    order = find_order(account["id"], code)

    if state["active"] is None:
        if order:
            return open_order(account, user, order, code)
        db.log_event("avito_scan_unknown", level="error", account_id=account["id"], user=user,
                     barcode=code, message="Стикер не опознан")
        return ScanResult(
            "error",
            f"Код «{code}» не найден среди заказов. Сначала отсканируйте наклейку заказа.",
            action="unknown",
            state=state,
        )

    active_id = state["active"]["id"]
    if order and order["id"] != active_id:
        with db.write() as conn:
            db.log_event("avito_scan_wrong_label", level="error", account_id=account["id"], user=user,
                         posting_number=active_id, barcode=code,
                         message=f"Стикер заказа {order.get('marketplace_id') or order['id']}", conn=conn)
            report.record_error(
                conn, account, user, "wrong_label", posting_number=active_id, barcode=code,
                name=f"стикер заказа {order.get('marketplace_id') or order['id']}",
            )
        return ScanResult(
            "error",
            f"СТОП: это стикер заказа {order.get('marketplace_id') or order['id']}, "
            f"а вы собираете {state['active'].get('marketplace_id') or active_id}.",
            action="wrong_label",
            state=state,
        )

    if order and order["id"] == active_id:
        if state["complete"]:
            return complete(account, user, active_id, code)
        left = state["total"] - state["done"]
        return ScanResult(
            "warning",
            f"Сначала отсканируйте товар: осталось {left} из {state['total']}.",
            action="incomplete",
            sound="error",
            state=state,
        )

    return _scan_item(account, user, state, code)


def open_order(account: dict, user: dict, order: dict, code: str | None = None) -> ScanResult:
    order_id = order["id"]
    number = order.get("marketplace_id") or order_id

    if order.get("local_state") == "packed":
        return ScanResult(
            "warning",
            f"Заказ {number} уже собран ({order.get('packed_by') or '—'}). Повторно собирать не нужно.",
            action="already_packed",
            sound="error",
            state=load_state(account, user),
        )
    if order.get("status") != PACKABLE:
        label = STATUS_LABELS.get(order.get("status") or "", order.get("status") or "")
        return ScanResult(
            "warning",
            f"Заказ {number} в статусе «{label}» — собирать его рано. "
            "Собираются заказы со статусом «Отправьте заказ».",
            action="wrong_status",
            sound="error",
            state=load_state(account, user),
        )
    if core_store.claim_is_active(order.get("claim_at")) and order.get("claim_user_id") != user["id"]:
        return ScanResult(
            "error",
            f"Заказ {number} уже собирает {order.get('claim_login')}.",
            action="locked",
            sound="error",
            state=load_state(account, user),
        )

    now = db.now_iso()
    with db.write() as conn:
        pack_state.release_previous(conn, user, keep=(account["id"], order_id))
        conn.execute(
            "UPDATE avito_orders SET claim_user_id = ?, claim_login = ?, claim_at = ? "
            "WHERE account_id = ? AND id = ?",
            (user["id"], user["login"], now, account["id"], order_id),
        )
        pack_state.save(conn, account, user, order_id, [])
        db.log_event("avito_pack_start", account_id=account["id"], user=user,
                     posting_number=order_id, barcode=code, message="Заказ взят в сборку", conn=conn)

    state = load_state(account, user)
    if not state["total"]:
        return ScanResult(
            "warning",
            f"Заказ {number} открыт, но состав не загружен. Обновите заказы из Avito.",
            action="opened",
            sound="error",
            state=state,
        )
    return ScanResult(
        "ok",
        f"Заказ {number}: отсканируйте штрихкоды товаров — нужно {state['total']} шт.",
        action="order_opened",
        sound="ok",
        state=state,
    )


def _scan_item(account: dict, user: dict, state: dict, code: str) -> ScanResult:
    """Штрихкод товара: к какой единице заказа он подходит.

    По порядку: единица, чей товар сопоставлен с владельцем штрихкода, — скан
    сверен; часть набора; единица, сверять которую не с чем, — записываем как
    есть. Ни то, ни другое, ни третье — «СТОП»: остались только единицы со
    сверкой, и этот товар ни одной из них не подходит.
    """
    order_id = state["active"]["id"]
    if state["complete"]:
        return _extra_scan(account, user, state, code)

    variants = barcode_variants(code)
    cards = linked.cards_of_code(variants)
    mine = [u for u in state["items"] if linked.card(account["id"], card_sku(u)) in cards]
    # Комплект ждёт вложение — скан идёт ему, даже если такой товар есть в заказе.
    if product_sets.waiting(state["items"]) is None:
        unit = next((u for u in mine if not u["scanned"]), None)
        if unit is not None:
            return _credit_unit(account, user, state, unit, code)
        if mine:
            # Товар этого заказа, но все его единицы уже набраны: это лишнее, а не
            # повод записать его в соседнюю единицу без сверки.
            return _too_many(account, user, state, mine[0].get("title") or "Товар", code,
                             f"«{mine[0].get('title') or 'Товар'}» уже отсканирован в нужном количестве. "
                             "Лишнее не кладите.")

    parents = product_sets.parents_of(account["id"], barcodes=variants)
    picked = product_sets.pick_part(state["items"], parents, card_sku) if parents else None
    if picked is not None and not picked[0]["ok"] and not picked[1]["ok"]:
        return _credit_part(account, user, state, picked[0], picked[1], code)
    if picked is not None:
        part, set_name = picked[1]["name"], picked[0].get("title") or product_sets.word(picked[0])
        whose = "комплекта" if picked[0].get("kind") == "kit" else "набора"
        return _too_many(account, user, state, f"{set_name} — {part}", code,
                         f"«{part}» для {whose} «{set_name}» уже набран. Лишнее не кладите.")

    unit = next((u for u in state["items"] if not u["scanned"] and not u["checked"]), None)
    if unit is not None:
        return _credit_unit(account, user, state, unit, code)
    return _wrong_product(account, user, order_id, state, cards, code)


def _extra_scan(account: dict, user: dict, state: dict, code: str) -> ScanResult:
    """Все единицы уже отсканированы — лишний скан."""
    order_id = state["active"]["id"]
    with db.write() as conn:
        db.log_event("avito_scan_extra", level="warn", account_id=account["id"], user=user,
                     posting_number=order_id, barcode=code, message="Лишний скан", conn=conn)
        report.record_error(conn, account, user, "extra_product",
                            posting_number=order_id, barcode=code)
    return ScanResult(
        "warning",
        f"Все {state['total']} шт уже отсканированы. Отсканируйте стикер, чтобы закрыть заказ.",
        action="extra_product",
        sound="error",
        state=state,
    )


def _wrong_product(account: dict, user: dict, order_id: str, state: dict,
                   cards: set, code: str) -> ScanResult:
    """Остались только единицы со сверкой, и этот товар ни одной не подходит."""
    name = _name_of(cards) or f"код {code}"
    with db.write() as conn:
        db.log_event("avito_scan_wrong_product", level="error", account_id=account["id"], user=user,
                     posting_number=order_id, barcode=code, message="Товар не из заказа", conn=conn)
        report.record_error(conn, account, user, "wrong_product", posting_number=order_id,
                            barcode=code, name=name if cards else None)
    number = state["active"].get("marketplace_id") or order_id
    return ScanResult(
        "error",
        f"СТОП: {'«' + name + '»' if cards else name} не входит в заказ {number}. Уберите товар.",
        action="wrong_product",
        state=state,
    )


def _name_of(cards: set) -> str | None:
    """Название товара по карточкам, которым принадлежит штрихкод."""
    for account_id, sku in sorted(cards):
        row = db.query_one("SELECT name FROM products WHERE account_id = ? AND sku = ?", (account_id, sku))
        if row and row["name"]:
            return row["name"]
    return None


def _too_many(account: dict, user: dict, state: dict, name: str, code: str, text: str) -> ScanResult:
    """Товар или часть набора этого заказа, но нужное количество уже набрано."""
    order_id = state["active"]["id"]
    with db.write() as conn:
        db.log_event("avito_scan_extra", level="warn", account_id=account["id"], user=user,
                     posting_number=order_id, barcode=code, message=f"Сверх нужного: {name}", conn=conn)
        report.record_error(conn, account, user, "extra_product", posting_number=order_id,
                            barcode=code, name=name)
    return ScanResult("warning", text, action="extra_product", sound="error", state=state)


def _credit_part(account: dict, user: dict, state: dict, unit: dict, part: dict, code: str) -> ScanResult:
    """Засчитать часть набора. В отчёт единица идёт, только когда собрана целиком."""
    order_id = state["active"]["id"]
    set_name = unit.get("title") or "набор"
    entries = list(state["scanned"]) + [{"u": unit["index"], "part": part["part_key"], "code": code}]
    with db.write() as conn:
        pack_state.save(conn, account, user, order_id, entries)
        db.log_event("avito_scan_set_part", account_id=account["id"], user=user, posting_number=order_id,
                     barcode=code, message=f"{set_name}: {part['name']} {part['scanned'] + 1}/{part['need']}",
                     conn=conn)
    new_state = load_state(account, user)
    new_unit = new_state["items"][unit["index"]]
    if new_unit["scanned"]:
        # Отгружен набор, а не его содержимое: пишем единицу, а не каждую часть.
        with db.write() as conn:
            report.record_shipped(conn, account, user, order_id, _report_item(unit), unit["unit_no"], code)
    if new_state["complete"]:
        return complete(account, user, order_id, code, auto=True)
    left = [p["name"] for p in new_unit.get("parts", []) if not p["ok"]]
    tail = f" Осталось: {', '.join(left)}." if left else ""
    return ScanResult(
        "ok",
        f"«{set_name}»: {part['name']} {part['scanned'] + 1}/{part['need']}. "
        f"Собрано {new_state['done']} из {new_state['total']}.{tail}",
        action="set_part_scanned",
        state=new_state,
    )


def _report_item(unit: dict) -> dict:
    return {"sku": None, "offer_id": unit.get("seller_id"), "name": unit.get("title"),
            "key": f"av:{unit.get('avito_id')}"}


def _credit_unit(account: dict, user: dict, state: dict, unit: dict, code: str) -> ScanResult:
    """Записать штрихкод в единицу. Сверенный — так и сказать сборщику."""
    order_id = state["active"]["id"]
    entries = list(state["scanned"]) + [{"u": unit["index"], "code": code}]
    with db.write() as conn:
        pack_state.save(conn, account, user, order_id, entries)
        db.log_event("avito_scan_item", account_id=account["id"], user=user, posting_number=order_id,
                     barcode=code, message=f"{state['done'] + 1}/{state['total']}", conn=conn)
        # Отчёт: сошлась пара «штрихкод -> отправление», её и записываем.
        report.record_shipped(conn, account, user, order_id, _report_item(unit), unit["unit_no"], code)

    new_state = load_state(account, user)
    if new_state["complete"]:
        return complete(account, user, order_id, code, auto=True)
    head = (f"«{unit.get('title') or 'Товар'}»: штрихкод сверен." if unit["checked"]
            else f"Записан штрихкод {code}.")
    return ScanResult(
        "ok",
        f"{head} Собрано {new_state['done']} из {new_state['total']}.",
        action="item_scanned",
        state=new_state,
    )


def complete(account: dict, user: dict, order_id: str, code: str | None = None,
             auto: bool = False) -> ScanResult:
    now = db.now_iso()
    with db.write() as conn:
        conn.execute(
            "UPDATE avito_orders SET local_state = 'packed', packed_at = ?, packed_by = ?, "
            "claim_user_id = NULL, claim_login = NULL, claim_at = NULL "
            "WHERE account_id = ? AND id = ?",
            (now, user["login"], account["id"], order_id),
        )
        db.log_event("avito_pack_complete", account_id=account["id"], user=user,
                     posting_number=order_id, barcode=code, message="Заказ собран", conn=conn)
        pack_state.clear(user, conn)
    row = db.query_one("SELECT marketplace_id FROM avito_orders WHERE account_id = ? AND id = ?",
                       (account["id"], order_id))
    number = (row["marketplace_id"] if row and row["marketplace_id"] else order_id)
    tail = " Осталось отправить заказ в Avito." if auto else ""
    return ScanResult(
        "ok",
        f"Готово: заказ {number} собран.{tail}",
        action="completed",
        sound="done",
        completed_order=order_id,
        state=load_state(account, user),
    )


# ------------------------------------------------------------------- Avito
def pending_labels(account_id: int) -> list[str]:
    """Заказы «Ожидает отгрузки» («Отправьте заказ»), чья этикетка ещё не выгружена."""
    return core_labels.waiting(account_id, table="avito_orders", key="id", status_sql=store.BOARD_STATUS_SQL)
