"""Рабочее место сборщика: сканирование, печать стикеров, завершение сборки."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .. import db, packing, store
from ..config import settings
from ..deps import check_csrf, current_user, require_ozon_account, templates
from ..ozon import OzonError

router = APIRouter()


@router.get("/pack", response_class=HTMLResponse)
def pack_page(request: Request, user: dict = Depends(current_user),
              account: dict = Depends(require_ozon_account)):
    state = packing.load_state(account, user)
    counters = _counters(account)
    return templates.TemplateResponse(
        request,
        "pack.html",
        {
            "request": request,
            "user": user,
            "state": state,
            "counters": counters,
            "account": account,
            "csrf": request.state.session.get("csrf"),
            "active_tab": "pack",
        },
    )


def _counters(account: dict) -> dict:
    account_id = account["id"]
    return {
        "awaiting_packaging": db.query_one(
            "SELECT COUNT(*) AS c FROM postings WHERE account_id = ? AND status = ?",
            (account_id, store.STATUS_AWAITING_PACKAGING),
        )["c"],
        "awaiting_deliver": db.query_one(
            "SELECT COUNT(*) AS c FROM postings WHERE account_id = ? AND status = ? AND local_state = 'new'",
            (account_id, store.STATUS_AWAITING_DELIVER),
        )["c"],
        "packed_today": db.query_one(
            "SELECT COUNT(*) AS c FROM postings WHERE account_id = ? AND local_state = 'packed' "
            "AND packed_at >= date('now')",
            (account_id,),
        )["c"],
        "returns_ready": db.query_one(
            "SELECT COUNT(*) AS c FROM returns WHERE account_id = ? AND is_ready = 1 AND taken_at IS NULL",
            (account_id,),
        )["c"],
    }


@router.get("/api/state")
def api_state(user: dict = Depends(current_user), account: dict = Depends(require_ozon_account)):
    return {"state": packing.load_state(account, user), "counters": _counters(account)}


@router.post("/api/scan")
def api_scan(request: Request, payload: dict = Body(...), user: dict = Depends(current_user),
             account: dict = Depends(require_ozon_account)):
    check_csrf(request)
    result = packing.scan(account, user, str(payload.get("code") or ""))
    result["counters"] = _counters(account)
    return result


@router.post("/api/select")
def api_select(request: Request, payload: dict = Body(...), user: dict = Depends(current_user),
               account: dict = Depends(require_ozon_account)):
    check_csrf(request)
    number = str(payload.get("posting_number") or "")
    result = packing.select_posting(account, user, number, first_sku=payload.get("sku") or None)
    result["counters"] = _counters(account)
    return result


@router.post("/api/release")
def api_release(request: Request, user: dict = Depends(current_user),
                account: dict = Depends(require_ozon_account)):
    check_csrf(request)
    result = packing.release(account, user)
    result["counters"] = _counters(account)
    return result


@router.post("/api/complete")
def api_complete(request: Request, payload: dict = Body(default={}), user: dict = Depends(current_user),
                 account: dict = Depends(require_ozon_account)):
    """Ручное завершение — например, если стикер не читается сканером."""
    check_csrf(request)
    state = packing.load_state(account, user)
    if not state["active"]:
        raise HTTPException(status_code=400, detail="Нет активного отправления")
    if settings.require_all_items and not state["complete"] and user.get("role") != "admin":
        raise HTTPException(status_code=400, detail="Сначала отсканируйте все товары")
    result = packing.complete(
        account, user, state["active"]["posting_number"], code=payload.get("reason") or "ручное завершение"
    )
    result["counters"] = _counters(account)
    return result


@router.get("/api/label/{posting_number}.pdf")
def api_label(posting_number: str, user: dict = Depends(current_user),
              account: dict = Depends(require_ozon_account)):
    try:
        pdf, filename = packing.label_pdf(account, user, [posting_number])
    except OzonError as exc:
        raise HTTPException(status_code=502, detail=f"Ozon не отдал стикер: {exc.message}") from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.post("/api/labels.pdf")
def api_labels(request: Request, payload: dict = Body(...), user: dict = Depends(current_user),
               account: dict = Depends(require_ozon_account)):
    """Пачка стикеров — для печати нескольких отправлений сразу."""
    check_csrf(request)
    numbers = [str(n) for n in (payload.get("posting_numbers") or []) if n]
    if not numbers:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного отправления")
    try:
        pdf, filename = packing.label_pdf(account, user, numbers)
    except OzonError as exc:
        raise HTTPException(status_code=502, detail=f"Ozon не отдал стикеры: {exc.message}") from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-store"},
    )
