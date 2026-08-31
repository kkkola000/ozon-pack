"""Раздел «Заказы FBS»: списки, сборка на стороне Ozon, печать стикеров."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import db, packing, store, sync
from ..deps import check_csrf, current_user, templates

router = APIRouter()

# «Собранные» — это очередь на отгрузку, а не архив: как только Ozon переводит
# отправление дальше (отгружено, доставляется, доставлено), оно уходит из списка.
# История сборки при этом остаётся в журнале и в самой записи отправления.
TABS = {
    "packaging": ("Ожидает сборки", "p.status = 'awaiting_packaging'"),
    "deliver": ("Ожидает отгрузки", "p.status = 'awaiting_deliver' AND p.local_state = 'new'"),
    "packed": ("Собранные", "p.local_state = 'packed' AND p.status = 'awaiting_deliver'"),
}


def _list_postings(tab: str, search: str = "", limit: int = 300) -> list[dict]:
    _title, condition = TABS.get(tab, TABS["packaging"])
    params: list = []
    sql = f"SELECT p.* FROM postings p WHERE {condition}"
    if search:
        like = f"%{search.strip()}%"
        sql += """
            AND (p.posting_number LIKE ? OR p.order_number LIKE ? OR p.city LIKE ?
                 OR EXISTS (SELECT 1 FROM posting_items i WHERE i.posting_number = p.posting_number
                            AND (i.name LIKE ? OR i.offer_id LIKE ? OR i.sku LIKE ?)))
        """
        params += [like] * 6
    order = "p.packed_at DESC" if tab == "packed" else "(p.shipment_date IS NULL), p.shipment_date"
    sql += f" ORDER BY {order} LIMIT ?"
    params.append(limit)
    return [store.posting_view(row) for row in db.query(sql, params)]


@router.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, tab: str = "packaging", q: str = "", user: dict = Depends(current_user)):
    if tab not in TABS:
        tab = "packaging"
    postings = _list_postings(tab, q)
    counts = {
        key: db.query_one(f"SELECT COUNT(*) AS c FROM postings p WHERE {condition}")["c"]
        for key, (_title, condition) in TABS.items()
    }
    return templates.TemplateResponse(
        request,
        "orders.html",
        {
            "request": request,
            "user": user,
            "tab": tab,
            "tabs": TABS,
            "counts": counts,
            "postings": postings,
            "search": q,
            "sync": sync.status(),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "orders",
        },
    )


@router.get("/api/orders")
def api_orders(tab: str = "packaging", q: str = "", user: dict = Depends(current_user)):
    return {"postings": _list_postings(tab, q)}


@router.post("/api/ship")
def api_ship(request: Request, payload: dict = Body(...), user: dict = Depends(current_user)):
    """Перевести отправления в «Ожидает отгрузки»."""
    check_csrf(request)
    numbers = [str(n) for n in (payload.get("posting_numbers") or []) if n]
    if not numbers:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного отправления")
    results = [packing.ship_posting(user, number) for number in numbers]
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    shipped: list[str] = []
    for item in ok:
        shipped.extend(item.get("postings") or [])
    return {
        "status": "ok" if not failed else ("warning" if ok else "error"),
        "message": f"Собрано отправлений: {len(ok)}" + (f", с ошибкой: {len(failed)}" if failed else ""),
        "results": results,
        "shipped": shipped,
    }


@router.post("/api/sync")
def api_sync(request: Request, user: dict = Depends(current_user)):
    check_csrf(request)
    try:
        result = sync.run_once(returns=True)
    except Exception as exc:  # noqa: BLE001 - показываем причину оператору
        raise HTTPException(status_code=502, detail=f"Синхронизация не удалась: {exc}") from exc
    return {"status": "ok", "message": "Данные обновлены", "result": result, "sync": sync.status()}
