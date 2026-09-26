"""Сборка заказов Яндекс Маркета — по образцу рабочего места Ozon.

Порядок тот же: сборщик сканирует штрихкод товара, панель находит заказ, в
котором этот товар нужен, показывает состав и держит заказ за сборщиком.
Дальше сканируются остальные товары, а закрывает сборку скан ярлыка.

Отличие одно, и оно в данных: Маркет штрихкодов товаров не отдаёт. В заказе
есть артикул продавца (offerId) — он же стоит у товара в каталоге Ozon,
загруженном в панель. По нему штрихкод и находится. Значит, чтобы сборка
Маркета сверяла товар, в панели должен быть кабинет Ozon с этим же каталогом:
без него любой скан товара окажется «неизвестным кодом».

Защита от тех же трёх ошибок: чужой товар — стоп, чужой ярлык — стоп,
собранное второй раз не открывается.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ...core import board as core_board
from ...core import access, db, pack_state, report
from . import client as yandex
from ...core.config import settings
from ..ozon.pack import ScanResult, barcode_variants
from .client import YandexError
from . import store

# Номер заказа Маркета — число. На ярлыке грузового места к нему дописан номер
# места через дефис: «12345678-1».
ORDER_ID_RE = re.compile(r"^\d{5,}$")
BOX_LABEL_RE = re.compile(r"^(\d{5,})-(\d{1,3})$")

# Ключ единицы в отчёте. У Маркета нет SKU, поэтому строка опознаётся по
# позиции заказа — иначе защите от дублей не за что зацепиться.
REPORT_PREFIX = "ym"


def order_id_from_code(code: str) -> str | None:
    """Номер заказа из скана ярлыка: сам номер или номер с грузовым местом."""
    code = (code or "").strip()
    if ORDER_ID_RE.match(code):
        return code
    box = BOX_LABEL_RE.match(code)
    return box.group(1) if box else None


# ------------------------------------------------------------------ опознание скана
def classify(account_id: int, code: str) -> tuple[str, Any]:
    """Что отсканировали: ярлык заказа, товар (по артикулам) или неизвестное."""
    variants = barcode_variants(code)
    if not variants:
        return "unknown", None
    number = order_id_from_code(code)
    if number and number not in variants:
        variants.append(number)

    marks = ",".join("?" for _ in variants)
    order = db.query_one(
        f"SELECT * FROM yandex_orders WHERE account_id = ? "
        f"AND (id IN ({marks}) OR external_id IN ({marks})) LIMIT 1",
        [account_id] + variants * 2,
    )
    if order:
        return "order", dict(order)

    # Штрихкод -> артикулы. Каталог берём из любого кабинета панели: артикул у
    # продавца один на все площадки, а заказ пришёл из Маркета, где каталога нет.
    rows = db.query(
        f"SELECT DISTINCT p.offer_id FROM product_barcodes b "
        f"JOIN products p ON p.account_id = b.account_id AND p.sku = b.sku "
        f"WHERE b.barcode IN ({marks}) AND p.offer_id IS NOT NULL AND p.offer_id != ''",
        variants,
    )
    offers = [row["offer_id"] for row in rows]
    if offers:
        return "product", offers

    # Отсканировали сам артикул — так бывает, когда наклейки с EAN нет.
    row = db.query_one(
        f"SELECT offer_id FROM yandex_order_items WHERE account_id = ? AND offer_id IN ({marks}) LIMIT 1",
        [account_id] + variants,
    )
    if row:
        return "product", [row["offer_id"]]

    if number:
        return "order_unknown", number
    return "unknown", code.strip()


# ------------------------------------------------------------------ состояние сборки
def _empty() -> dict:
    return {"active": None, "scanned": {}, "items": [], "done": 0, "total": 0, "complete": False}


def load_state(account: dict, user: dict) -> dict:
    """Активный заказ сборщика — только в текущем кабинете."""
    row = db.query_one(
        "SELECT * FROM pack_state WHERE user_id = ? AND account_id = ?", (user["id"], account["id"])
    )
    if not row or not row["posting_number"]:
        return _empty()
    order_row = db.query_one(
        "SELECT * FROM yandex_orders WHERE account_id = ? AND id = ?", (account["id"], row["posting_number"])
    )
    if not order_row:
        pack_state.clear(user)
        return _empty()

    scanned = json.loads(row["scanned"] or "{}")
    if not isinstance(scanned, dict):  # состояние от рабочего места Avito
        scanned = {}
    order = store.yandex_view(order_row)
    items = []
    done = total = 0
    for item in order["items"]:
        need = max(1, int(item.get("quantity") or 1))
        got = int(scanned.get(item["item_id"], 0))
        total += need
        done += min(got, need)
        items.append({**item, "need": need, "scanned": got, "ok": got >= need})
    return {
        "active": order,
        "scanned": scanned,
        "items": items,
        "done": done,
        "total": total,
        "complete": total > 0 and done >= total,
        "started_at": row["started_at"],
    }


def missing_items(state: dict) -> list[str]:
    """Чего не хватает до полной сборки — словами, которые видит сборщик."""
    return [
        f"{item.get('name') or item.get('offer_id') or item['item_id']} — {item['need'] - item['scanned']} шт"
        for item in state.get("items", [])
        if not item.get("ok")
    ]


def release(account: dict, user: dict, *, reason: str = "manual") -> ScanResult:
    """Отпустить активный заказ, ничего не завершая."""
    pack_state.release(user, event="yandex_pack_release", reason=reason)
    return ScanResult("ok", "Сборка отменена", action="released", state=load_state(account, user))


# ------------------------------------------------------------------ подбор заказа
def candidates_for_offers(account: dict, offers: list[str], user: dict) -> list[dict]:
    """Заказы в работе, где нужен товар с одним из артикулов, — самые срочные первыми."""
    if not offers:
        return []
    marks = ",".join("?" for _ in offers)
    subs = ",".join("?" for _ in yandex.WORK_SUBSTATUSES)
    rows = db.query(
        f"""
        SELECT DISTINCT o.* FROM yandex_orders o
        JOIN yandex_order_items i ON i.order_id = o.id AND i.account_id = o.account_id
        WHERE o.account_id = ? AND i.offer_id IN ({marks})
          AND o.substatus IN ({subs}) AND o.local_state != 'packed'
        ORDER BY (o.shipment_date IS NULL), o.shipment_date, o.id
        """,
        [account["id"]] + list(offers) + list(yandex.WORK_SUBSTATUSES),
    )
    result = []
    for row in rows:
        view = store.yandex_view(row)
        if view["claim_active"] and row["claim_user_id"] != user["id"]:
            view["locked_by"] = row["claim_login"]
        result.append(view)
    return result


def _offer_title(account_id: int, offers: list[str]) -> str:
    marks = ",".join("?" for _ in offers)
    row = db.query_one(
        f"SELECT name FROM yandex_order_items WHERE account_id = ? AND offer_id IN ({marks}) "
        f"AND name IS NOT NULL LIMIT 1",
        [account_id] + list(offers),
    )
    if row and row["name"]:
        return row["name"]
    row = db.query_one(
        f"SELECT name FROM products WHERE offer_id IN ({marks}) AND name IS NOT NULL LIMIT 1", list(offers)
    )
    return row["name"] if row and row["name"] else f"артикул {offers[0]}"


def _report_item(item: dict) -> dict:
    return {
        "key": f"{REPORT_PREFIX}:{item['item_id']}",
        "sku": None,
        "offer_id": item.get("offer_id"),
        "name": item.get("name"),
    }


# ------------------------------------------------------------------ основной вход
def scan(account: dict, user: dict, code: str) -> ScanResult:
    """Один скан рабочего места.

    Ярлык уходит на печать в одном случае: скан товара открыл заказ. При уже
    открытой сборке печати не бывает, какой бы код ни отсканировали, — иначе
    каждое подтверждение товара давало бы второй экземпляр ярлыка.
    """
    had_active = load_state(account, user)["active"] is not None
    result = _dispatch_scan(account, user, code)
    if had_active and result.get("print"):
        result["print"] = None
    return result


def _dispatch_scan(account: dict, user: dict, code: str) -> ScanResult:
    code = (code or "").strip()
    if not code:
        return ScanResult("error", "Пустой скан", state=load_state(account, user))

    kind, target = classify(account["id"], code)
    if kind == "order":
        return _scan_order(account, user, target, code)
    if kind == "product":
        return _scan_product(account, user, list(target), code)
    if kind == "order_unknown":
        db.log_event(
            "scan_unknown_posting", level="error", account_id=account["id"], user=user,
            barcode=code, posting_number=target,
        )
        return ScanResult(
            "error",
            f"Заказ {target} не найден в панели. Обновите список или проверьте склад.",
            action="unknown", state=load_state(account, user),
        )

    state = load_state(account, user)
    active = state["active"]
    with db.write() as conn:
        db.log_event(
            "scan_unknown", level="error", account_id=account["id"], user=user, barcode=code,
            message="Код не распознан", posting_number=(active or {}).get("id"), conn=conn,
        )
        if active:
            items = state.get("items") or []
            report.record_unmatched(
                conn, account, user, active["id"], code,
                name=items[0].get("name") if len(items) == 1 else None,
            )
        else:
            report.record_error(conn, account, user, "unknown_barcode", barcode=code)
    return ScanResult(
        "error",
        f"Код «{code}» не найден: это не товар из заказов и не наклейка заказа. "
        "Штрихкоды берутся из каталога по артикулу продавца — обновите каталог "
        "в разделе «Товары» и проверьте, что товар там есть.",
        action="unknown", state=state,
    )


# ------------------------------------------------------------------ скан товара
def _scan_product(account: dict, user: dict, offers: list[str], code: str) -> ScanResult:
    state = load_state(account, user)
    active = state["active"]
    name = _offer_title(account["id"], offers)

    if active:
        matching = [item for item in state["items"] if item.get("offer_id") in offers]
        if not matching:
            with db.write() as conn:
                db.log_event(
                    "scan_wrong_product", level="error", account_id=account["id"], user=user,
                    posting_number=active["id"], barcode=code, message="Товар не из активного заказа",
                    conn=conn,
                )
                report.record_error(
                    conn, account, user, "wrong_product", posting_number=active["id"],
                    barcode=code, name=name, offer_id=offers[0],
                )
            return ScanResult(
                "error",
                f"СТОП: «{name}» не входит в заказ {active['id']}. Уберите товар.",
                action="wrong_product", state=state,
            )

        # Один артикул может стоять в заказе двумя позициями — берём незакрытую.
        item = next((i for i in matching if not i["ok"]), matching[0])
        if item["ok"]:
            with db.write() as conn:
                db.log_event(
                    "scan_extra_product", level="warn", account_id=account["id"], user=user,
                    posting_number=active["id"], barcode=code, message="Повторный скан товара", conn=conn,
                )
                report.record_error(
                    conn, account, user, "extra_product", posting_number=active["id"],
                    barcode=code, name=item.get("name") or name, offer_id=item.get("offer_id"),
                )
            return ScanResult(
                "warning",
                f"«{item.get('name') or name}» уже отсканирован в нужном количестве ({item['need']} шт). "
                "Лишнее не кладите.",
                action="extra_product", sound="error", state=state,
            )

        scanned = dict(state["scanned"])
        scanned[item["item_id"]] = int(scanned.get(item["item_id"], 0)) + 1
        with db.write() as conn:
            pack_state.save(conn, account, user, active["id"], scanned)
            db.log_event(
                "scan_product", account_id=account["id"], user=user, posting_number=active["id"],
                barcode=code, message=f"{scanned[item['item_id']]}/{item['need']}", conn=conn,
            )
            # Отчёт пишется именно здесь: пара «штрихкод -> заказ» сошлась.
            report.record_shipped(
                conn, account, user, active["id"], _report_item(item), scanned[item["item_id"]], code,
            )
        new_state = load_state(account, user)
        if new_state["complete"]:
            return ScanResult(
                "ok",
                f"Все товары собраны ({new_state['done']}/{new_state['total']}). "
                "Наклейте и отсканируйте ярлык заказа.",
                action="ready_for_label", sound="done", state=new_state,
            )
        return ScanResult(
            "ok",
            f"«{item.get('name') or name}»: {scanned[item['item_id']]}/{item['need']}. "
            f"Собрано {new_state['done']} из {new_state['total']}.",
            action="product_scanned", state=new_state,
        )

    # Свободное рабочее место: подбираем заказ под товар.
    all_candidates = candidates_for_offers(account, offers, user)
    candidates = [c for c in all_candidates if not c.get("locked_by")]
    locked = [c for c in all_candidates if c.get("locked_by")]
    if not candidates:
        marks = ",".join("?" for _ in offers)
        packed = db.query_one(
            f"""
            SELECT COUNT(DISTINCT o.id) AS c FROM yandex_orders o
            JOIN yandex_order_items i ON i.order_id = o.id AND i.account_id = o.account_id
            WHERE o.account_id = ? AND i.offer_id IN ({marks}) AND o.local_state = 'packed'
            """,
            [account["id"]] + list(offers),
        )["c"]
        with db.write() as conn:
            db.log_event(
                "scan_no_candidates", level="warn", account_id=account["id"], user=user,
                barcode=code, message=name, conn=conn,
            )
            report.record_error(
                conn, account, user, "no_candidates", barcode=code, name=name, offer_id=offers[0],
            )
        if locked:
            return ScanResult(
                "warning",
                f"«{name}»: все подходящие заказы сейчас собирает {locked[0]['locked_by']}.",
                action="locked", sound="error", state=state,
            )
        if packed:
            return ScanResult(
                "warning",
                f"«{name}»: все заказы с этим товаром уже собраны ({packed} шт). Не собирайте повторно.",
                action="already_packed", sound="error", state=state,
            )
        return ScanResult(
            "error", f"«{name}» не нужен ни в одном заказе Маркета в работе.",
            action="no_candidates", sound="error", state=state,
        )

    # Товар нужен в нескольких заказах — берём самый срочный. Собранный выпадает
    # из подбора сам, и следующий скан того же штрихкода отдаёт следующий заказ.
    chosen = candidates[0]
    if len(candidates) > 1:
        db.log_event(
            "scan_choice", account_id=account["id"], user=user, barcode=code,
            message=f"{len(candidates)} заказов с этим товаром, взят {chosen['id']}",
        )
    first = next((i for i in chosen["items"] if i.get("offer_id") in offers), None)
    result = select_order(
        account, user, chosen["id"], first_item_id=first["item_id"] if first else None, scan_code=code,
    )
    if len(candidates) > 1 and result["status"] == "ok":
        left = len(candidates) - 1
        word = "заказе" if left % 10 == 1 and left % 100 != 11 else "заказах"
        result["message"] = (
            f"{result['message']} Этот товар нужен ещё в {left} {word} — "
            "отсканируйте его снова, когда закроете этот."
        )
    return result


# ------------------------------------------------------------------ выбор заказа
def select_order(account: dict, user: dict, order_id: str, *, first_item_id: str | None = None,
                 scan_code: str | None = None, label_in_hand: bool = False) -> ScanResult:
    """Взять заказ в сборку.

    label_in_hand — заказ открыли сканом самого ярлыка. Он уже распечатан и в
    руках у сборщика, второй раз печатать незачем.
    """
    row = db.query_one(
        "SELECT * FROM yandex_orders WHERE account_id = ? AND id = ?", (account["id"], order_id)
    )
    if not row:
        return ScanResult("error", f"Заказ {order_id} не найден", state=load_state(account, user))

    order = store.yandex_view(row)
    if order["local_state"] == "packed":
        return ScanResult(
            "warning", f"Заказ {order_id} уже собран ({order.get('packed_by') or '—'}).",
            action="already_packed", sound="error", state=load_state(account, user),
        )
    if order["substatus"] not in yandex.WORK_SUBSTATUSES:
        return ScanResult(
            "error", f"Заказ {order_id} в статусе «{order['status_label']}» — он не в работе.",
            action="wrong_status", state=load_state(account, user),
        )
    if order["claim_active"] and row["claim_user_id"] != user["id"]:
        return ScanResult(
            "error", f"Заказ {order_id} уже собирает {row['claim_login']}.",
            action="locked", state=load_state(account, user),
        )
    if not order["items"]:
        return ScanResult(
            "error", f"У заказа {order_id} нет состава — обновите данные из Маркета.",
            action="wrong_status", state=load_state(account, user),
        )

    previous = load_state(account, user)
    resumed = bool(previous["active"] and previous["active"]["id"] == order_id)
    scanned: dict[str, int] = dict(previous["scanned"]) if resumed else {}
    first_item = next((i for i in order["items"] if i["item_id"] == first_item_id), None)
    if first_item:
        scanned[first_item_id] = min(
            int(scanned.get(first_item_id, 0)) + 1, max(1, int(first_item.get("quantity") or 1))
        )

    now = db.now_iso()
    with db.write() as conn:
        pack_state.release_previous(conn, user, keep=(account["id"], order_id))
        conn.execute(
            "UPDATE yandex_orders SET claim_user_id = ?, claim_login = ?, claim_at = ? "
            "WHERE account_id = ? AND id = ?",
            (user["id"], user["login"], now, account["id"], order_id),
        )
        pack_state.save(conn, account, user, order_id, scanned)
        db.log_event(
            "yandex_pack_start", account_id=account["id"], user=user, posting_number=order_id,
            barcode=scan_code, message="Заказ взят в сборку", conn=conn,
        )
        if first_item and scanned.get(first_item_id):
            # Этот скан и выбрал заказ — пара сошлась, строка в отчёт.
            report.record_shipped(
                conn, account, user, order_id, _report_item(first_item), scanned[first_item_id], scan_code,
            )

    state = load_state(account, user)
    should_print = settings.autoprint and not label_in_hand and not resumed
    if state["complete"]:
        message = "Все товары собраны. Наклейте и отсканируйте ярлык заказа."
    else:
        message = f"Заказ {order_id}: соберите {state['total']} шт. Отсканировано {state['done']}."
    return ScanResult(
        "ok", message, action="order_selected", sound="ok", state=state,
        print=({"order_id": order_id} if should_print else None),
    )


# ------------------------------------------------------------------ скан ярлыка
def _scan_order(account: dict, user: dict, order_row: dict, code: str) -> ScanResult:
    order_id = order_row["id"]
    state = load_state(account, user)
    active = state["active"]

    if order_row.get("local_state") == "packed":
        db.log_event(
            "scan_packed_again", level="warn", account_id=account["id"], user=user,
            posting_number=order_id, barcode=code, message="Повторный скан собранного заказа",
        )
        return ScanResult(
            "warning",
            f"Заказ {order_id} уже собран ({order_row.get('packed_by') or '—'}, "
            f"{(order_row.get('packed_at') or '')[:16].replace('T', ' ')}). Повторно собирать не нужно.",
            action="already_packed", sound="error", state=state,
        )

    if active is None:
        return select_order(account, user, order_id, scan_code=code, label_in_hand=True)

    if active["id"] != order_id:
        with db.write() as conn:
            db.log_event(
                "scan_wrong_label", level="error", account_id=account["id"], user=user,
                posting_number=active["id"], barcode=code, message=f"Отсканирован ярлык {order_id}",
                conn=conn,
            )
            report.record_error(
                conn, account, user, "wrong_label", posting_number=active["id"], barcode=code,
                name=f"ярлык заказа {order_id}",
            )
        return ScanResult(
            "error", f"СТОП: это ярлык заказа {order_id}, а вы собираете {active['id']}.",
            action="wrong_label", state=state,
        )

    if settings.require_all_items and not state["complete"]:
        db.log_event(
            "scan_label_incomplete", level="warn", account_id=account["id"], user=user,
            posting_number=order_id, barcode=code, message="Ярлык отсканирован до сборки всех товаров",
        )
        return ScanResult(
            "warning", "Сначала отсканируйте все товары. Осталось: " + "; ".join(missing_items(state)),
            action="incomplete", sound="error", state=state,
        )
    return complete(account, user, order_id, code)


def complete(account: dict, user: dict, order_id: str, code: str | None = None) -> ScanResult:
    now = db.now_iso()
    with db.write() as conn:
        conn.execute(
            """
            UPDATE yandex_orders SET local_state = 'packed', packed_at = ?, packed_by = ?,
                claim_user_id = NULL, claim_login = NULL, claim_at = NULL
            WHERE account_id = ? AND id = ?
            """,
            (now, user["login"], account["id"], order_id),
        )
        db.log_event(
            "yandex_pack_complete", account_id=account["id"], user=user, posting_number=order_id,
            barcode=code, message="Заказ собран", conn=conn,
        )
        pack_state.clear(user, conn)
    return ScanResult(
        "ok", f"Готово: заказ {order_id} собран.", action="completed", sound="done",
        completed_order=order_id, state=load_state(account, user),
    )


def complete_active(account: dict, user: dict, reason: str = "ручное завершение") -> ScanResult:
    """«Завершить без скана ярлыка» — когда ярлык не читается сканером.

    Проверки здесь, а не в маршруте: рабочее место одно на все площадки, и что
    считать «рано завершать», знает только площадка. Отказ — ValueError с
    готовым текстом для оператора.
    """
    state = load_state(account, user)
    if not state["active"]:
        raise ValueError("Нет активного заказа")
    if settings.require_all_items and not state["complete"] and not access.is_manager(user):
        raise ValueError("Сначала отсканируйте все товары. Осталось: " + "; ".join(missing_items(state)))
    return complete(account, user, state["active"]["id"], code=reason)


def owner(account: dict, user: dict, code: str) -> tuple[str, str] | None:
    """Чей это код. None — не наш. Иначе (вид, номер заказа):

    * «label» — ярлык заказа этого кабинета;
    * «product» — товар, и вот какой заказ он откроет;
    * «known» — товар есть в заказах этого кабинета, но открыть сейчас нечего:
      всё собрано или собирает другой. Объяснить это может только этот кабинет.

    Штрихкод Маркет узнаёт по каталогу любого кабинета: артикул у продавца
    один на все площадки. Поэтому «товар знаком» здесь значит одно — он есть в
    заказах именно этого кабинета, иначе ответ «мой» давал бы каждый.

    Подбор тот же, что у скана (`candidates_for_offers`): ответ обязан
    совпасть с тем, что скан потом сделает.
    """
    kind, target = classify(account["id"], code)
    if kind == "order":
        return "label", str(target["id"])
    if kind != "product":
        # «Похоже на номер заказа, но его тут нет» — не наш: иначе кабинет
        # забирал бы себе чужие номера и отвечал за них «не найдено».
        return None

    offers = list(target)
    found = candidates_for_offers(account, offers, user)
    free = [c for c in found if not c.get("locked_by")]
    if free:
        return "product", str(free[0]["id"])
    if found:
        return "known", str(found[0]["id"])
    marks = ",".join("?" for _ in offers)
    row = db.query_one(
        f"SELECT o.id FROM yandex_orders o "
        f"JOIN yandex_order_items i ON i.order_id = o.id AND i.account_id = o.account_id "
        f"WHERE o.account_id = ? AND i.offer_id IN ({marks}) LIMIT 1",
        [account["id"]] + offers,
    )
    return ("known", str(row["id"])) if row else None


# ------------------------------------------------------------------ ярлыки
def labels(account: dict, user: dict, order_ids: list[str], *, mark: bool = True) -> tuple[bytes, str]:
    """Ярлыки заказов — файл Маркета как есть. mark — отметить печать.

    Один заказ печатается поштучным методом — он отвечает сразу. Пачка идёт
    массовым: Маркет собирает файл в фоне, и панель ждёт его. Если поштучный
    отказал (нет прав, метод недоступен), пачка из одного заказа выручит.
    Выгрузка архива до смены печать не отмечает: у неё своя отметка «скачано».
    """
    client = yandex.get_client(account)
    pdf: bytes | None = None
    if len(order_ids) == 1:
        row = db.query_one(
            "SELECT campaign_id FROM yandex_orders WHERE account_id = ? AND id = ?",
            (account["id"], order_ids[0]),
        )
        if row and row["campaign_id"]:
            try:
                pdf = client.order_labels(row["campaign_id"], order_ids[0])
            except YandexError:
                pdf = None
    if not pdf:
        pdf, _name = client.labels_pdf(order_ids)
    if mark:
        core_board.mark_printed(account, user, order_ids, event="yandex_label_print",
                                message="Ярлык отправлен на печать")
    return pdf, f"yandex-label-{order_ids[0]}.pdf" if len(order_ids) == 1 else "yandex-labels.pdf"


# ------------------------------------------------------------------- Яндекс Маркет
def pending_labels(account_id: int) -> list[str]:
    """Заказы Маркета в работе, чей ярлык ещё не выгружен.

    У Маркета ярлык есть с момента подтверждения заказа, а сборка в панели
    закрывается его сканом — значит, нужен он по каждому заказу в работе, и
    «Ожидает сборки», и «Ожидает отгрузки». Собранное замок не держит.
    """
    from . import client as yandex

    subs = ",".join("?" for _ in yandex.WORK_SUBSTATUSES)
    rows = db.query(
        f"SELECT id FROM yandex_orders WHERE account_id = ? AND substatus IN ({subs}) "
        "AND local_state != 'packed' AND label_saved_at IS NULL ORDER BY id",
        [account_id] + list(yandex.WORK_SUBSTATUSES),
    )
    return [row["id"] for row in rows]



