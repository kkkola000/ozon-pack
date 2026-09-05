"""Раздел «Заказы Avito»: подтверждение, отправка и оригинальные этикетки.

Сборщику нужны ровно два действия — «Подтвердите заказ» и «Отправьте заказ».
Остальные возможности Avito (отмена, маркировка «Честный знак», трек-номера,
интервалы курьера, споры) в интерфейс не выводятся: лишняя кнопка на складе —
это лишняя ошибка.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .. import avito, db, store, sync
from ..avito import AvitoError
from ..deps import check_csrf, current_user, require_avito_account, templates

log = logging.getLogger("avito")

router = APIRouter()

# Вкладки соответствуют двум задачам сборщика; всё остальное в панель не попадает.
TABS = {
    "confirm": ("Подтвердите заказ", avito.STATUS_ON_CONFIRMATION),
    "ship": ("Отправьте заказ", avito.STATUS_READY_TO_SHIP),
}


def _list_orders(account: dict, tab: str, search: str = "", limit: int = 300) -> list[dict]:
    status = TABS.get(tab, TABS["confirm"])[1]
    params: list = [account["id"], status]
    sql = "SELECT * FROM avito_orders WHERE account_id = ? AND status = ?"
    if search:
        like = f"%{search.strip()}%"
        sql += """
            AND (id LIKE ? OR marketplace_id LIKE ? OR tracking_number LIKE ? OR buyer_name LIKE ?
                 OR EXISTS (SELECT 1 FROM avito_order_items i
                            WHERE i.account_id = avito_orders.account_id AND i.order_id = avito_orders.id
                            AND (i.title LIKE ? OR i.avito_id LIKE ? OR i.seller_id LIKE ?)))
        """
        params += [like] * 7
    # Сначала то, что горит: срок подтверждения или отправки.
    deadline = "confirm_till" if status == avito.STATUS_ON_CONFIRMATION else "ship_till"
    sql += f" ORDER BY ({deadline} IS NULL), {deadline} LIMIT ?"
    params.append(limit)
    return [store.avito_view(row) for row in db.query(sql, params)]


def _counts(account: dict) -> dict:
    return {
        key: db.query_one(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = ?",
            (account["id"], status),
        )["c"]
        for key, (_title, status) in TABS.items()
    }


@router.get("/avito", response_class=HTMLResponse)
def avito_page(request: Request, tab: str = "confirm", q: str = "", user: dict = Depends(current_user),
               account: dict = Depends(require_avito_account)):
    if tab not in TABS:
        tab = "confirm"
    return templates.TemplateResponse(
        request,
        "avito.html",
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
            "active_tab": "avito",
        },
    )


@router.get("/api/avito/orders")
def api_avito_orders(tab: str = "confirm", q: str = "", user: dict = Depends(current_user),
                     account: dict = Depends(require_avito_account)):
    return {"orders": _list_orders(account, tab, q), "counts": _counts(account)}


def _order_row(account: dict, order_id: str) -> dict:
    row = db.query_one(
        "SELECT * FROM avito_orders WHERE account_id = ? AND id = ?", (account["id"], order_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Заказ {order_id} не найден в этом кабинете")
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


@router.post("/api/avito/confirm")
def api_avito_confirm(request: Request, payload: dict = Body(...), user: dict = Depends(current_user),
                      account: dict = Depends(require_avito_account)):
    """«Подтвердите заказ» — переход confirm в Avito."""
    check_csrf(request)
    return _bulk(account, user, payload, avito.TRANSITION_CONFIRM, "подтверждено")


@router.post("/api/avito/ship")
def api_avito_ship(request: Request, payload: dict = Body(...), user: dict = Depends(current_user),
                   account: dict = Depends(require_avito_account)):
    """«Отправьте заказ» — переход perform. Доступен для доставки курьером продавца."""
    check_csrf(request)
    return _bulk(account, user, payload, avito.TRANSITION_PERFORM, "отправлено")


def _bulk(account: dict, user: dict, payload: dict, transition: str, verb: str) -> dict:
    ids = [str(i) for i in (payload.get("order_ids") or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного заказа")
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


@router.get("/api/avito/label/{order_id}.pdf")
def api_avito_label(order_id: str, user: dict = Depends(current_user),
                    account: dict = Depends(require_avito_account)):
    """Оригинальный PDF-файл этикетки от Avito — без нашего редактирования."""
    order = _order_row(account, order_id)
    return _label_response(account, user, [order])


@router.post("/api/avito/labels.pdf")
def api_avito_labels(request: Request, payload: dict = Body(...), user: dict = Depends(current_user),
                     account: dict = Depends(require_avito_account)):
    """Пачка этикеток: Avito принимает до 50 номеров за раз."""
    check_csrf(request)
    ids = [str(i) for i in (payload.get("order_ids") or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного заказа")
    if len(ids) > 50:
        raise HTTPException(status_code=400, detail="За один раз Avito печатает не больше 50 этикеток")
    return _label_response(account, user, [_order_row(account, order_id) for order_id in ids])


def _label_response(account: dict, user: dict, orders: list[dict]) -> Response:
    # Этикетки Avito запрашиваются по номеру из сервиса сделок (marketplaceId).
    numbers = [order.get("marketplace_id") or order["id"] for order in orders]
    try:
        pdf, filename = avito.get_client(account).label_pdf(numbers)
    except AvitoError as exc:
        raise HTTPException(status_code=502, detail=f"Avito не отдал этикетку: {exc.message}") from exc
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
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.post("/api/avito/sync")
def api_avito_sync(request: Request, user: dict = Depends(current_user),
                   account: dict = Depends(require_avito_account)):
    check_csrf(request)
    try:
        result = sync.sync_avito(account)
    except AvitoError as exc:
        raise HTTPException(status_code=502, detail=f"Avito недоступен: {exc.message}") from exc
    return {
        "status": "ok",
        "message": f"Загружено заказов: {result.get('avito', 0)}",
        "result": result,
        "counts": _counts(account),
    }
