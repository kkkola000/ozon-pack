"""Раздел «Возвраты FBO/FBS»: что готово к выдаче и печать листа."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .. import db, options, store, sync
from ..deps import check_csrf, current_user, templates
from ..ozon import OzonError, get_client

router = APIRouter()


def _filter_returns(
    scheme: str = "all",
    place: str = "",
    q: str = "",
    show: str = "ready",
    limit: int = 1000,
) -> list[dict]:
    conditions = []
    params: list = []
    if show == "ready":
        conditions.append("is_ready = 1 AND taken_at IS NULL")
    elif show == "taken":
        conditions.append("taken_at IS NOT NULL")
    if scheme in ("FBO", "FBS"):
        conditions.append("(type = ? OR scheme = ?)")
        params += [scheme, scheme]
    if place:
        conditions.append("place_name = ?")
        params.append(place)
    if q:
        like = f"%{q.strip()}%"
        conditions.append(
            "(product_name LIKE ? OR offer_id LIKE ? OR sku LIKE ? OR order_number LIKE ?"
            " OR posting_number LIKE ? OR barcode LIKE ? OR id LIKE ?)"
        )
        params += [like] * 7
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    rows = db.query(
        f"SELECT * FROM returns{where} ORDER BY (place_name IS NULL), place_name, product_name LIMIT ?",
        params + [limit],
    )
    return [store.return_view(row) for row in rows]


def _places() -> list[str]:
    rows = db.query(
        "SELECT DISTINCT place_name FROM returns WHERE is_ready = 1 AND place_name IS NOT NULL ORDER BY place_name"
    )
    return [row["place_name"] for row in rows]


@router.get("/returns", response_class=HTMLResponse)
def returns_page(
    request: Request,
    scheme: str = "all",
    place: str = "",
    q: str = "",
    show: str = "ready",
    user: dict = Depends(current_user),
):
    items = _filter_returns(scheme, place, q, show)
    totals = {
        "ready": db.query_one("SELECT COUNT(*) AS c FROM returns WHERE is_ready = 1 AND taken_at IS NULL")["c"],
        "taken": db.query_one("SELECT COUNT(*) AS c FROM returns WHERE taken_at IS NOT NULL")["c"],
        "all": db.query_one("SELECT COUNT(*) AS c FROM returns")["c"],
        "fbo": db.query_one("SELECT COUNT(*) AS c FROM returns WHERE is_ready = 1 AND taken_at IS NULL AND (type = 'FBO' OR scheme = 'FBO')")["c"],
        "fbs": db.query_one("SELECT COUNT(*) AS c FROM returns WHERE is_ready = 1 AND taken_at IS NULL AND (type = 'FBS' OR scheme = 'FBS')")["c"],
    }
    import json as _json

    wanted = options.get_returns_statuses()
    try:
        histogram = _json.loads(db.kv_get("returns_last_statuses") or "{}")
    except ValueError:
        histogram = {}
    hidden = {code: count for code, count in histogram.items() if code not in set(wanted)}

    return templates.TemplateResponse(
        request,
        "returns.html",
        {
            "request": request,
            "user": user,
            "items": items,
            "wanted_labels": [options.status_label(code) for code in wanted],
            "hidden_statuses": [(options.status_label(code), count) for code, count in sorted(hidden.items())],
            "places": _places(),
            "scheme": scheme,
            "place": place,
            "q": q,
            "show": show,
            "totals": totals,
            "sync": sync.status(),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "returns",
        },
    )


@router.get("/returns/print", response_class=HTMLResponse)
def returns_print(
    request: Request,
    scheme: str = "all",
    place: str = "",
    q: str = "",
    show: str = "ready",
    user: dict = Depends(current_user),
):
    """Лист для печати: сборщик идёт с ним получать возвраты."""
    items = _filter_returns(scheme, place, q, show)
    now = datetime.now(timezone.utc)
    db.log_event("returns_print", user=user, message=f"Лист возвратов: {len(items)} поз.")
    if items:
        placeholders = ",".join("?" for _ in items)
        db.execute(
            f"UPDATE returns SET printed_at = ? WHERE id IN ({placeholders})",
            [db.now_iso()] + [item["id"] for item in items],
        )
    return templates.TemplateResponse(
        request,
        "returns_print.html",
        {
            "request": request,
            "user": user,
            "items": items,
            "printed_at": now,
            "scheme": scheme,
            "place": place,
            "show": show,
        },
    )


@router.post("/api/returns/taken")
def api_returns_taken(request: Request, payload: dict = Body(...), user: dict = Depends(current_user)):
    """Отметить возвраты как забранные (локальная отметка, в Ozon не уходит)."""
    check_csrf(request)
    ids = [str(i) for i in (payload.get("ids") or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного возврата")
    taken = bool(payload.get("taken", True))
    placeholders = ",".join("?" for _ in ids)
    with db.write() as conn:
        conn.execute(
            f"UPDATE returns SET taken_at = ?, taken_by = ? WHERE id IN ({placeholders})",
            [db.now_iso() if taken else None, user["login"] if taken else None] + ids,
        )
        db.log_event(
            "returns_taken" if taken else "returns_untaken",
            user=user,
            message=f"{len(ids)} поз.",
            payload={"ids": ids},
            conn=conn,
        )
    return {"status": "ok", "message": ("Отмечено как забрано: " if taken else "Отметка снята: ") + str(len(ids))}


@router.get("/api/returns/giveout.pdf")
def api_giveout(user: dict = Depends(current_user)):
    """Штрихкод Ozon на выдачу возвратов (FBS)."""
    try:
        pdf = get_client().giveout_pdf()
    except OzonError as exc:
        raise HTTPException(status_code=502, detail=f"Ozon не отдал документ выдачи: {exc.message}") from exc
    db.log_event("returns_giveout", user=user, message="Запрошен штрихкод выдачи возвратов")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="giveout.pdf"', "Cache-Control": "no-store"},
    )


@router.post("/api/returns/sync")
def api_returns_sync(request: Request, payload: dict = Body(default={}), user: dict = Depends(current_user)):
    check_csrf(request)
    full = bool(payload.get("full"))
    try:
        result = sync.sync_returns(full=full)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Не удалось обновить возвраты: {exc}") from exc
    return {"status": "ok", "message": f"Обновлено возвратов: {result.get('returns', 0)}", "result": result}
