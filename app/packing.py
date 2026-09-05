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

from . import db, store
from .config import settings
from .ozon import OzonError, get_client

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
    items = []
    done = total = 0
    for item in posting["items"]:
        need = int(item["quantity"])
        got = int(scanned.get(item["sku"], 0))
        total += need
        done += min(got, need)
        items.append({**item, "need": need, "scanned": got, "ok": got >= need})
    return {
        "active": posting,
        "scanned": scanned,
        "items": items,
        "done": done,
        "total": total,
        "complete": total > 0 and done >= total,
        "started_at": row["started_at"],
    }


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

    # Последняя попытка: спросить Ozon по штрихкоду стикера.
    posting = _fetch_by_barcode(account, code)
    if posting:
        return _scan_posting(account, user, posting, code)

    db.log_event(
        "scan_unknown", level="error", account_id=account["id"], user=user, barcode=code,
        message="Код не распознан",
    )
    return ScanResult(
        "error",
        f"Код «{code}» не найден: это не товар из заданий и не стикер отправления",
        action="unknown",
        state=load_state(account, user),
    )


def _fetch_by_barcode(account: dict, code: str) -> dict | None:
    """Штрихкод стикера может быть неизвестен локально — спрашиваем Ozon."""
    try:
        raw = get_client(account).posting_by_barcode(code)
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
        if sku not in required:
            db.log_event(
                "scan_wrong_product",
                level="error",
                user=user,
                posting_number=active["posting_number"],
                sku=sku,
                barcode=code,
                message="Товар не из активного отправления",
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
            db.log_event(
                "scan_extra_product",
                level="warn",
                user=user,
                posting_number=active["posting_number"],
                sku=sku,
                barcode=code,
                message="Повторный скан товара",
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
                user=user,
                posting_number=active["posting_number"],
                sku=sku,
                barcode=code,
                message=f"{scanned[sku]}/{required[sku]}",
                conn=conn,
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

    # Свободное рабочее место: подбираем отправление под товар.
    all_candidates = candidates_for_sku(account, sku, user)
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
        db.log_event(
            "scan_no_candidates", level="warn", account_id=account["id"], user=user,
            sku=sku, barcode=code, message=name,
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

    if len(candidates) == 1:
        return select_posting(account, user, candidates[0]["posting_number"], first_sku=sku, scan_code=code)

    db.log_event(
        "scan_choice", account_id=account["id"], user=user, sku=sku, barcode=code,
        message=f"{len(candidates)} кандидатов",
    )
    return ScanResult(
        "choose",
        f"«{name}» нужен в {len(candidates)} отправлениях — выберите одно (первое самое срочное).",
        action="need_choice",
        sound="warning",
        candidates=candidates,
        sku=sku,
        state=state,
    )


# ------------------------------------------------------------------ выбор отправления
def select_posting(account: dict, user: dict, posting_number: str, *, first_sku: str | None = None,
                   scan_code: str | None = None) -> ScanResult:
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
    scanned: dict[str, int] = {}
    if previous["active"] and previous["active"]["posting_number"] == posting_number:
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

    state = load_state(account, user)
    should_print = settings.autoprint
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
        return select_posting(account, user, posting_number, scan_code=code)

    if active["posting_number"] != posting_number:
        db.log_event(
            "scan_wrong_label",
            level="error",
            account_id=account["id"],
            user=user,
            posting_number=active["posting_number"],
            barcode=code,
            message=f"Отсканирован стикер {posting_number}",
        )
        return ScanResult(
            "error",
            f"СТОП: это стикер отправления {posting_number}, а вы собираете {active['posting_number']}.",
            action="wrong_label",
            state=state,
        )

    if settings.require_all_items and not state["complete"]:
        missing = [f"{i['name']} — {i['need'] - i['scanned']} шт" for i in state["items"] if not i["ok"]]
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
    package = [{"product_id": int(item["sku"]), "quantity": int(item["quantity"])} for item in items]

    client = get_client(account)
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
    pdf, filename = get_client(account).package_label(posting_numbers)
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
