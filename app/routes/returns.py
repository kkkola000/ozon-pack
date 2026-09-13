"""Раздел «Возвраты FBO/FBS»: что готово к выдаче и печать листа."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .. import accounts, avito, db, giveouts, options, return_acts, returns_pdf, store, sync
from ..deps import (check_csrf, current_user, require_admin, require_avito_account,
                    require_ozon_account, templates)
from .. import ozon
from ..ozon import OzonError

router = APIRouter()


# Значение параметра, которым просят лист сразу по всем кабинетам.
ALL_ACCOUNTS = "all"

# Куда пишется отметка сборщика: раздел возвратов один, а таблицы у площадок разные.
MARK_TABLES = {"ozon": "returns", "avito": "avito_orders"}


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
    tab: str = "ready",
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
            "tab": "acts" if tab == "acts" else "ready",
            "acts": return_acts.pending([account["id"]]),
            # Акт выдачи ведёт и сам Ozon — показываем рядом, чтобы было с чем
            # сверить полученное. У Avito такого документа нет.
            "giveouts": giveouts.recent(account["id"]),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "returns",
        },
    )


@router.get("/api/returns/giveouts/{giveout_id}/raw")
def api_giveout_raw(giveout_id: str, admin: dict = Depends(require_admin),
                    account: dict = Depends(require_ozon_account)):
    """Ответ Ozon по акту выдачи как есть.

    Поля этого метода у Ozon менялись, и разобрать их вслепую нельзя: здесь
    видно, что площадка реально прислала для конкретного кабинета.
    """
    raw = giveouts.raw_of(account["id"], giveout_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="Акт выдачи не найден")
    return {"giveout_id": giveout_id, "raw": raw}


@router.post("/api/returns/acts/{act_id}/confirm")
def api_confirm_act(act_id: str, request: Request, user: dict = Depends(current_user)):
    """Подтвердить акт: по всем его возвратам решение принято."""
    check_csrf(request)
    result = return_acts.confirm(act_id, user)
    if result["status"] == "error":
        raise HTTPException(status_code=409 if "отметьте" in result["message"] else 404,
                            detail=result["message"])
    return result


def _act_or_404(act_id: str) -> dict:
    act = return_acts.detail(act_id)
    if not act:
        raise HTTPException(status_code=404, detail="Акт не найден")
    return act


@router.get("/returns/acts/{act_id}/print", response_class=HTMLResponse)
def act_print(act_id: str, request: Request, user: dict = Depends(current_user)):
    """Лист акта — тот же вид, что и лист выдачи, но уже с отметками."""
    act = _act_or_404(act_id)
    return templates.TemplateResponse(
        request,
        "returns_print.html",
        {
            "request": request,
            "user": user,
            "items": act["ozon"],
            "avito_orders": act["avito"],
            "account": None,
            "everywhere": act["kind"] == ALL_ACCOUNTS,
            "truncated": False,
            "printed_at": datetime.now(timezone.utc),
            "scheme": "all",
            "place": "",
            "act": act,
        },
    )


@router.get("/returns/acts/{act_id}.pdf")
def act_pdf(act_id: str, user: dict = Depends(current_user)):
    act = _act_or_404(act_id)
    printed_at = datetime.now(timezone.utc)
    try:
        pdf = returns_pdf.build_sheet(
            act["ozon"], act["avito"], user=user, printed_at=printed_at,
            everywhere=act["kind"] == ALL_ACCOUNTS, account=None, act=act,
        )
    except returns_pdf.PdfUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    stamp = (act.get("created_at") or "")[:10] or printed_at.strftime("%Y-%m-%d")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="akt-vozvratov-{stamp}-{act_id[:8]}.pdf"',
            "Cache-Control": "no-store",
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


def _collect_sheet(request: Request, user: dict, scheme: str, place: str, q: str, scope: str,
                   *, kind: str) -> dict:
    """Данные листа возвратов — общие для печати из браузера и для файла PDF.

    scope=all — один лист сразу по всем кабинетам, Ozon и Avito. Фильтры
    текущего кабинета к нему не применяются: на таком листе нужно всё, что
    готово к выдаче, иначе сборщик уедет за частью возвратов.
    """
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
            kind, user=user,
            message=f"Лист возвратов по всем кабинетам: {len(items)} поз. Ozon, {len(avito_orders)} заказов Avito",
        )
    else:
        account = require_ozon_account(request)
        items = _filter_returns([account["id"]], scheme, place, q)
        avito_orders = []
        truncated = False
        db.log_event(
            kind, account_id=account["id"], user=user,
            message=f"Лист возвратов: {len(items)} поз.",
        )

    _mark_printed("returns", items)
    _mark_printed("avito_orders", avito_orders)
    # Печать листа заводит акт: с этим листом поедут в пункт выдачи, и по нему
    # же потом будут ставить отметки. Строкам, у которых акт уже есть, новый не
    # достаётся — возврат остаётся в той поездке, для которой его напечатали.
    return_acts.open_sheet_act(
        user, items, avito_orders,
        kind=ALL_ACCOUNTS if everywhere else "account",
        account_id=None if everywhere else account["id"],
    )

    return {
        "items": items,
        "avito_orders": avito_orders,
        "account": account,
        "everywhere": everywhere,
        "truncated": truncated,
        "printed_at": datetime.now(timezone.utc),
        "scheme": scheme,
        "place": place,
    }


@router.get("/returns/print", response_class=HTMLResponse)
def returns_print(
    request: Request,
    scheme: str = "all",
    place: str = "",
    q: str = "",
    scope: str = "",
    user: dict = Depends(current_user),
):
    """Лист для печати: сборщик идёт с ним получать возвраты."""
    sheet = _collect_sheet(request, user, scheme, place, q, scope, kind="returns_print")
    return templates.TemplateResponse(
        request, "returns_print.html", {"request": request, "user": user, **sheet}
    )


@router.get("/returns/sheet.pdf")
def returns_sheet_pdf(
    request: Request,
    scheme: str = "all",
    place: str = "",
    q: str = "",
    scope: str = "",
    user: dict = Depends(current_user),
):
    """Тот же лист готовым файлом: сохранить, переслать, напечатать где угодно."""
    sheet = _collect_sheet(request, user, scheme, place, q, scope, kind="returns_pdf")
    try:
        pdf = returns_pdf.build_sheet(user=user, **sheet)
    except returns_pdf.PdfUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    name = returns_pdf.filename(
        sheet["printed_at"], everywhere=sheet["everywhere"], account=sheet["account"]
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/api/returns/mark")
def api_returns_mark(request: Request, payload: dict = Body(...), user: dict = Depends(current_user)):
    """Отметка сборщика о возврате: принят или нет, плюс комментарий.

    Отметку заводит панель, площадка о ней не знает — поэтому синхронизация
    эти поля не трогает и при обновлении списка комментарий не пропадёт.
    """
    check_csrf(request)
    table = MARK_TABLES.get(str(payload.get("marketplace") or "ozon"))
    if not table:
        raise HTTPException(status_code=400, detail="Неизвестная площадка")

    return_id = str(payload.get("id") or "").strip()
    if not return_id:
        raise HTTPException(status_code=400, detail="Не указан возврат")

    mark = str(payload.get("mark") or "").strip()
    if mark not in store.RETURN_MARKS and mark != "":
        raise HTTPException(status_code=400, detail="Неизвестная отметка")
    note = str(payload.get("note") or "").strip()[:2000]

    account = require_ozon_account(request) if table == "returns" else require_avito_account(request)
    row = db.query_one(
        f"SELECT id FROM {table} WHERE account_id = ? AND id = ?", (account["id"], return_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Возврат {return_id} не найден в этом кабинете")

    # Пустая отметка без комментария — это «снять»: следов в строке остаться
    # не должно, иначе в списке будет висеть имя и время неизвестно чего.
    keeps = bool(mark or note)
    now = db.now_iso() if keeps else None
    db.execute(
        f"UPDATE {table} SET mark = ?, note = ?, mark_at = ?, mark_by = ? WHERE account_id = ? AND id = ?",
        (mark or None, note or None, now, user["login"] if keeps else None, account["id"], return_id),
    )
    db.log_event(
        "return_mark", account_id=account["id"], user=user,
        message=f"{return_id}: {store.mark_label(mark) or 'отметка снята'}"
                + (f" — {note}" if note else ""),
    )
    return {
        "status": "ok",
        "id": return_id,
        "mark": mark,
        "mark_label": store.mark_label(mark),
        "mark_sign": store.RETURN_MARK_SIGNS.get(mark, ""),
        "note": note,
        "mark_by": user["login"] if keeps else "",
        "mark_at_local": store.local_time(now) if keeps else "",
        "message": f"Отметка сохранена: {store.mark_label(mark) or 'снята'}",
    }


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
