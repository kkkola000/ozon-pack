"""Раздел «Возвраты» — один на все площадки.

Раздел общий: что готово к выдаче, лист для поездки в пункт выдачи, отметки
«принят / не принят», акты за день. Откуда берутся строки и как они выглядят,
знает площадка — она объявляет ReturnsSource, а здесь по именам площадок
ничего не решается.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ..core import accounts, db, return_acts, returns_pdf, store
from ..core import sync as core_sync
from ..core.deps import (check_csrf, current_account, require_account, require_owner, require_section,
                         templates)
from ..markets.base import MarketError

router = APIRouter()

# Значение параметра, которым просят лист сразу по всем кабинетам.
ALL_ACCOUNTS = "all"


def _registry():
    from ..markets import registry

    return registry


def require_returns(request: Request) -> dict:
    """Кабинет площадки, у которой в панели есть возвраты."""
    account = require_account(request)
    market = _registry().get(account["marketplace"])
    if market is None or market.returns is None:
        title = market.title if market else account["marketplace"]
        raise HTTPException(status_code=409, detail=f"У площадки «{title}» раздела возвратов в панели нет")
    return account


def _source(account: dict):
    return _registry().require(account["marketplace"]).returns


def _sections_everywhere(limit: int = 1000) -> list[dict]:
    """Всё, что готово к выдаче, по всем включённым кабинетам — секциями по площадкам."""
    active = accounts.all_accounts(active_only=True)
    sections = []
    for market, source in return_acts.sources():
        ids = [a["id"] for a in active if a["marketplace"] == market.code]
        rows = source.ready(ids, params={}, limit=limit) if ids else []
        if rows:
            sections.append(_section(market, source, rows))
    return sections


def _section(market, source, rows: list[dict]) -> dict:
    return {
        "code": market.code,
        "label": source.label,
        "unit": source.unit,
        "rows": rows,
        "pieces": sum(int(source.quantity(row) or 0) for row in rows),
        "template": source.sheet_template,
        "table": source.table,
        "pdf_table": source.pdf_table,
    }


def ready_everywhere() -> int:
    """Сколько возвратов готово к выдаче во всех кабинетах — для подписи кнопки.

    Считаем запросом: выбирать строки целиком ради длины дорого.
    """
    active = accounts.all_accounts(active_only=True)
    total = 0
    for market, source in return_acts.sources():
        ids = [a["id"] for a in active if a["marketplace"] == market.code]
        if ids:
            total += source.count_ready(ids)
    return total


@router.get("/returns", response_class=HTMLResponse)
def returns_page(
    request: Request,
    tab: str = "ready",
    user: dict = Depends(require_section("returns")),
    account: dict = Depends(require_returns),
):
    source = _source(account)
    params = {key: value for key, value in request.query_params.items() if key != "tab"}
    page = source.page(account, params)
    return templates.TemplateResponse(
        request,
        "returns.html",
        {
            "request": request,
            "user": user,
            "account": account,
            **page,
            "list_template": source.list_template,
            "hint_template": source.hint_template,
            "ready_total": source.count_ready([account["id"]]),
            "query": params,
            "sync": core_sync.status(),
            "all_total": ready_everywhere(),
            "tab": "acts" if tab == "acts" else "ready",
            "acts": return_acts.pending([account["id"]]),
            "today": store.local_day(),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "returns",
        },
    )


@router.post("/api/returns/sync")
def api_returns_sync(request: Request, payload: dict = Body(default={}),
                     user: dict = Depends(require_section("returns")),
                     account: dict = Depends(require_returns)):
    """Обновить возвраты кабинета — как именно, знает площадка."""
    check_csrf(request)
    full = bool(payload.get("full"))
    try:
        result = _source(account).sync(account, full=full)
    except Exception as exc:  # noqa: BLE001 - причину показываем оператору
        raise HTTPException(status_code=502, detail=f"Не удалось обновить возвраты: {exc}") from exc
    return {"status": "ok", "message": result.get("message") or "Возвраты обновлены", "result": result}


@router.get("/api/returns/giveout.pdf")
def api_giveout(user: dict = Depends(require_section("returns")),
                account: dict = Depends(require_returns)):
    """Документ площадки на выдачу возвратов — у кого он есть (Ozon: штрихкод FBS)."""
    source = _source(account)
    if not source.giveout:
        raise HTTPException(status_code=409, detail="У этой площадки документа на выдачу нет")
    try:
        pdf = source.giveout(account)
    except MarketError as exc:
        raise HTTPException(status_code=502, detail=f"Площадка не отдала документ выдачи: {exc.message}") from exc
    db.log_event(
        "returns_giveout", account_id=account["id"], user=user, message="Запрошен штрихкод выдачи возвратов"
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="giveout.pdf"', "Cache-Control": "no-store"},
    )


@router.post("/api/returns/acts/by-day")
def api_act_by_day(request: Request, payload: dict = Body(...),
                   user: dict = Depends(require_section("returns")),
                   account: dict = Depends(require_returns)):
    """Составить акт из возвратов за указанное число. Кто работает с возвратами.

    Акт составляет человек: когда поездка закончилась, знает только он. За
    возвратами ездит сборщик, он же их и отмечает, — значит и акт заводит он,
    не дожидаясь администратора. Панель копит полученные возвраты, а кнопка
    сводит в акт те из них, что ещё ни в один акт не вошли.

    За возвратами ездят несколько раз в день, поэтому актов за одно число
    бывает несколько — каждое нажатие делает новый. Задвоения при этом нет: в
    акт берутся только свободные возвраты, и нажать дважды подряд безопасно.

    Число одно, а не промежуток: акт — это поездка в пункт выдачи, и смешивать
    в нём разные дни значило бы подтверждать одной подписью две работы.
    """
    check_csrf(request)
    source = _source(account)
    if not source.received:
        raise HTTPException(status_code=409, detail="Площадка не ведёт полученные возвраты — акт составить не из чего")
    day = _valid_day(str(payload.get("day") or ""))
    if bool(payload.get("dry_run")):
        ids = source.received(account["id"], day)
        return {
            "status": "ok" if ids else "warning",
            "found": len(ids),
            "day": day,
            "message": (f"В акт попадёт возвратов: {len(ids)}" if ids else
                        "За это число полученных возвратов без акта нет"),
        }
    return return_acts.from_received(source, account["id"], day, user=user)


def _valid_day(day: str) -> str:
    try:
        return date.fromisoformat(day.strip()).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Число указывается как ГГГГ-ММ-ДД") from exc


@router.post("/api/returns/acts/{act_id}/confirm")
def api_confirm_act(act_id: str, request: Request, user: dict = Depends(require_section("returns"))):
    """Подтвердить акт: по всем его возвратам решение принято."""
    check_csrf(request)
    result = return_acts.confirm(act_id, user)
    if result["status"] == "error":
        raise HTTPException(status_code=409 if "отметьте" in result["message"] else 404,
                            detail=result["message"])
    return result


@router.post("/api/returns/acts/{act_id}/unconfirm")
def api_unconfirm_act(act_id: str, request: Request, user: dict = Depends(require_owner)):
    """Вернуть подтверждённый акт в работу. Отметки остаются. Только владельцу.

    Подтверждение — это подпись под принятой работой, и снимать её может
    только тот, кто за склад отвечает. Администратору этого не дают: иначе
    подпись владельца снималась бы без него.
    """
    check_csrf(request)
    result = return_acts.unconfirm(act_id, user)
    if result["status"] == "error":
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.post("/api/returns/acts/{act_id}/delete")
def api_delete_act(act_id: str, request: Request, user: dict = Depends(require_owner)):
    """Удалить акт, освободив возвраты и сняв с них отметки. Только владельцу.

    Это «принять заново с нуля»: акт исчезает, а его возвраты снова попадают
    в «Составить акт» за своё число. Отменить это нельзя — отметки прошлого
    раза останутся только в журнале, — поэтому и владелец.
    """
    check_csrf(request)
    result = return_acts.remove(act_id, user)
    if result["status"] == "error":
        raise HTTPException(status_code=404, detail=result["message"])
    return result


def _act_or_404(act_id: str) -> dict:
    act = return_acts.detail(act_id)
    if not act:
        raise HTTPException(status_code=404, detail="Акт не найден")
    return act


def _act_sections(act: dict) -> list[dict]:
    """Секции акта для листа: те же, что у листа к выдаче, только строки уже с отметками."""
    by_code = {market.code: (market, source) for market, source in return_acts.sources()}
    return [
        _section(*by_code[part["code"]], part["rows"]) for part in act["sections"] if part["code"] in by_code
    ]


@router.get("/returns/acts/{act_id}/print", response_class=HTMLResponse)
def act_print(act_id: str, request: Request, user: dict = Depends(require_section("returns"))):
    """Лист акта — тот же вид, что и лист выдачи, но уже с отметками."""
    act = _act_or_404(act_id)
    return templates.TemplateResponse(
        request,
        "returns_print.html",
        {
            "request": request,
            "user": user,
            "sections": _act_sections(act),
            "account": None,
            "everywhere": act["kind"] == ALL_ACCOUNTS,
            "truncated": False,
            "printed_at": datetime.now(timezone.utc),
            "subtitle": "",
            "act": act,
        },
    )


@router.get("/returns/acts/{act_id}.pdf")
def act_pdf(act_id: str, user: dict = Depends(require_section("returns"))):
    act = _act_or_404(act_id)
    printed_at = datetime.now(timezone.utc)
    try:
        pdf = returns_pdf.build_sheet(
            _act_sections(act), user=user, printed_at=printed_at,
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


def _collect_sheet(request: Request, user: dict, scope: str, *, kind: str) -> dict:
    """Данные листа возвратов — общие для печати из браузера и для файла PDF.

    scope=all — один лист сразу по всем кабинетам и площадкам. Фильтры
    текущего кабинета к нему не применяются: на таком листе нужно всё, что
    готово к выдаче, иначе сборщик уедет за частью возвратов.
    """
    everywhere = scope == ALL_ACCOUNTS
    params = {key: value for key, value in request.query_params.items() if key != "scope"}

    if everywhere:
        sections = _sections_everywhere()
        account = None
        subtitle = ""
        # Лимит выборки может обрезать лист. Промолчать нельзя: сборщик уедет,
        # решив, что забрал всё, и за остатком никто не вернётся.
        truncated = sum(len(s["rows"]) for s in sections) < ready_everywhere()
        db.log_event(
            kind, user=user,
            message="Лист возвратов по всем кабинетам: "
                    + ", ".join(f"{s['label']} {len(s['rows'])} {s['unit']}" for s in sections),
        )
    else:
        account = require_returns(request)
        market = _registry().require(account["marketplace"])
        source = market.returns
        rows = source.ready([account["id"]], params=params)
        sections = [_section(market, source, rows)] if rows else []
        subtitle = source.page(account, params).get("sheet_subtitle", "")
        truncated = False
        db.log_event(
            kind, account_id=account["id"], user=user,
            message=f"Лист возвратов: {len(rows)} {source.unit}",
        )

    for section in sections:
        _mark_printed(section["table"], section["rows"])
    # Акт печать не заводит: его составляет человек за число, когда поездка
    # закончилась, — это факт передачи, а не намерение съездить.

    return {
        "sections": sections,
        "account": account,
        "everywhere": everywhere,
        "truncated": truncated,
        "printed_at": datetime.now(timezone.utc),
        "subtitle": subtitle,
    }


@router.get("/returns/print", response_class=HTMLResponse)
def returns_print(request: Request, scope: str = "", user: dict = Depends(require_section("returns"))):
    """Лист для печати: сборщик идёт с ним получать возвраты."""
    sheet = _collect_sheet(request, user, scope, kind="returns_print")
    return templates.TemplateResponse(
        request, "returns_print.html", {"request": request, "user": user, "act": None, **sheet}
    )


@router.get("/returns/sheet.pdf")
def returns_sheet_pdf(request: Request, scope: str = "", user: dict = Depends(require_section("returns"))):
    """Тот же лист готовым файлом: сохранить, переслать, напечатать где угодно."""
    sheet = _collect_sheet(request, user, scope, kind="returns_pdf")
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
def api_returns_mark(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("returns"))):
    """Отметка сборщика о возврате: принят или нет, плюс комментарий.

    Отметку заводит панель, площадка о ней не знает — поэтому синхронизация
    эти поля не трогает и при обновлении списка комментарий не пропадёт.
    """
    check_csrf(request)
    code = str(payload.get("marketplace") or "").strip()
    account = current_account(request)
    if not code and account:
        code = account["marketplace"]
    market = _registry().get(code)
    if market is None or market.returns is None:
        raise HTTPException(status_code=400, detail="Неизвестная площадка")
    table = market.returns.table

    return_id = str(payload.get("id") or "").strip()
    if not return_id:
        raise HTTPException(status_code=400, detail="Не указан возврат")

    mark = str(payload.get("mark") or "").strip()
    if mark not in store.RETURN_MARKS and mark != "":
        raise HTTPException(status_code=400, detail="Неизвестная отметка")
    note = str(payload.get("note") or "").strip()[:2000]

    # Отмечают возврат текущего кабинета: строка чужого кабинета не находится.
    if not account or account["marketplace"] != code:
        raise HTTPException(
            status_code=409, detail=f"Этот раздел работает только с кабинетами площадки «{market.title}»"
        )
    row = db.query_one(
        f"SELECT id, act_id FROM {table} WHERE account_id = ? AND id = ?", (account["id"], return_id)
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
        # Возврат из акта: отметка меняет и счётчики шапки, и право подтвердить.
        # Отдаём их сразу — иначе кнопка появляется только после перезагрузки.
        "act": return_acts.progress(row["act_id"]) if row["act_id"] else None,
    }
