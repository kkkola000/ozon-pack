"""Разделы Яндекс Маркета: заказы и рабочее место сборщика.

Устроено как у Ozon: вкладки «Ожидает сборки» / «Ожидает отгрузки» / «Собранные»,
сборка по скану товара, ярлыки выгружаются архивом до начала сборки.

Чего здесь нет — переводов статуса на стороне Маркета. Панель только читает
заказы и берёт ярлыки; «Готов к отгрузке» продавец отмечает в кабинете Маркета,
как и раньше. Отметка «собрано» — наша, площадка о ней не знает.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ...core import access, db, labels, sync
from . import client as yandex, pack as yandex_pack
from ...core.config import settings
from ...core.deps import check_csrf, require_manager, require_market, require_section, safe_filename, templates
from ..base import NavItem, Workspace
from .client import YandexError
from ...core import store as core_store
from . import store

log = logging.getLogger("yandex")

router = APIRouter()

# Сколько ярлыков печатается одним запросом с вкладки заказов.
MAX_LABELS = 50

# «Собранные» — очередь на отгрузку, а не архив: как только заказ уходит из
# рабочих этапов Маркета, синхронизация убирает его из панели. Кто и когда
# собрал, остаётся в журнале.
TABS = {
    "pack": ("Ожидает сборки", "o.substatus = 'STARTED' AND o.local_state != 'packed'"),
    "ship": ("Ожидает отгрузки", "o.substatus = 'READY_TO_SHIP' AND o.local_state != 'packed'"),
    "packed": ("Собранные", "o.local_state = 'packed'"),
}


def _list_orders(account: dict, tab: str, search: str = "", limit: int = 300) -> list[dict]:
    _title, condition = TABS.get(tab, TABS["pack"])
    params: list = [account["id"]]
    sql = f"SELECT o.* FROM yandex_orders o WHERE o.account_id = ? AND {condition}"
    if search:
        like = f"%{search.strip()}%"
        sql += """
            AND (o.id LIKE ? OR o.external_id LIKE ? OR o.service_name LIKE ?
                 OR EXISTS (SELECT 1 FROM yandex_order_items i
                            WHERE i.account_id = o.account_id AND i.order_id = o.id
                            AND (i.name LIKE ? OR i.offer_id LIKE ?)))
        """
        params += [like] * 5
    order = "o.packed_at DESC" if tab == "packed" else "(o.shipment_date IS NULL), o.shipment_date, o.id"
    sql += f" ORDER BY {order} LIMIT ?"
    params.append(limit)
    return [store.yandex_view(row) for row in db.query(sql, params)]


def _counts(account: dict) -> dict:
    return {
        key: db.query_one(
            f"SELECT COUNT(*) AS c FROM yandex_orders o WHERE o.account_id = ? AND {condition}",
            (account["id"],),
        )["c"]
        for key, (_title, condition) in TABS.items()
    }


@router.get("/yandex", response_class=HTMLResponse)
def yandex_page(request: Request, tab: str = "pack", q: str = "", user: dict = Depends(require_section("orders")),
                account: dict = Depends(require_market("yandex"))):
    if tab not in TABS:
        tab = "pack"
    return templates.TemplateResponse(
        request,
        "yandex/orders.html",
        {
            "request": request,
            "user": user,
            "account": account,
            "tab": tab,
            "tabs": TABS,
            "counts": _counts(account),
            "orders": _list_orders(account, tab, q),
            "search": q,
            "sync": sync.status(),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "yandex",
        },
    )


@router.get("/api/yandex/orders")
def api_yandex_orders(tab: str = "pack", q: str = "", user: dict = Depends(require_section("orders")),
                      account: dict = Depends(require_market("yandex"))):
    return {"orders": _list_orders(account, tab, q), "counts": _counts(account)}


def _order_row(account: dict, order_id: str) -> dict:
    row = db.query_one(
        "SELECT * FROM yandex_orders WHERE account_id = ? AND id = ?", (account["id"], order_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Заказ {order_id} не найден в этом кабинете")
    return dict(row)


@router.post("/api/yandex/orders/{order_id}/reset")
def api_yandex_reset_order(order_id: str, request: Request, admin: dict = Depends(require_manager),
                           account: dict = Depends(require_market("yandex"))):
    """Снять отметку «собрано» — например, если сборку закрыли по ошибке.

    Только админу и владельцу: отметка — результат работы сборщика, и снимать
    её должен тот, кто отвечает за склад.
    """
    check_csrf(request)
    _order_row(account, order_id)
    db.execute(
        "UPDATE yandex_orders SET local_state = 'new', packed_at = NULL, packed_by = NULL, "
        "claim_user_id = NULL, claim_login = NULL, claim_at = NULL "
        "WHERE account_id = ? AND id = ?",
        (account["id"], order_id),
    )
    db.log_event(
        "yandex_order_reset", level="warn", account_id=account["id"], user=admin,
        posting_number=order_id, message="Сброшена отметка сборки",
    )
    return {"status": "ok", "message": f"{order_id}: отметка сборки снята"}


@router.post("/api/yandex/sync")
def api_yandex_sync(request: Request, user: dict = Depends(require_section("orders")),
                    account: dict = Depends(require_market("yandex"))):
    check_csrf(request)
    try:
        result = sync.run_once(account=account)
    except Exception as exc:  # noqa: BLE001 - причину показываем оператору
        raise HTTPException(status_code=502, detail=f"Маркет недоступен: {exc}") from exc
    return {
        "status": "ok",
        "message": f"Загружено заказов: {result.get('yandex', 0)}",
        "result": result,
        "counts": _counts(account),
        "sync": sync.status(),
    }


# ------------------------------------------------------------------ ярлыки
def _pdf_response(pdf: bytes, filename: str) -> Response:
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename(filename)}"',
                 "Cache-Control": "no-store"},
    )


@router.get("/api/yandex/label/{order_id}.pdf")
def api_yandex_label(order_id: str, user: dict = Depends(require_section("pack")),
                     account: dict = Depends(require_market("yandex"))):
    """Ярлык одного заказа — файл Маркета как есть."""
    _order_row(account, order_id)
    try:
        pdf, filename = yandex_pack.label_pdf(account, user, [order_id])
    except YandexError as exc:
        raise HTTPException(status_code=502, detail=f"Маркет не отдал ярлык: {exc.message}") from exc
    return _pdf_response(pdf, filename)


@router.post("/api/yandex/labels.pdf")
def api_yandex_labels(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("orders")),
                      account: dict = Depends(require_market("yandex"))):
    """Пачка ярлыков — для печати нескольких заказов сразу."""
    check_csrf(request)
    ids = [str(i) for i in (payload.get("order_ids") or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного заказа")
    if len(ids) > MAX_LABELS:
        raise HTTPException(status_code=400, detail=f"За один раз печатается не больше {MAX_LABELS} ярлыков")
    for order_id in ids:
        _order_row(account, order_id)
    try:
        pdf, filename = yandex_pack.label_pdf(account, user, ids)
    except YandexError as exc:
        raise HTTPException(status_code=502, detail=f"Маркет не отдал ярлыки: {exc.message}") from exc
    return _pdf_response(pdf, filename)


@router.post("/api/yandex/labels/archive.zip")
def api_yandex_labels_archive(request: Request, user: dict = Depends(require_section("pack")),
                              account: dict = Depends(require_market("yandex"))):
    """Ярлыки всех заказов в работе, ждущих выгрузки, — архивом на компьютер.

    Файл панель у себя не оставляет: архив уходит в браузер. В базе только
    отметка о выгрузке — по ней открывается сканирование.
    """
    check_csrf(request)
    ids = yandex_pack.pending_labels(account["id"])
    if not ids:
        raise HTTPException(status_code=400, detail="Все ярлыки уже выгружены")
    ids = ids[: labels.MAX_AT_ONCE]
    client = yandex.get_client(account)
    archive, saved = labels.build_archive(ids, lambda batch: client.labels_pdf(batch)[0], prefix="ярлыки")
    if not saved:
        raise HTTPException(status_code=502, detail="Маркет не отдал ни одного ярлыка")
    labels.mark_saved("yandex_orders", account["id"], saved, "id")
    db.log_event(
        "yandex_labels_archive", account_id=account["id"], user=user,
        message=f"Выгружены ярлыки: {len(saved)} шт.",
    )
    stamp = core_store.local_time(db.now_iso(), "%Y-%m-%d_%H-%M")
    name = safe_filename(f"yandex-labels-{account.get('title') or account['id']}-{stamp}.zip")
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
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


@router.get("/api/yandex/pack/state")
def api_yandex_pack_state(user: dict = Depends(require_section("pack")),
                          account: dict = Depends(require_market("yandex"))):
    return {
        "state": yandex_pack.load_state(account, user),
        "counters": _pack_counters(account),
        "labels": labels.state(yandex_pack.pending_labels(account["id"])),
    }


@router.post("/api/yandex/pack/scan")
def api_yandex_pack_scan(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("pack")),
                         account: dict = Depends(require_market("yandex"))):
    check_csrf(request)
    result = yandex_pack.scan(account, user, str(payload.get("code") or ""))
    result["counters"] = _pack_counters(account)
    return result


@router.post("/api/yandex/pack/release")
def api_yandex_pack_release(request: Request, user: dict = Depends(require_section("pack")),
                            account: dict = Depends(require_market("yandex"))):
    check_csrf(request)
    result = yandex_pack.release(account, user)
    result["counters"] = _pack_counters(account)
    return result


@router.post("/api/yandex/pack/complete")
def api_yandex_pack_complete(request: Request, payload: dict = Body(default={}),
                             user: dict = Depends(require_section("pack")),
                             account: dict = Depends(require_market("yandex"))):
    """Ручное завершение — например, если ярлык не читается сканером."""
    check_csrf(request)
    state = yandex_pack.load_state(account, user)
    if not state["active"]:
        raise HTTPException(status_code=400, detail="Нет активного заказа")
    if settings.require_all_items and not state["complete"] and not access.is_manager(user):
        raise HTTPException(
            status_code=400,
            detail="Сначала отсканируйте все товары. Осталось: " + "; ".join(yandex_pack.missing_items(state)),
        )
    result = yandex_pack.complete(
        account, user, state["active"]["id"], code=payload.get("reason") or "ручное завершение"
    )
    result["counters"] = _pack_counters(account)
    return result


# ------------------------------------------------------------------ для реестра площадок
# Рабочее место сборщика: страница одна на все площадки, слова — свои.
WORKSPACE = Workspace(
    placeholder="Сканируйте штрихкод товара или ярлык заказа…",
    banner="Отсканируйте штрихкод товара — система сама найдёт заказ Маркета и отправит ярлык на печать.",
    gate_title="Скачайте ярлыки",
    download="Скачать ярлыки",
    gate_template="yandex/pack_gate.html",
    url="/yandex/pack",
    tab="yandex_pack",
    load_state=yandex_pack.load_state,
    count_queue=lambda account: _pack_counters(account),
    counters=(
        ("c-packaging", "awaiting_packaging", "Ожидает сборки", ""),
        ("c-deliver", "awaiting_deliver", "Ожидает отгрузки", ""),
        ("c-packed", "packed_today", "Собрано сегодня", "ok"),
    ),
)

def _count(sql: str, params: tuple) -> int:
    row = db.query_one(sql, params)
    return row["c"] if row else 0


def nav_items(account: dict) -> list[NavItem]:
    """Меню кабинета Маркета. Как у Ozon: значок — сколько работы осталось."""
    aid = (account["id"],)
    return [
        NavItem("/yandex/pack", "Сборка", "yandex_pack", "pack"),
        NavItem("/yandex?tab=pack", "Заказы Маркета", "yandex", "orders", (
            (_count("SELECT COUNT(*) AS c FROM yandex_orders WHERE account_id = ? "
                    "AND substatus = 'STARTED' AND local_state != 'packed'", aid),
             "warn", "Ожидает сборки"),
            (_count("SELECT COUNT(*) AS c FROM yandex_orders WHERE account_id = ? "
                    "AND substatus = 'READY_TO_SHIP' AND local_state != 'packed'", aid),
             "accent", "Ожидает отгрузки"),
        )),
    ]


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
