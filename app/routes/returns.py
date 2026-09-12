"""Раздел «Возвраты FBO/FBS»: что готово к выдаче и печать листа."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .. import accounts, avito, db, options, store, sync
from ..deps import check_csrf, current_user, require_ozon_account, templates
from .. import ozon
from ..ozon import OzonError

router = APIRouter()


# Значение параметра, которым просят лист сразу по всем кабинетам.
ALL_ACCOUNTS = "all"


def _filter_returns(
    account_ids: list[int],
    scheme: str = "all",
    place: str = "",
    q: str = "",
    limit: int = 1000,
) -> list[dict]:
    """Возвраты Ozon, готовые к выдаче, по одному кабинету или сразу по нескольким.

    В панели только то, что лежит в пункте выдачи: забранное Ozon переводит
    дальше сам, и синхронизация убирает такие записи.
    """
    if not account_ids:
        return []
    placeholders = ",".join("?" for _ in account_ids)
    conditions = [f"r.account_id IN ({placeholders})", "r.is_ready = 1"]
    params: list = list(account_ids)
    if scheme in ("FBO", "FBS"):
        conditions.append("(r.type = ? OR r.scheme = ?)")
        params += [scheme, scheme]
    if place:
        conditions.append("r.place_name = ?")
        params.append(place)
    if q:
        like = f"%{q.strip()}%"
        conditions.append(
            "(r.product_name LIKE ? OR r.offer_id LIKE ? OR r.sku LIKE ? OR r.order_number LIKE ?"
            " OR r.posting_number LIKE ? OR r.barcode LIKE ? OR r.id LIKE ?)"
        )
        params += [like] * 7
    where = " WHERE " + " AND ".join(conditions)
    # Пункт выдачи впереди: за возвратами едут в конкретный ПВЗ, и на листе по
    # нескольким кабинетам строки одного пункта должны идти подряд.
    rows = db.query(
        f"SELECT r.*, a.title AS account_title FROM returns r "
        f"LEFT JOIN accounts a ON a.id = r.account_id{where} "
        f"ORDER BY (r.place_name IS NULL), r.place_name, a.title, r.product_name LIMIT ?",
        params + [limit],
    )
    return [store.return_view(row) for row in rows]


def _avito_returns(account_ids: list[int], limit: int = 500) -> list[dict]:
    """Возвраты Avito, готовые к выдаче. Строка — заказ целиком, с вложенными товарами."""
    if not account_ids:
        return []
    placeholders = ",".join("?" for _ in account_ids)
    rows = db.query(
        f"SELECT o.*, a.title AS account_title FROM avito_orders o "
        f"LEFT JOIN accounts a ON a.id = o.account_id "
        f"WHERE o.account_id IN ({placeholders}) AND o.status = ? "
        f"ORDER BY a.title, (o.updated_at_api IS NULL), o.updated_at_api DESC LIMIT ?",
        list(account_ids) + [avito.STATUS_ON_RETURN, limit],
    )
    return [store.avito_view(row) for row in rows]


def _accounts_by_marketplace() -> tuple[list[int], list[int]]:
    """Включённые кабинеты, разложенные по площадкам."""
    active = accounts.all_accounts(active_only=True)
    return (
        [a["id"] for a in active if a["marketplace"] == "ozon"],
        [a["id"] for a in active if a["marketplace"] == "avito"],
    )


def ready_everywhere() -> int:
    """Сколько возвратов готово к выдаче во всех кабинетах — для подписи кнопки.

    Считаем запросом: выбирать строки целиком ради длины дорого, у Avito к
    каждому заказу ещё и товары подтягиваются отдельным запросом.
    """
    ozon_ids, avito_ids = _accounts_by_marketplace()
    total = 0
    if ozon_ids:
        placeholders = ",".join("?" for _ in ozon_ids)
        total += db.query_one(
            f"SELECT COUNT(*) AS c FROM returns WHERE is_ready = 1 AND account_id IN ({placeholders})",
            ozon_ids,
        )["c"]
    if avito_ids:
        placeholders = ",".join("?" for _ in avito_ids)
        total += db.query_one(
            f"SELECT COUNT(*) AS c FROM avito_orders WHERE status = ? AND account_id IN ({placeholders})",
            [avito.STATUS_ON_RETURN] + avito_ids,
        )["c"]
    return total


def _places(account: dict) -> list[str]:
    rows = db.query(
        "SELECT DISTINCT place_name FROM returns WHERE account_id = ? AND is_ready = 1 "
        "AND place_name IS NOT NULL ORDER BY place_name",
        (account["id"],),
    )
    return [row["place_name"] for row in rows]


@router.get("/returns", response_class=HTMLResponse)
def returns_page(
    request: Request,
    scheme: str = "all",
    place: str = "",
    q: str = "",
    user: dict = Depends(current_user),
    account: dict = Depends(require_ozon_account),
):
    items = _filter_returns([account["id"]], scheme, place, q)
    aid = (account["id"],)
    totals = {
        "ready": db.query_one(
            "SELECT COUNT(*) AS c FROM returns WHERE account_id = ? AND is_ready = 1", aid
        )["c"],
        "fbo": db.query_one(
            "SELECT COUNT(*) AS c FROM returns WHERE account_id = ? AND is_ready = 1 "
            "AND (type = 'FBO' OR scheme = 'FBO')", aid
        )["c"],
        "fbs": db.query_one(
            "SELECT COUNT(*) AS c FROM returns WHERE account_id = ? AND is_ready = 1 "
            "AND (type = 'FBS' OR scheme = 'FBS')", aid
        )["c"],
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
            "places": _places(account),
            "account": account,
            "scheme": scheme,
            "place": place,
            "q": q,
            "totals": totals,
            "sync": sync.status(),
            "all_total": ready_everywhere(),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "returns",
        },
    )


def _mark_printed(table: str, rows: list[dict]) -> None:
    """Отметить строки напечатанными. Кабинеты могут быть разные — ключ составной."""
    if not rows:
        return
    pairs = ",".join("(?,?)" for _ in rows)
    params: list = [db.now_iso()]
    for row in rows:
        params += [row["account_id"], row["id"]]
    db.execute(
        f"UPDATE {table} SET printed_at = ? WHERE (account_id, id) IN ({pairs})",
        params,
    )


@router.get("/returns/print", response_class=HTMLResponse)
def returns_print(
    request: Request,
    scheme: str = "all",
    place: str = "",
    q: str = "",
    scope: str = "",
    user: dict = Depends(current_user),
):
    """Лист для печати: сборщик идёт с ним получать возвраты.

    scope=all — один лист сразу по всем кабинетам, Ozon и Avito. Фильтры
    текущего кабинета к нему не применяются: на таком листе нужно всё, что
    готово к выдаче, иначе сборщик уедет за частью возвратов.
    """
    now = datetime.now(timezone.utc)
    everywhere = scope == ALL_ACCOUNTS

    if everywhere:
        ozon_ids, avito_ids = _accounts_by_marketplace()
        items = _filter_returns(ozon_ids)
        avito_orders = _avito_returns(avito_ids)
        account = None
        scheme, place, q = "all", "", ""
        # Лимит выборки может обрезать лист. Промолчать нельзя: сборщик уедет,
        # решив, что забрал всё, и за остатком никто не вернётся.
        truncated = len(items) + len(avito_orders) < ready_everywhere()
        db.log_event(
            "returns_print", user=user,
            message=f"Лист возвратов по всем кабинетам: {len(items)} поз. Ozon, {len(avito_orders)} заказов Avito",
        )
    else:
        account = require_ozon_account(request)
        items = _filter_returns([account["id"]], scheme, place, q)
        avito_orders = []
        truncated = False
        db.log_event(
            "returns_print", account_id=account["id"], user=user,
            message=f"Лист возвратов: {len(items)} поз.",
        )

    _mark_printed("returns", items)
    _mark_printed("avito_orders", avito_orders)

    return templates.TemplateResponse(
        request,
        "returns_print.html",
        {
            "request": request,
            "user": user,
            "items": items,
            "avito_orders": avito_orders,
            "account": account,
            "everywhere": everywhere,
            "truncated": truncated,
            "printed_at": now,
            "scheme": scheme,
            "place": place,
        },
    )


@router.get("/api/returns/giveout.pdf")
def api_giveout(user: dict = Depends(current_user), account: dict = Depends(require_ozon_account)):
    """Штрихкод Ozon на выдачу возвратов (FBS)."""
    try:
        pdf = ozon.get_client(account).giveout_pdf()
    except OzonError as exc:
        raise HTTPException(status_code=502, detail=f"Ozon не отдал документ выдачи: {exc.message}") from exc
    db.log_event(
        "returns_giveout", account_id=account["id"], user=user, message="Запрошен штрихкод выдачи возвратов"
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="giveout.pdf"', "Cache-Control": "no-store"},
    )


@router.post("/api/returns/sync")
def api_returns_sync(request: Request, payload: dict = Body(default={}), user: dict = Depends(current_user),
                     account: dict = Depends(require_ozon_account)):
    check_csrf(request)
    full = bool(payload.get("full"))
    try:
        result = sync.sync_returns(account, full=full)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Не удалось обновить возвраты: {exc}") from exc
    return {"status": "ok", "message": f"Обновлено возвратов: {result.get('returns', 0)}", "result": result}
