"""Avito: рабочее место, своя часть общих «Заказов» и возвраты.

Сборщику нужны ровно два действия — «Подтвердите заказ» и «Отправьте заказ».
Остальные возможности Avito (отмена, маркировка «Честный знак», трек-номера,
интервалы курьера, споры) в интерфейс не выводятся: лишняя кнопка на складе —
это лишняя ошибка. В возвратах то же правило: показываем только те, что уже
лежат в пункте выдачи и которые можно забрать.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import client as avito, pack as avito_pack
from ...core import db, return_acts
from .client import AvitoError
from ...core.deps import check_csrf, require_manager, require_market, require_section
from ..base import NavItem, OrderAction, OrdersBoard, Workspace
from . import store

log = logging.getLogger("avito")

router = APIRouter()

# Раздел «Заказы» общий на все кабинеты (core/board.py). Старый адрес со
# вкладками ведёт туда же, в тот же статус склада.
OLD_TABS = {"confirm": "packaging", "ship": "deliver", "packed": "packed"}


@router.get("/avito")
def avito_page(tab: str = "confirm"):
    return RedirectResponse(f"/orders?status={OLD_TABS.get(tab, 'packaging')}", status_code=303)


# ------------------------------------------------------------------ сборка заказа
def _pack_counters(account: dict) -> dict:
    aid = (account["id"],)

    def count(sql: str, params=aid) -> int:
        row = db.query_one(sql, params)
        return row["c"] if row else 0

    return {
        "to_pack": count(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = ? "
            "AND local_state != 'packed'", (account["id"], avito.STATUS_READY_TO_SHIP)),
        "packed_today": count(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND local_state = 'packed' "
            "AND packed_at >= date('now')"),
        "confirm": count(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = ?",
            (account["id"], avito.STATUS_ON_CONFIRMATION)),
    }


@router.post("/api/avito/pack/open")
def api_avito_pack_open(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("pack")),
                        account: dict = Depends(require_market("avito"))):
    """Открыть сборку без сканера — если стикер не читается."""
    check_csrf(request)
    order = avito_pack.find_order(account["id"], str(payload.get("order_id") or ""))
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    result = avito_pack.open_order(account, user, order)
    result["counters"] = _pack_counters(account)
    return result


def _order_row(account: dict, order_id: str) -> dict:
    row = db.query_one(
        "SELECT * FROM avito_orders WHERE account_id = ? AND id = ?", (account["id"], order_id)
    )
    if not row:
        raise LookupError(f"Заказ {order_id} не найден в кабинете «{account['title']}»")
    return dict(row)


def _forget(account: dict, order_id: str) -> None:
    """Убрать заказ из панели: он вышел из рабочих статусов."""
    with db.write() as conn:
        conn.execute(
            "DELETE FROM avito_order_items WHERE account_id = ? AND order_id = ?", (account["id"], order_id)
        )
        conn.execute("DELETE FROM avito_orders WHERE account_id = ? AND id = ?", (account["id"], order_id))


def _refresh(account: dict, order_id: str) -> str | None:
    """Перечитать заказ у Avito после действия.

    Иначе в панели останется прежний список availableActions, и у только что
    подтверждённого заказа будет висеть кнопка «Подтвердить заказ».
    Возвращает актуальный статус или None, если заказ ушёл из рабочих.
    """
    try:
        raw = avito.get_client(account).order(order_id)
    except AvitoError as exc:
        log.warning("Заказ %s не перечитан: %s", order_id, exc)
        return None
    if not raw:
        _forget(account, order_id)
        return None
    if raw.get("status") not in avito.WORK_STATUSES:
        _forget(account, order_id)
        return raw.get("status")
    with db.write() as conn:
        store.upsert_avito_order(conn, account["id"], raw)
    return raw.get("status")


def _apply(account: dict, user: dict, order_id: str, transition: str) -> dict:
    """Один переход заказа + обновление локальной копии по ответу Avito."""
    order = _order_row(account, order_id)
    number = order.get("marketplace_id") or order_id
    client = avito.get_client(account)
    try:
        client.apply_transition(order_id, transition)
    except AvitoError as exc:
        db.log_event(
            "avito_error", level="error", account_id=account["id"], user=user,
            posting_number=number, message=f"{transition}: {exc}",
        )
        return {"status": "error", "order_id": order_id, "message": f"Avito отклонил действие: {exc.message}"}

    now = db.now_iso()
    if transition == avito.TRANSITION_CONFIRM:
        db.execute(
            "UPDATE avito_orders SET status = ?, confirmed_at = ?, confirmed_by = ?, updated_at = ? "
            "WHERE account_id = ? AND id = ?",
            (avito.STATUS_READY_TO_SHIP, now, user["login"], now, account["id"], order_id),
        )
        kind, text = "avito_confirm", "Заказ подтверждён"
    else:
        db.execute(
            "UPDATE avito_orders SET status = ?, shipped_at = ?, shipped_by = ?, updated_at = ? "
            "WHERE account_id = ? AND id = ?",
            (avito.STATUS_IN_TRANSIT, now, user["login"], now, account["id"], order_id),
        )
        kind, text = "avito_ship", "Отправка заказа подтверждена"
    # Локальную копию приводим к тому, что теперь говорит площадка.
    _refresh(account, order_id)
    db.log_event(kind, account_id=account["id"], user=user, posting_number=number, message=text)
    return {"status": "ok", "order_id": order_id, "message": f"{number}: {text.lower()}"}


def _transition_many(account: dict, user: dict, ids: list[str], transition: str, verb: str) -> dict:
    for order_id in ids:
        _order_row(account, order_id)
    results = [_apply(account, user, order_id, transition) for order_id in ids]
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    message = f"{verb.capitalize()}: {len(ok)}"
    if failed:
        message += f", с ошибкой: {len(failed)} — {failed[0]['message']}"
    return {
        "status": "ok" if not failed else ("warning" if ok else "error"),
        "message": message,
        "results": results,
        "done": [r["order_id"] for r in ok],
    }


def confirm_many(account: dict, user: dict, ids: list[str]) -> dict:
    """«Подтвердите заказ» — переход confirm в Avito."""
    return _transition_many(account, user, ids, avito.TRANSITION_CONFIRM, "подтверждено")


def ship_many(account: dict, user: dict, ids: list[str]) -> dict:
    """«Отправьте заказ» — переход perform. Доступен для доставки курьером продавца."""
    return _transition_many(account, user, ids, avito.TRANSITION_PERFORM, "отправлено")


def reset_mark(account: dict, user: dict, order_id: str) -> str:
    """Снять отметку «собрано» — например, если сборку закрыли по ошибке.

    Только админу и владельцу — это проверяет ядро: отметка — результат работы
    сборщика, и снимать её должен тот, кто отвечает за склад, а не тот, кто ошибся.
    """
    order = _order_row(account, order_id)
    db.execute(
        "UPDATE avito_orders SET local_state = 'new', packed_at = NULL, packed_by = NULL, "
        "claim_user_id = NULL, claim_login = NULL, claim_at = NULL "
        "WHERE account_id = ? AND id = ?",
        (account["id"], order_id),
    )
    db.log_event(
        "avito_order_reset", level="warn", account_id=account["id"], user=user,
        posting_number=order.get("marketplace_id") or order_id,
        message="Сброшена отметка сборки",
    )
    return f"{order.get('marketplace_id') or order_id}: отметка сборки снята"


def print_labels(account: dict, user: dict, ids: list[str]) -> tuple[bytes, str]:
    """Оригинальные этикетки Avito — без нашего редактирования — и отметка о печати."""
    orders = [_order_row(account, order_id) for order_id in ids]
    # Этикетки Avito запрашиваются по номеру из сервиса сделок (marketplaceId).
    numbers = [order.get("marketplace_id") or order["id"] for order in orders]
    pdf, filename = avito.get_client(account).label_pdf(numbers)
    now = db.now_iso()
    with db.write() as conn:
        for order in orders:
            conn.execute(
                "UPDATE avito_orders SET printed_at = ?, print_count = print_count + 1 "
                "WHERE account_id = ? AND id = ?",
                (now, account["id"], order["id"]),
            )
            db.log_event(
                "avito_label_print", account_id=account["id"], user=user,
                posting_number=order.get("marketplace_id") or order["id"],
                message="Этикетка отправлена на печать", conn=conn,
            )
    return pdf, filename


ORDERS = OrdersBoard(
    table="avito_orders",
    status_sql=store.BOARD_STATUS_SQL,
    deadline_sql=store.BOARD_DEADLINE_SQL,
    search_sql=store.BOARD_SEARCH_SQL,
    card=store.board_card,
    label="Этикетка",
    printed="Этикетка печаталась",
    labels=print_labels,
    reset=reset_mark,
    # Avito отдаёт не больше 50 этикеток за раз.
    max_labels=50,
    actions=(
        OrderAction(key="confirm", title="Подтвердить в Avito", short="Подтвердить", run=confirm_many,
                    ask="Подтвердить {n} заказ(ов) в Avito?"),
        OrderAction(key="ship", title="Отправить в Avito", short="Отправить", run=ship_many,
                    ask="Отметить {n} заказ(ов) Avito как отправленные?"),
    ),
)


# ================================================================== возвраты
# «Возврат: заберите заказ» — заказ вернулся и лежит в пункте выдачи. Только
# такие возвраты панель и хранит: пока посылка едет обратно, забирать нечего.
# Забирать вручную ничего не отмечают: как только возврат получен, Avito
# переводит заказ дальше, и ближайшая синхронизация убирает его из панели.
@router.get("/api/avito/returns/{order_id}/raw")
def api_avito_return_raw(order_id: str, request: Request, admin: dict = Depends(require_manager),
                         account: dict = Depends(require_market("avito"))):
    """Ответ Avito по возврату как есть — чтобы видеть, что площадка реально прислала.

    Нужен, когда чего-то не хватает на экране: например, Avito не отдал адрес ПВЗ.
    """
    try:
        row = _order_row(account, order_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        raw = json.loads(row.get("raw") or "{}")
    except ValueError:
        raw = {}
    return {
        "order_id": order_id,
        "pickup_address": avito.pickup_address(raw),
        "pickup_code": avito.pickup_code(raw),
        "raw": raw,
    }


# ------------------------------------------------------------------ для реестра площадок
# Рабочее место сборщика: страница одна на все площадки, слова — свои.
WORKSPACE = Workspace(
    placeholder="Сканируйте стикер отправления, затем штрихкоды товаров…",
    banner="Отсканируйте стикер отправления — откроется сборка заказа.",
    url="/avito/pack",
    tab="avito_pack",
    load_state=avito_pack.load_state,
    count_queue=lambda account: _pack_counters(account),
    counters=(
        ("c-to-pack", "to_pack", "К сборке", ""),
        ("c-packed", "packed_today", "Собрано сегодня", "ok"),
        ("c-confirm", "confirm", "Ждут подтверждения", ""),
    ),
    owner=avito_pack.owner,
    label=avito_pack.label_one,
    scan=avito_pack.scan,
    release=avito_pack.release,
    # «Завершить без скана» у Avito нет: заказ закрывает последняя единица товара.
    complete=None,
)

def _count(sql: str, params: tuple) -> int:
    row = db.query_one(sql, params)
    return row["c"] if row else 0


def nav_items(account: dict) -> list[NavItem]:
    """Меню кабинета Avito. Собранные не в счёт, как и у Ozon: значок — сколько дел осталось."""
    aid = (account["id"],)
    return [
        NavItem("/avito/pack", "Сборка", "avito_pack", "pack"),
        # «Заказов» здесь нет: пункт общий, со счётчиками по всем кабинетам, —
        # его ставит ядро (core/deps.market_nav).
        # Раздел возвратов один на все площадки — адрес общий, счётчики свои.
        # В таблице лежат только возвраты, готовые к выдаче, — фильтровать ещё
        # и по return_status незачем: написание значения у Avito плавает.
        # Полученные (пропавшие из выдачи) в счётчик не идут: забирать нечего,
        # они ждут акта.
        NavItem("/returns", "Возвраты", "returns", "returns", (
            (_count("SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? "
                    "AND status = 'on_return' AND received_at IS NULL", aid),
             "", "Заберите заказ"),
            (return_acts.pending_count([account["id"]]), "warn", "Акты ждут подтверждения"),
        )),
    ]


def settings_stats(account_id: int) -> dict[str, int]:
    aid = (account_id,)
    return {
        "Ждут подтверждения": _count(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = 'on_confirmation'", aid),
        "Ждут отправки": _count(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = 'ready_to_ship'", aid),
        "Позиций в заказах": _count("SELECT COUNT(*) AS c FROM avito_order_items WHERE account_id = ?", aid),
    }
