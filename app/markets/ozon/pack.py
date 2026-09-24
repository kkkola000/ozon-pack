"""Логика рабочего места сборщика.

Защита от трёх ошибок, ради которых всё и делается:
  1. взяли не тот товар      -> товар не из активного отправления = стоп;
  2. положили не в то место  -> стикер обязан совпасть с активным отправлением;
  3. собрали дважды          -> собранные отправления исключены из подбора,
                                повторный скан стикера даёт предупреждение.

Все запросы ограничены кабинетом (account_id): товар одного магазина никогда
не подойдёт к отправлению другого, даже если штрихкод совпадает.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ...core import access, db, product_sets, report
from ...core.config import settings
from . import client as ozon
from .client import OzonError
from . import store

POSTING_NUMBER_RE = re.compile(r"^\d{5,}-\d{3,}-\d{1,3}$")


class ScanResult(dict):
    """Ответ рабочего места (обычный dict — сериализуется в JSON как есть)."""

    def __init__(
        self,
        status: str,
        message: str,
        *,
        action: str = "noop",
        sound: str | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(status=status, message=message, action=action, sound=sound or status, **extra)


# ------------------------------------------------------------------ штрихкоды
def barcode_variants(code: str) -> list[str]:
    """Варианты одного и того же кода: EAN с ведущим нулём, GTIN из «Честного знака»."""
    code = (code or "").strip()
    if not code:
        return []
    variants = [code]
    digits = re.sub(r"\D", "", code)

    # DataMatrix маркировки: 01<GTIN-14>21<серийный номер>...
    if len(code) >= 16 and code[:2] == "01" and code[2:16].isdigit():
        gtin = code[2:16]
        variants += [gtin, gtin.lstrip("0"), gtin[1:] if gtin.startswith("0") else gtin]

    if digits and digits != code:
        variants.append(digits)
    if len(digits) == 13 and digits.startswith("0"):
        variants.append(digits[1:])
    if len(digits) == 12:
        variants.append("0" + digits)
    if len(digits) == 14 and digits.startswith("0"):
        variants.append(digits[1:])

    seen: list[str] = []
    for variant in variants:
        if variant and variant not in seen:
            seen.append(variant)
    return seen


def classify(account_id: int, code: str) -> tuple[str, Any]:
    """Определить, что отсканировали: стикер отправления, товар или неизвестное."""
    variants = barcode_variants(code)
    if not variants:
        return "unknown", None

    placeholders = ",".join("?" for _ in variants)
    posting = db.query_one(
        f"""
        SELECT * FROM postings
        WHERE account_id = ?
          AND (posting_number IN ({placeholders})
               OR barcode_upper IN ({placeholders})
               OR barcode_lower IN ({placeholders}))
        LIMIT 1
        """,
        [account_id] + variants * 3,
    )
    if posting:
        return "posting", posting

    barcode_row = db.query_one(
        f"SELECT sku FROM product_barcodes WHERE account_id = ? AND barcode IN ({placeholders}) LIMIT 1",
        [account_id] + variants,
    )
    if barcode_row:
        return "product", barcode_row["sku"]

    # Штучные случаи: отсканировали SKU или артикул продавца.
    item = db.query_one(
        f"SELECT sku FROM posting_items WHERE account_id = ? "
        f"AND (sku IN ({placeholders}) OR offer_id IN ({placeholders})) LIMIT 1",
        [account_id] + variants * 2,
    )
    if item:
        return "product", item["sku"]

    product = db.query_one(
        f"SELECT sku FROM products WHERE account_id = ? "
        f"AND (sku IN ({placeholders}) OR offer_id IN ({placeholders})) LIMIT 1",
        [account_id] + variants * 2,
    )
    if product:
        return "product", product["sku"]

    if POSTING_NUMBER_RE.match(code.strip()):
        return "posting_unknown", code.strip()
    return "unknown", code.strip()


# ------------------------------------------------------------------ состояние сборки
def load_state(account: dict, user: dict) -> dict:
    """Активное отправление сборщика — только в текущем кабинете."""
    row = db.query_one(
        "SELECT * FROM pack_state WHERE user_id = ? AND account_id = ?", (user["id"], account["id"])
    )
    if not row or not row["posting_number"]:
        return {"active": None, "scanned": {}, "items": [], "done": 0, "total": 0, "complete": False}

    posting_row = db.query_one(
        "SELECT * FROM postings WHERE account_id = ? AND posting_number = ?",
        (account["id"], row["posting_number"]),
    )
    if not posting_row:
        clear_state(user)
        return {"active": None, "scanned": {}, "items": [], "done": 0, "total": 0, "complete": False}

    scanned = json.loads(row["scanned"] or "{}")
    posting = store.posting_view(posting_row)
    sets = product_sets.parts_for(account["id"], [item["sku"] for item in posting["items"]])
    items = []
    done = total = 0
    for item in posting["items"]:
        need = int(item["quantity"])
        got = int(scanned.get(item["sku"], 0))
        row_extra: dict = {}
        if sets.get(item["sku"]):
            got, row_extra = _set_progress(item["sku"], need, got, sets[item["sku"]], scanned)
        total += need
        done += min(got, need)
        items.append({**item, "need": need, "scanned": got, "ok": got >= need, **row_extra})
    return {
        "active": posting,
        "scanned": scanned,
        "items": items,
        "done": done,
        "total": total,
        "complete": total > 0 and done >= total,
        "started_at": row["started_at"],
    }


def part_slot(set_sku: str, part_key: str) -> str:
    """Ключ прогресса по части набора внутри одного отправления."""
    return f"{set_sku}#{part_key}"


def missing_items(state: dict) -> list[str]:
    """Чего не хватает до полной сборки — словами, которые видит сборщик.

    У набора называем недостающие части, а не сам набор: названия набора мало,
    к полке с ним не пойдёшь. Набор на площадке — обычный товар, и что в нём
    внутри, знает только панель.
    """
    missing = []
    for item in state.get("items", []):
        if item.get("ok"):
            continue
        name = item.get("name") or item.get("sku")
        if item.get("is_set"):
            left = [f"{part['name']} — {part['need'] - part['scanned']} шт"
                    for part in item["parts"] if not part["ok"]]
            if left:
                missing.append(f"{name} (набор): " + ", ".join(left))
                continue
        missing.append(f"{name} — {item['need'] - item['scanned']} шт")
    return missing


def _set_progress(set_sku: str, need: int, direct: int,
                  parts: list[dict], scanned: dict) -> tuple[int, dict]:
    """Сколько наборов собрано и что ещё осталось взять с полки.

    Набор считается собранным, когда набраны все его части: по одной неполной
    части нельзя закрыть позицию, иначе в коробку уедет половина комплекта.
    Отсюда минимум по частям, а не сумма.

    Штрихкод самого набора тоже засчитывается (direct) — если такая наклейка на
    складе есть, сканировать части незачем. Тогда и частей нужно меньше.
    """
    from_parts = None
    left = max(0, need - direct)
    rows = []
    for part in parts:
        per_set = max(1, int(part.get("quantity") or 1))
        got = int(scanned.get(part_slot(set_sku, part["part_key"]), 0))
        part_need = per_set * left
        from_parts = got // per_set if from_parts is None else min(from_parts, got // per_set)
        rows.append({
            "part_key": part["part_key"],
            "sku": part.get("part_sku"),
            "barcode": part.get("barcode"),
            "name": product_sets.part_title(part),
            "image": part.get("image"),
            "per_set": per_set,
            "need": part_need,
            "scanned": got,
            "ok": got >= part_need,
        })
    total = direct + min(from_parts or 0, left)
    return total, {"is_set": True, "parts": rows}


def clear_state(user: dict, conn=None) -> None:
    sql = "DELETE FROM pack_state WHERE user_id = ?"
    if conn is not None:
        conn.execute(sql, (user["id"],))
    else:
        db.execute(sql, (user["id"],))


def _release_previous(conn, user: dict, *, keep: tuple[int, str] | None = None) -> tuple[int, str] | None:
    """Снять бронь с отправления, которое сборщик держал раньше.

    Строка pack_state одна на сборщика, а кабинетов несколько: если человек
    переключил кабинет посреди сборки, бронь в прежнем кабинете надо отпустить,
    иначе отправление зависнет до истечения CLAIM_TTL_MINUTES.
    """
    row = conn.execute(
        "SELECT account_id, posting_number FROM pack_state WHERE user_id = ?", (user["id"],)
    ).fetchone()
    if not row or not row["posting_number"]:
        return None
    previous = (row["account_id"], row["posting_number"])
    if keep is not None and previous == keep:
        return None
    conn.execute(
        "UPDATE postings SET claim_user_id = NULL, claim_login = NULL, claim_at = NULL "
        "WHERE account_id = ? AND posting_number = ?",
        previous,
    )
    return previous


def _save_state(conn, account: dict, user: dict, posting_number: str, scanned: dict) -> None:
    now = db.now_iso()
    conn.execute(
        """
        INSERT INTO pack_state(user_id, account_id, posting_number, scanned, started_at, updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET account_id = excluded.account_id,
            posting_number = excluded.posting_number,
            scanned = excluded.scanned, updated_at = excluded.updated_at,
            started_at = CASE WHEN pack_state.posting_number = excluded.posting_number
                              THEN pack_state.started_at ELSE excluded.started_at END
        """,
        (user["id"], account["id"], posting_number, json.dumps(scanned, ensure_ascii=False), now, now),
    )


def release(account: dict, user: dict, *, reason: str = "manual") -> ScanResult:
    """Отпустить активное отправление, ничего не завершая."""
    with db.write() as conn:
        released = _release_previous(conn, user)
        if released:
            db.log_event(
                "pack_release",
                account_id=released[0],
                user=user,
                posting_number=released[1],
                message=f"Сборка отменена ({reason})",
                conn=conn,
            )
        clear_state(user, conn)
    return ScanResult("ok", "Сборка отменена", action="released", state=load_state(account, user))


# ------------------------------------------------------------------ подбор отправлений
def candidates_for_sku(account: dict, sku: str, user: dict) -> list[dict]:
    rows = db.query(
        """
        SELECT p.* FROM postings p
        JOIN posting_items i ON i.posting_number = p.posting_number AND i.account_id = p.account_id
        WHERE p.account_id = ? AND i.sku = ? AND p.status = ? AND p.local_state = 'new'
        ORDER BY (p.shipment_date IS NULL), p.shipment_date
        """,
        (account["id"], sku, store.STATUS_AWAITING_DELIVER),
    )
    result = []
    for row in rows:
        view = store.posting_view(row)
        if view["claim_active"] and row["claim_user_id"] != user["id"]:
            view["locked_by"] = row["claim_login"]
        result.append(view)
    return result


def openable(account: dict, user: dict, sku: str | None, code: str) -> list[dict]:
    """Отправления, которые откроет скан этого товара, — самые срочные первыми.

    Товар может быть и сам по себе, и частью набора — тогда годятся
    отправления обоих видов. Одна функция на скан и на вопрос «чей это код»:
    ответ ядру обязан совпадать с тем, что потом сделает скан, иначе скан
    уйдёт не в тот кабинет.
    """
    found = candidates_for_sku(account, sku, user) if sku else []
    seen = {c["posting_number"] for c in found}
    for parent in product_sets.parents_of(account["id"], sku=sku, barcodes=barcode_variants(code)):
        for candidate in candidates_for_sku(account, parent["set_sku"], user):
            if candidate["posting_number"] not in seen:
                seen.add(candidate["posting_number"])
                # Помечаем, что отправление подошло не самим товаром, а набором:
                # засчитывать по такому скану целый набор нельзя.
                found.append({**candidate, "via_set": parent["set_sku"]})
    return found


def _sku_offer(account_id: int, sku: str) -> str | None:
    """Артикул продавца по SKU — в отчёте по нему опознают товар."""
    for table in ("products", "posting_items"):
        row = db.query_one(
            f"SELECT offer_id FROM {table} WHERE account_id = ? AND sku = ? AND offer_id IS NOT NULL LIMIT 1",
            (account_id, sku),
        )
        if row and row["offer_id"]:
            return row["offer_id"]
    return None


def _sku_title(account_id: int, sku: str) -> str:
    row = db.query_one("SELECT name FROM products WHERE account_id = ? AND sku = ?", (account_id, sku))
    if row and row["name"]:
        return row["name"]
    row = db.query_one(
        "SELECT name FROM posting_items WHERE account_id = ? AND sku = ? LIMIT 1", (account_id, sku)
    )
    return (row["name"] if row and row["name"] else f"SKU {sku}")


# ------------------------------------------------------------------ основной вход
def scan(account: dict, user: dict, code: str) -> ScanResult:
    """Один скан рабочего места.

    Стикер уходит на печать ровно в одном случае: отсканировали штрихкод товара,
    когда открытой сборки нет. Здесь это закреплено на входе: если сборка была
    открыта до скана, печати не будет, какой бы код ни отсканировали. Иначе
    сборщик, подтверждая товар или закрывая отправление, каждый раз получал бы
    второй экземпляр стикера.
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
    if kind == "posting":
        return _scan_posting(account, user, dict(target), code)
    if kind == "product":
        return _scan_product(account, user, str(target), code)
    if kind == "posting_unknown":
        return _scan_unknown_posting(account, user, target, code)

    # Часть набора, которой нет в каталоге площадки: своего SKU у неё не
    # бывает, поэтому classify до сюда её и доводит. Для сборщика это обычный
    # товар с полки, и «код не распознан» было бы неправдой.
    part = _scan_set_part(account, user, code, sku=None)
    if part is not None:
        return part

    # Последняя попытка: спросить Ozon по штрихкоду стикера.
    posting = _fetch_by_barcode(account, code)
    if posting:
        return _scan_posting(account, user, posting, code)

    state = load_state(account, user)
    active = state["active"]
    with db.write() as conn:
        db.log_event(
            "scan_unknown", level="error", account_id=account["id"], user=user, barcode=code,
            message="Код не распознан", posting_number=(active or {}).get("posting_number"), conn=conn,
        )
        if active:
            # Штрихкода нет в справочнике — так бывает у Avito. В отчёт всё равно
            # пишем: что отсканировали, в какое отправление и что там за товар.
            report.record_unmatched(
                conn, account, user, active["posting_number"], code, name=_single_item_name(state)
            )
        else:
            report.record_error(conn, account, user, "unknown_barcode", barcode=code)
    return ScanResult(
        "error",
        f"Код «{code}» не найден: это не товар из заданий и не стикер отправления",
        action="unknown",
        state=state,
    )


def _scan_set_part(account: dict, user: dict, code: str, *, sku: str | None) -> ScanResult | None:
    """Отсканирована часть набора. None — это не часть, идём дальше по обычному пути.

    Набор на Ozon — обычный товар, и в отправлении он стоит одной позицией. На
    складе его собирают из нескольких вещей, и сборщик сканирует именно их:
    наклейки набора на полке нет. Значит, скан части — это работа по позиции
    набора, а не чужой товар.
    """
    parents = product_sets.parents_of(
        account["id"], sku=sku, barcodes=barcode_variants(code)
    )
    if not parents:
        return None

    state = load_state(account, user)
    active = state["active"]
    if not active:
        return _pick_posting_for_set(account, user, parents, code, sku)

    by_sku = {item["sku"]: item for item in state["items"] if item.get("is_set")}
    # Часть может входить в несколько наборов. Берём тот, что есть в этом
    # отправлении и ещё не собран: иначе скан уйдёт в уже закрытую позицию.
    usable = [p for p in parents if p["set_sku"] in by_sku]
    if not usable:
        return None
    parent = next((p for p in usable if not by_sku[p["set_sku"]]["ok"]), usable[0])
    item = by_sku[parent["set_sku"]]
    part = next(p for p in item["parts"] if p["part_key"] == parent["part_key"])
    name = part["name"]
    set_name = item.get("name") or parent["set_sku"]

    if item["ok"] or part["scanned"] >= part["need"]:
        with db.write() as conn:
            db.log_event(
                "scan_extra_product", level="warn", account_id=account["id"], user=user,
                posting_number=active["posting_number"], sku=parent["set_sku"], barcode=code,
                message=f"Часть набора сверх нужного: {name}", conn=conn,
            )
            report.record_error(
                conn, account, user, "extra_product",
                posting_number=active["posting_number"], barcode=code, sku=parent["set_sku"],
                name=f"{set_name} — {name}", offer_id=item.get("offer_id"),
            )
        return ScanResult(
            "warning",
            f"«{name}» для набора «{set_name}» уже набран ({part['need']} шт). Лишнее не кладите.",
            action="extra_product", sound="error", state=state,
        )

    scanned = dict(state["scanned"])
    slot = part_slot(parent["set_sku"], parent["part_key"])
    scanned[slot] = int(scanned.get(slot, 0)) + 1
    was_done = item["scanned"]
    with db.write() as conn:
        _save_state(conn, account, user, active["posting_number"], scanned)
        db.log_event(
            "scan_set_part", account_id=account["id"], user=user,
            posting_number=active["posting_number"], sku=parent["set_sku"], barcode=code,
            message=f"{set_name}: {name} {scanned[slot]}/{part['need']}", conn=conn,
        )

    new_state = load_state(account, user)
    new_item = next(i for i in new_state["items"] if i["sku"] == parent["set_sku"])
    if new_item["scanned"] > was_done:
        # Набор собран целиком — только теперь позиция зачтена в отчёт. Писать
        # туда каждую часть нельзя: отгружен набор, а не его содержимое.
        with db.write() as conn:
            report.record_shipped(
                conn, account, user, active["posting_number"],
                {"sku": parent["set_sku"], "name": set_name, "offer_id": new_item.get("offer_id")},
                new_item["scanned"], code,
            )
        new_state = load_state(account, user)
        new_item = next(i for i in new_state["items"] if i["sku"] == parent["set_sku"])

    if new_state["complete"]:
        return ScanResult(
            "ok",
            f"Все товары собраны ({new_state['done']}/{new_state['total']}). "
            "Наклейте и отсканируйте стикер отправления.",
            action="ready_for_label", sound="done", state=new_state,
        )
    left = [p["name"] for p in new_item["parts"] if not p["ok"]]
    tail = f" Осталось: {', '.join(left)}." if left else ""
    return ScanResult(
        "ok",
        f"«{set_name}»: {name} {scanned[slot]}/{part['need']}. "
        f"Набор {new_item['scanned']}/{new_item['need']}.{tail}",
        action="set_part_scanned", state=new_state,
    )


def _pick_posting_for_set(account: dict, user: dict, parents: list[dict], code: str,
                          sku: str | None) -> ScanResult | None:
    """Свободное место, отсканирована часть набора — ищем отправление с набором.

    Сюда доходят части, у которых своего товара в каталоге нет: у части-товара
    отправление подбирается обычным путём, там набор идёт вторым вариантом.

    Отправление берём «пустым», а скан проводим как часть: засчитать целый
    набор по одной части нельзя.
    """
    seen: list[str] = []
    for parent in parents:
        if parent["set_sku"] not in seen:
            seen.append(parent["set_sku"])
    for set_sku in seen:
        candidates = [c for c in candidates_for_sku(account, set_sku, user) if not c.get("locked_by")]
        if not candidates:
            continue
        result = select_posting(account, user, candidates[0]["posting_number"], scan_code=code)
        if result["status"] != "ok":
            return result
        credited = _scan_set_part(account, user, code, sku=sku)
        if credited is not None:
            result["state"] = credited["state"]
            result["message"] = f"{result['message']} {credited['message']}"
        return result
    return None


def _single_item_name(state: dict) -> str | None:
    """Название товара, если в отправлении он один — иначе угадывать нечего."""
    items = state.get("items") or []
    return items[0].get("name") if len(items) == 1 else None


def _fetch_by_barcode(account: dict, code: str) -> dict | None:
    """Штрихкод стикера может быть неизвестен локально — спрашиваем Ozon."""
    try:
        raw = ozon.get_client(account).posting_by_barcode(code)
    except OzonError:
        return None
    if not raw:
        return None
    with db.write() as conn:
        store.upsert_posting(conn, account["id"], raw)
    row = db.query_one(
        "SELECT * FROM postings WHERE account_id = ? AND posting_number = ?",
        (account["id"], raw["posting_number"]),
    )
    return dict(row) if row else None


def _scan_unknown_posting(account: dict, user: dict, number: str, code: str) -> ScanResult:
    posting = _fetch_by_barcode(account, number)
    if posting:
        return _scan_posting(account, user, posting, code)
    db.log_event(
        "scan_unknown_posting", level="error", account_id=account["id"], user=user,
        barcode=code, posting_number=number,
    )
    return ScanResult(
        "error",
        f"Отправление {number} не найдено в панели. Обновите список или проверьте склад.",
        action="unknown",
        state=load_state(account, user),
    )


# ------------------------------------------------------------------ скан товара
def _scan_product(account: dict, user: dict, sku: str, code: str) -> ScanResult:
    state = load_state(account, user)
    active = state["active"]
    name = _sku_title(account["id"], sku)

    if active:
        required = {item["sku"]: item["need"] for item in state["items"]}
        by_sku = {item["sku"]: item for item in state["items"]}
        if sku not in required:
            # Прежде чем говорить «СТОП»: это может быть часть набора из этого
            # же отправления. Сборщик берёт с полки части, а не набор — на
            # складе такой наклейки просто нет.
            part = _scan_set_part(account, user, code, sku=sku)
            if part is not None:
                return part
            with db.write() as conn:
                db.log_event(
                    "scan_wrong_product",
                    level="error",
                    account_id=account["id"],
                    user=user,
                    posting_number=active["posting_number"],
                    sku=sku,
                    barcode=code,
                    message="Товар не из активного отправления",
                    conn=conn,
                )
                report.record_error(
                    conn, account, user, "wrong_product",
                    posting_number=active["posting_number"], barcode=code, sku=sku, name=name,
                    offer_id=_sku_offer(account["id"], sku),
                )
            return ScanResult(
                "error",
                f"СТОП: «{name}» не входит в отправление {active['posting_number']}. Уберите товар.",
                action="wrong_product",
                state=state,
            )

        scanned = dict(state["scanned"])
        already = int(scanned.get(sku, 0))
        if already >= required[sku]:
            with db.write() as conn:
                db.log_event(
                    "scan_extra_product",
                    level="warn",
                    account_id=account["id"],
                    user=user,
                    posting_number=active["posting_number"],
                    sku=sku,
                    barcode=code,
                    message="Повторный скан товара",
                    conn=conn,
                )
                report.record_error(
                    conn, account, user, "extra_product",
                    posting_number=active["posting_number"], barcode=code, sku=sku,
                    name=name, offer_id=(by_sku.get(sku) or {}).get("offer_id"),
                )
            return ScanResult(
                "warning",
                f"«{name}» уже отсканирован в нужном количестве ({required[sku]} шт). Лишнее не кладите.",
                action="extra_product",
                sound="error",
                state=state,
            )

        scanned[sku] = already + 1
        with db.write() as conn:
            _save_state(conn, account, user, active["posting_number"], scanned)
            db.log_event(
                "scan_product",
                account_id=account["id"],
                user=user,
                posting_number=active["posting_number"],
                sku=sku,
                barcode=code,
                message=f"{scanned[sku]}/{required[sku]}",
                conn=conn,
            )
            # Отчёт об отгруженных пишется именно здесь: пара «штрихкод -> отправление» сошлась.
            report.record_shipped(
                conn, account, user, active["posting_number"],
                by_sku.get(sku) or {"sku": sku, "name": name}, scanned[sku], code,
            )
        new_state = load_state(account, user)
        if new_state["complete"]:
            return ScanResult(
                "ok",
                f"Все товары собраны ({new_state['done']}/{new_state['total']}). Наклейте и отсканируйте стикер отправления.",
                action="ready_for_label",
                sound="done",
                state=new_state,
            )
        return ScanResult(
            "ok",
            f"«{name}»: {scanned[sku]}/{required[sku]}. Собрано {new_state['done']} из {new_state['total']}.",
            action="product_scanned",
            state=new_state,
        )

    all_candidates = openable(account, user, sku, code)
    candidates = [c for c in all_candidates if not c.get("locked_by")]
    locked = [c for c in all_candidates if c.get("locked_by")]
    if not candidates:
        packed = db.query_one(
            """
            SELECT COUNT(*) AS c FROM postings p
            JOIN posting_items i ON i.posting_number = p.posting_number AND i.account_id = p.account_id
            WHERE p.account_id = ? AND i.sku = ? AND p.local_state = 'packed'
            """,
            (account["id"], sku),
        )["c"]
        waiting = db.query_one(
            """
            SELECT COUNT(*) AS c FROM postings p
            JOIN posting_items i ON i.posting_number = p.posting_number AND i.account_id = p.account_id
            WHERE p.account_id = ? AND i.sku = ? AND p.status = ?
            """,
            (account["id"], sku, store.STATUS_AWAITING_PACKAGING),
        )["c"]
        with db.write() as conn:
            db.log_event(
                "scan_no_candidates", level="warn", account_id=account["id"], user=user,
                sku=sku, barcode=code, message=name, conn=conn,
            )
            report.record_error(
                conn, account, user, "no_candidates", barcode=code, sku=sku, name=name,
                offer_id=_sku_offer(account["id"], sku),
            )
        if locked:
            return ScanResult(
                "warning",
                f"«{name}»: все подходящие отправления сейчас собирает {locked[0]['locked_by']}.",
                action="locked",
                sound="error",
                state=state,
            )
        if waiting:
            return ScanResult(
                "warning",
                f"«{name}»: {waiting} отправл. с этим товаром ещё в статусе «Ожидает сборки». "
                "Сначала соберите их на вкладке «Ожидает сборки».",
                action="needs_ship",
                sound="error",
                state=state,
            )
        if packed:
            return ScanResult(
                "warning",
                f"«{name}»: все отправления с этим товаром уже собраны ({packed} шт). Не собирайте повторно.",
                action="already_packed",
                sound="error",
                state=state,
            )
        return ScanResult(
            "error",
            f"«{name}» не нужен ни в одном отправлении к отгрузке.",
            action="no_candidates",
            sound="error",
            state=state,
        )

    # Товар нужен в нескольких отправлениях — берём самое срочное и печатаем его
    # стикер. Выбирать сборщику нечего: порядок всё равно один, по сроку
    # отгрузки. Собранное отправление выпадает из подбора само, и следующий скан
    # того же штрихкода отдаёт следующее по очереди.
    chosen = candidates[0]
    if len(candidates) > 1:
        db.log_event(
            "scan_choice", account_id=account["id"], user=user, sku=sku, barcode=code,
            message=f"{len(candidates)} отправлений с этим товаром, взято {chosen['posting_number']}",
        )
    if chosen.get("via_set"):
        # Отсканирована часть набора: отправление берём, но целый набор по
        # одной части не засчитываем — он закрывается, только когда набраны
        # все части. Поэтому берём отправление «пустым» и тут же проводим скан
        # как часть, уже по обычному пути.
        result = select_posting(account, user, chosen["posting_number"], scan_code=code)
        if result["status"] == "ok":
            credited = _scan_set_part(account, user, code, sku=sku)
            if credited is not None:
                result["state"] = credited["state"]
                result["message"] = f"{result['message']} {credited['message']}"
    else:
        result = select_posting(
            account, user, chosen["posting_number"], first_sku=sku, scan_code=code
        )
    if len(candidates) > 1 and result["status"] == "ok":
        # Говорим, сколько ещё впереди: сборщик должен понимать, что отсканирует
        # этот штрихкод снова и получит следующее отправление, а не дубль.
        result["message"] = (
            f"{result['message']} Этот товар нужен ещё в {len(candidates) - 1} отправл. — "
            "отсканируйте его снова, когда закроете это."
        )
    return result


# ------------------------------------------------------------------ выбор отправления
def select_posting(account: dict, user: dict, posting_number: str, *, first_sku: str | None = None,
                   scan_code: str | None = None, label_in_hand: bool = False) -> ScanResult:
    """Взять отправление в сборку.

    label_in_hand — отправление открыли сканом самого стикера. Значит стикер
    уже распечатан и в руках у сборщика, и отправлять его на печать второй раз
    незачем: это лишняя бумага и лишний повод перепутать.
    """
    row = db.query_one(
        "SELECT * FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting_number)
    )
    if not row:
        return ScanResult("error", f"Отправление {posting_number} не найдено", state=load_state(account, user))

    posting = store.posting_view(row)
    if posting["local_state"] == "packed":
        return ScanResult(
            "warning",
            f"Отправление {posting_number} уже собрано ({posting.get('packed_by') or '—'}).",
            action="already_packed",
            sound="error",
            state=load_state(account, user),
        )
    if posting["status"] == store.STATUS_AWAITING_PACKAGING:
        if settings.auto_ship_on_scan:
            ship_result = ship_posting(account, user, posting_number)
            if ship_result["status"] != "ok":
                return ScanResult(
                    ship_result["status"], ship_result["message"], sound="error", state=load_state(account, user)
                )
            row = db.query_one(
                "SELECT * FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting_number)
            )
            posting = store.posting_view(row)
        else:
            return ScanResult(
                "warning",
                f"Отправление {posting_number} в статусе «Ожидает сборки». Сначала соберите его на вкладке заказов.",
                action="needs_ship",
                sound="error",
                state=load_state(account, user),
            )
    if posting["status"] != store.STATUS_AWAITING_DELIVER:
        return ScanResult(
            "error",
            f"Отправление {posting_number} в статусе «{posting['status_label']}» — оно не в работе.",
            action="wrong_status",
            state=load_state(account, user),
        )
    if posting["claim_active"] and row["claim_user_id"] != user["id"]:
        return ScanResult(
            "error",
            f"Отправление {posting_number} уже собирает {row['claim_login']}.",
            action="locked",
            state=load_state(account, user),
        )

    previous = load_state(account, user)
    resumed = bool(previous["active"] and previous["active"]["posting_number"] == posting_number)
    scanned: dict[str, int] = {}
    if resumed:
        scanned = dict(previous["scanned"])
    if first_sku:
        scanned[first_sku] = min(
            scanned.get(first_sku, 0) + 1,
            next((i["quantity"] for i in posting["items"] if i["sku"] == first_sku), 1),
        )

    now = db.now_iso()
    with db.write() as conn:
        _release_previous(conn, user, keep=(account["id"], posting_number))
        conn.execute(
            "UPDATE postings SET claim_user_id = ?, claim_login = ?, claim_at = ? "
            "WHERE account_id = ? AND posting_number = ?",
            (user["id"], user["login"], now, account["id"], posting_number),
        )
        _save_state(conn, account, user, posting_number, scanned)
        db.log_event(
            "pack_start",
            account_id=account["id"],
            user=user,
            posting_number=posting_number,
            sku=first_sku,
            barcode=scan_code,
            message="Отправление взято в сборку",
            conn=conn,
        )
        if first_sku and scanned.get(first_sku):
            # Этот скан и выбрал отправление — пара сошлась, строка в отчёт.
            item = next((i for i in posting["items"] if i["sku"] == first_sku), {"sku": first_sku})
            report.record_shipped(
                conn, account, user, posting_number, item, scanned[first_sku], scan_code
            )

    state = load_state(account, user)
    # Стикер печатаем только когда его нет на руках. Пришли сюда со скана
    # товара — печатаем; со скана стикера — он уже распечатан; вернулись в уже
    # открытую сборку — тем более, стикер для неё печатался при её открытии.
    should_print = settings.autoprint and not label_in_hand and not resumed
    if state["complete"]:
        message = "Все товары собраны. Наклейте и отсканируйте стикер отправления."
    else:
        message = f"Отправление {posting_number}: соберите {state['total']} шт. Отсканировано {state['done']}."
    return ScanResult(
        "ok",
        message,
        action="posting_selected",
        sound="ok",
        state=state,
        print=({"posting_number": posting_number} if should_print else None),
    )


# ------------------------------------------------------------------ скан стикера
def _scan_posting(account: dict, user: dict, posting_row: dict, code: str) -> ScanResult:
    posting_number = posting_row["posting_number"]
    state = load_state(account, user)
    active = state["active"]

    if posting_row.get("local_state") == "packed":
        db.log_event(
            "scan_packed_again",
            level="warn",
            account_id=account["id"],
            user=user,
            posting_number=posting_number,
            barcode=code,
            message="Повторный скан собранного отправления",
        )
        return ScanResult(
            "warning",
            f"Отправление {posting_number} уже собрано "
            f"({posting_row.get('packed_by') or '—'}, {(posting_row.get('packed_at') or '')[:16].replace('T', ' ')}). "
            "Повторно собирать не нужно.",
            action="already_packed",
            sound="error",
            state=state,
        )

    if active is None:
        # Открыли сканом стикера — он уже на руках, печатать заново не нужно.
        return select_posting(account, user, posting_number, scan_code=code, label_in_hand=True)

    if active["posting_number"] != posting_number:
        with db.write() as conn:
            db.log_event(
                "scan_wrong_label",
                level="error",
                account_id=account["id"],
                user=user,
                posting_number=active["posting_number"],
                barcode=code,
                message=f"Отсканирован стикер {posting_number}",
                conn=conn,
            )
            report.record_error(
                conn, account, user, "wrong_label",
                posting_number=active["posting_number"], barcode=code,
                name=f"стикер отправления {posting_number}",
            )
        return ScanResult(
            "error",
            f"СТОП: это стикер отправления {posting_number}, а вы собираете {active['posting_number']}.",
            action="wrong_label",
            state=state,
        )

    if settings.require_all_items and not state["complete"]:
        missing = missing_items(state)
        db.log_event(
            "scan_label_incomplete",
            level="warn",
            account_id=account["id"],
            user=user,
            posting_number=posting_number,
            barcode=code,
            message="Стикер отсканирован до сборки всех товаров",
        )
        return ScanResult(
            "warning",
            "Сначала отсканируйте все товары. Осталось: " + "; ".join(missing),
            action="incomplete",
            sound="error",
            state=state,
        )

    return complete(account, user, posting_number, code)


def complete(account: dict, user: dict, posting_number: str, code: str | None = None) -> ScanResult:
    now = db.now_iso()
    with db.write() as conn:
        conn.execute(
            """
            UPDATE postings SET local_state = 'packed', packed_at = ?, packed_by = ?,
                claim_user_id = NULL, claim_login = NULL, claim_at = NULL
            WHERE account_id = ? AND posting_number = ?
            """,
            (now, user["login"], account["id"], posting_number),
        )
        db.log_event(
            "pack_complete",
            account_id=account["id"],
            user=user,
            posting_number=posting_number,
            barcode=code,
            message="Отправление собрано",
            conn=conn,
        )
        clear_state(user, conn)
    return ScanResult(
        "ok",
        f"Готово: отправление {posting_number} собрано.",
        action="completed",
        sound="done",
        completed_posting=posting_number,
        state=load_state(account, user),
    )


def complete_active(account: dict, user: dict, reason: str = "ручное завершение") -> ScanResult:
    """«Завершить без скана стикера» — когда стикер не читается сканером.

    Проверки здесь, а не в маршруте: рабочее место одно на все площадки, и что
    считать «рано завершать», знает только площадка. Отказ — ValueError с
    готовым текстом для оператора.
    """
    state = load_state(account, user)
    if not state["active"]:
        raise ValueError("Нет активного отправления")
    if settings.require_all_items and not state["complete"] and not access.is_manager(user):
        # Говорим, чего именно не хватает: у набора — недостающие части, а не
        # его название. К полке с названием набора не пойдёшь.
        raise ValueError("Сначала отсканируйте все товары. Осталось: " + "; ".join(missing_items(state)))
    return complete(account, user, state["active"]["posting_number"], code=reason)


def owner(account: dict, user: dict, code: str) -> tuple[str, str] | None:
    """Чей это код. None — не наш. Иначе (вид, номер отправления):

    * «label» — стикер отправления этого кабинета;
    * «product» — товар, и вот какое отправление он откроет;
    * «known» — товар наш, но открыть по нему сейчас нечего: отправление ещё
      «Ожидает сборки», уже собрано или его собирает другой. Объяснить это
      может только этот кабинет — иначе сборщик услышит «не нужен» от кабинета,
      где товара и не было. Номер — отправление, о котором пойдёт речь, или
      пусто, если товар есть только в каталоге.

    Спрашивается до скана и ничего не меняет. Подбор тот же, что у скана
    (`openable`), — ответ обязан совпасть с тем, что скан потом сделает. Ozon
    по штрихкоду стикера здесь не спрашиваем: это поход в сеть, а вопрос
    задаётся каждому кабинету подряд.
    """
    kind, target = classify(account["id"], code)
    if kind == "posting":
        return "label", str(target["posting_number"])
    sku = str(target) if kind == "product" else None
    if sku is None and not product_sets.parents_of(account["id"], sku=None, barcodes=barcode_variants(code)):
        # Ни товар, ни часть набора. «Похоже на номер отправления, но его тут
        # нет» — тоже не наш: иначе кабинет забирал бы себе чужие номера.
        return None

    found = openable(account, user, sku, code)
    free = [c for c in found if not c.get("locked_by")]
    if free:
        return "product", str(free[0]["posting_number"])
    if found:
        return "known", str(found[0]["posting_number"])
    row = db.query_one(
        """
        SELECT p.posting_number FROM postings p
        JOIN posting_items i ON i.posting_number = p.posting_number AND i.account_id = p.account_id
        WHERE p.account_id = ? AND i.sku = ?
        ORDER BY (p.status != ?), (p.shipment_date IS NULL), p.shipment_date LIMIT 1
        """,
        (account["id"], sku or "", store.STATUS_AWAITING_PACKAGING),
    )
    return "known", (str(row["posting_number"]) if row else "")


# ------------------------------------------------------------------ сборка на стороне Ozon
def ship_posting(account: dict, user: dict, posting_number: str) -> dict:
    """Перевести отправление в «Ожидает отгрузки» (v4/posting/fbs/ship)."""
    row = db.query_one(
        "SELECT * FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting_number)
    )
    if not row:
        return {"status": "error", "message": f"Отправление {posting_number} не найдено"}
    if row["status"] == store.STATUS_AWAITING_DELIVER:
        return {"status": "ok", "message": f"{posting_number}: уже «Ожидает отгрузки»", "postings": [posting_number]}
    if row["status"] != store.STATUS_AWAITING_PACKAGING:
        return {
            "status": "error",
            "message": f"{posting_number}: статус «{store.STATUS_LABELS.get(row['status'], row['status'])}», сборка невозможна",
        }

    items = store.posting_items(account["id"], posting_number)
    if not items:
        return {"status": "error", "message": f"{posting_number}: нет состава заказа, обновите данные"}
    try:
        # Ozon ждёт product_id числом. Колонка sku текстовая, и нечисловое
        # значение (сбой синхронизации, правка базы руками) раньше давало
        # оператору 500 вместо понятного сообщения.
        package = [{"product_id": int(item["sku"]), "quantity": int(item["quantity"])} for item in items]
    except (TypeError, ValueError):
        return {
            "status": "error",
            "message": f"{posting_number}: в составе некорректный SKU — обновите данные из Ozon",
        }

    client = ozon.get_client(account)
    try:
        result = client.ship(posting_number, [package])
    except OzonError as exc:
        db.log_event(
            "ship_error", level="error", account_id=account["id"], user=user,
            posting_number=posting_number, message=str(exc),
        )
        return {"status": "error", "message": f"{posting_number}: Ozon отклонил сборку — {exc.message}"}

    numbers = result.get("postings") or [posting_number]
    refreshed = []
    for number in numbers:
        try:
            raw = client.posting_get(number)
        except OzonError:
            raw = None
        if raw:
            with db.write() as conn:
                store.upsert_posting(conn, account["id"], raw)
            refreshed.append(number)
    db.log_event(
        "ship",
        account_id=account["id"],
        user=user,
        posting_number=posting_number,
        message="Отправление собрано в Ozon",
        payload={"result": numbers},
    )
    extra = ""
    if len(numbers) > 1 or (numbers and numbers[0] != posting_number):
        extra = f" Ozon разделил заказ: {', '.join(numbers)}."
    return {
        "status": "ok",
        "message": f"{posting_number}: переведено в «Ожидает отгрузки».{extra}",
        "postings": numbers or refreshed,
    }


def label_pdf(account: dict, user: dict, posting_numbers: list[str]) -> tuple[bytes, str]:
    """Стикер(ы) отправления + отметка о печати."""
    pdf, filename = ozon.get_client(account).package_label(posting_numbers)
    now = db.now_iso()
    with db.write() as conn:
        for number in posting_numbers:
            conn.execute(
                "UPDATE postings SET printed_at = ?, print_count = print_count + 1 "
                "WHERE account_id = ? AND posting_number = ?",
                (now, account["id"], number),
            )
            db.log_event(
                "label_print", account_id=account["id"], user=user, posting_number=number,
                message="Стикер отправлен на печать", conn=conn,
            )
    return pdf, filename


# ------------------------------------------------------------------- Ozon
def pending_labels(account_id: int) -> list[str]:
    """Отправления «Ожидает отгрузки», чей стикер ещё не выгружен.

    Только этот статус: в «Ожидает сборки» стикера ещё нет, а всё, что уехало
    дальше, замок не держит — там стикер уже не получить.
    """

    rows = db.query(
        "SELECT posting_number FROM postings WHERE account_id = ? AND status = ? "
        "AND label_saved_at IS NULL ORDER BY posting_number",
        (account_id, store.STATUS_AWAITING_DELIVER),
    )
    return [row["posting_number"] for row in rows]
