"""Яндекс Маркет: рабочее место сборщика и своя часть общих «Заказов».

Устроено как у Ozon: в «Заказах» статусы «Ожидает сборки» / «Ожидает отгрузки» /
«Собран», сборка по скану товара, ярлыки выгружаются архивом до начала сборки.

Чего здесь нет — переводов статуса на стороне Маркета. Панель только читает
заказы и берёт ярлыки; «Готов к отгрузке» продавец отмечает в кабинете Маркета,
как и раньше. Отметка «собрано» — наша, площадка о ней не знает.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ...core import db
from . import pack as yandex_pack
from ..base import OrdersBoard, Workspace
from . import store

log = logging.getLogger("yandex")

router = APIRouter()

# Сколько ярлыков печатается одним запросом с вкладки заказов.
MAX_LABELS = 50

# Раздел «Заказы» общий на все кабинеты (core/board.py). Старый адрес со
# вкладками ведёт туда же, в тот же статус склада.
OLD_TABS = {"pack": "packaging", "ship": "deliver", "packed": "packed"}


@router.get("/yandex")
def yandex_page(tab: str = "pack"):
    return RedirectResponse(f"/orders?status={OLD_TABS.get(tab, 'packaging')}", status_code=303)


# «Сборка» одна на все кабинеты — /pack. Старый адрес с закладок ведёт туда же.
@router.get("/yandex/pack")
def old_pack_address(request: Request):
    query = request.url.query
    return RedirectResponse("/pack" + (f"?{query}" if query else ""), status_code=303)


def _order_row(account: dict, order_id: str) -> dict:
    row = db.query_one(
        "SELECT * FROM yandex_orders WHERE account_id = ? AND id = ?", (account["id"], order_id)
    )
    if not row:
        raise LookupError(f"Заказ {order_id} не найден в кабинете «{account['title']}»")
    return dict(row)


def reset_mark(account: dict, user: dict, order_id: str) -> str:
    """Снять отметку «собрано» — например, если сборку закрыли по ошибке.

    Только админу и владельцу — это проверяет ядро: отметка — результат работы
    сборщика, и снимать её должен тот, кто отвечает за склад.
    """
    _order_row(account, order_id)
    db.execute(
        "UPDATE yandex_orders SET local_state = 'new', packed_at = NULL, packed_by = NULL, "
        "claim_user_id = NULL, claim_login = NULL, claim_at = NULL "
        "WHERE account_id = ? AND id = ?",
        (account["id"], order_id),
    )
    db.log_event(
        "yandex_order_reset", level="warn", account_id=account["id"], user=user,
        posting_number=order_id, message="Сброшена отметка сборки",
    )
    return f"{order_id}: отметка сборки снята"


def print_labels(account: dict, user: dict, ids: list[str]) -> tuple[bytes, str]:
    """Ярлыки заказов — файл Маркета как есть."""
    for order_id in ids:
        _order_row(account, order_id)
    return yandex_pack.label_pdf(account, user, ids)


# Переводов статуса на стороне Маркета панель не делает — действий нет.
ORDERS = OrdersBoard(
    table="yandex_orders",
    status_sql=store.BOARD_STATUS_SQL,
    deadline_sql="o.shipment_date",
    search_sql=store.BOARD_SEARCH_SQL,
    card=store.board_card,
    label="Ярлык",
    printed="Ярлык печатался",
    labels=print_labels,
    reset=reset_mark,
    max_labels=MAX_LABELS,
)


# ------------------------------------------------------------------ сборка
def _pack_counters(account: dict) -> dict:
    aid = account["id"]

    def count(sql: str, params=(aid,)) -> int:
        row = db.query_one(sql, params)
        return row["c"] if row else 0

    return {
        "awaiting_packaging": count(
            "SELECT COUNT(*) AS c FROM yandex_orders WHERE account_id = ? "
            "AND substatus = 'STARTED' AND local_state != 'packed'"),
        "awaiting_deliver": count(
            "SELECT COUNT(*) AS c FROM yandex_orders WHERE account_id = ? "
            "AND substatus = 'READY_TO_SHIP' AND local_state != 'packed'"),
        "packed_today": count(
            "SELECT COUNT(*) AS c FROM yandex_orders WHERE account_id = ? AND local_state = 'packed' "
            "AND packed_at >= date('now')"),
    }


# ------------------------------------------------------------------ для реестра площадок
# Рабочее место сборщика: страница одна на все площадки, слова — свои.
WORKSPACE = Workspace(
    placeholder="Сканируйте штрихкод товара или ярлык заказа…",
    banner="Отсканируйте штрихкод товара — система сама найдёт заказ Маркета и отправит ярлык на печать.",
    load_state=yandex_pack.load_state,
    count_queue=lambda account: _pack_counters(account),
    counters=(
        ("c-packaging", "awaiting_packaging", "Ожидает сборки", ""),
        ("c-deliver", "awaiting_deliver", "Ожидает отгрузки", ""),
        ("c-packed", "packed_today", "Собрано сегодня", "ok"),
    ),
    owner=yandex_pack.owner,
    label=lambda account, user, order_id: yandex_pack.label_pdf(account, user, [order_id]),
    scan=yandex_pack.scan,
    release=yandex_pack.release,
    complete=yandex_pack.complete_active,
)

def _count(sql: str, params: tuple) -> int:
    row = db.query_one(sql, params)
    return row["c"] if row else 0


def settings_stats(account_id: int) -> dict[str, int]:
    aid = (account_id,)
    return {
        "Ждут сборки": _count(
            "SELECT COUNT(*) AS c FROM yandex_orders WHERE account_id = ? AND substatus = 'STARTED'", aid),
        "Ждут отгрузки": _count(
            "SELECT COUNT(*) AS c FROM yandex_orders WHERE account_id = ? AND substatus = 'READY_TO_SHIP'", aid),
        "Собрано": _count(
            "SELECT COUNT(*) AS c FROM yandex_orders WHERE account_id = ? AND local_state = 'packed'", aid),
        "Позиций в заказах": _count("SELECT COUNT(*) AS c FROM yandex_order_items WHERE account_id = ?", aid),
    }
