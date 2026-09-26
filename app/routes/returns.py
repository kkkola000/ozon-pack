"""Раздел «Возвраты» — один на все площадки.

Раздел общий: что готово к выдаче, лист для поездки в пункт выдачи, отметки
«принят / не принят», акты за день. Откуда берутся строки и как они выглядят,
знает площадка — она объявляет ReturnsSource, а здесь по именам площадок
ничего не решается.

Кабинета в шапке нет: сверху фильтр кабинетов (core/shops.py). Выбран
кабинет — его список со своими фильтрами площадки. «Все кабинеты» — блок на
каждый кабинет, а лист печати и акты — сразу по всем.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from markupsafe import Markup

from ..core import accounts, db, return_acts, returns_pdf, shops, store
from ..core.deps import check_csrf, require_owner, require_section, templates
from ..markets.base import MarketError

router = APIRouter()

# Значение параметра, которым просят лист сразу по всем кабинетам.
ALL_ACCOUNTS = "all"


def _registry():
    from ..markets import registry

    return registry


def returns_shops() -> list[dict]:
    """Кабинеты, чья площадка отдаёт возвраты в панель, — в порядке «Настроек»."""
    return shops.live(lambda market: market.returns is not None)


def _where(value) -> tuple[str, list[dict], list[dict]]:
    """(выбор фильтра, кабинеты под ним, все кабинеты с возвратами)."""
    everyone = returns_shops()
    picked = shops.picked_of(value, everyone)
    return picked, shops.narrow(everyone, picked), everyone


def _one(value) -> dict:
    """Один кабинет с возвратами — для действий, которым нужен ровно один."""
    everyone = returns_shops()
    found = next((account for account in everyone if str(account["id"]) == str(value or "")), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Кабинет с возвратами не найден или выключен")
    return found


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


def _link(shop: str, tab: str = "ready") -> str:
    return f"/returns?shop={shop}" + ("&tab=acts" if tab == "acts" else "")


def _block(account: dict, params: dict, *, filters: bool) -> dict:
    """Список «К выдаче» одного кабинета — кусок площадки, отрисованный её шаблоном."""
    source = _source(account)
    page = source.page(account, params)
    html = templates.get_template(source.list_template).render(
        {**page, "picked": str(account["id"]), "filters": filters}
    )
    market = _registry().get(account["marketplace"])
    return {"account": account, "market": market, "page": page, "html": Markup(html),
            "ready": source.count_ready([account["id"]])}


@router.get("/returns", response_class=HTMLResponse)
def returns_page(
    request: Request,
    tab: str = "ready",
    shop: str = shops.ALL,
    user: dict = Depends(require_section("returns")),
):
    """Возвраты под фильтром кабинетов.

    Выбран кабинет — его список с фильтрами площадки (схема, пункт выдачи,
    поиск). «Все кабинеты» — по блоку на кабинет, без фильтров: это обзор
    перед поездкой, а лист печати тогда сразу общий.
    """
    picked, where, everyone = _where(shop)
    tab = "acts" if tab == "acts" else "ready"
    params = {key: value for key, value in request.query_params.items() if key not in ("tab", "shop")}
    counts = {account["id"]: _source(account).count_ready([account["id"]]) for account in everyone}
    blocks = []
    if tab == "ready":
        single = picked != shops.ALL
        blocks = [_block(account, params if single else {}, filters=single) for account in where]
    total = sum(counts.get(account["id"], 0) for account in where)
    if picked != shops.ALL and blocks:
        stats = blocks[0]["page"]["stats"]
        items = len(blocks[0]["page"]["items"])
    else:
        stats = [("Готовы к выдаче", total)]
        items = total
    # Лист печати: у одного кабинета — с его фильтрами, у всех — общий.
    query = {"shop": picked, **params} if picked != shops.ALL else {"scope": ALL_ACCOUNTS}
    return templates.TemplateResponse(
        request,
        "returns.html",
        {
            "request": request,
            "user": user,
            "shop_chips": shops.chips(everyone, picked, counts, lambda value: _link(value, tab)),
            "picked": picked,
            "where": where,
            "blocks": blocks,
            "stats": stats,
            "items_count": items,
            "ready_total": total,
            "query": query,
            "synced_at": _synced(where),
            "tab": tab,
            "acts": return_acts.pending([account["id"] for account in where]) if where else [],
            "today": store.local_day(),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "returns",
        },
    )


def _synced(where: list[dict]) -> str | None:
    """«Обновлено» — самый старый из заходов на площадку по кабинетам под фильтром."""
    from ..core import sync as core_sync

    stamps = [core_sync.synced_at(account["id"]) for account in where]
    if not stamps or any(stamp is None for stamp in stamps):
        return None
    return store.local_time(min(stamps))


@router.post("/api/returns/sync")
def api_returns_sync(request: Request, payload: dict = Body(default={}),
                     user: dict = Depends(require_section("returns"))):  # noqa: ARG001 - доступ
    """Обновить возвраты кабинетов под фильтром — как именно, знает площадка."""
    check_csrf(request)
    _picked, where, _everyone = _where(payload.get("shop") or request.query_params.get("shop"))
    if not where:
        raise HTTPException(status_code=400, detail="Нет ни одного кабинета с возвратами")
    full = bool(payload.get("full"))
    messages, failed = [], []
    for account in where:
        try:
            result = _source(account).sync(account, full=full)
        except Exception as exc:  # noqa: BLE001 - причину показываем оператору
            failed.append(f"{account['title']}: {exc}")
            continue
        text = result.get("message") or "возвраты обновлены"
        messages.append(text if len(where) == 1 else f"{account['title']}: {text}")
    if not messages:
        raise HTTPException(status_code=502, detail="Не удалось обновить возвраты: " + "; ".join(failed))
    message = "; ".join(messages + [f"не ответил {name}" for name in failed])
    return {"status": "warning" if failed else "ok", "message": message}


@router.get("/api/returns/giveout.pdf")
def api_giveout(shop: str = "", user: dict = Depends(require_section("returns"))):
    """Документ площадки на выдачу возвратов — у кого он есть (Ozon: штрихкод FBS)."""
    account = _one(shop)
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
                   user: dict = Depends(require_section("returns"))):
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

    Акт — всегда по одному кабинету: у каждого магазина своя подпись. Под
    фильтром «Все кабинеты» кнопка составляет по акту на каждый кабинет, где
    за это число есть полученные возвраты: из поездки везут всё сразу.
    """
    check_csrf(request)
    _picked, where, _everyone = _where(payload.get("shop"))
    where = [account for account in where if _source(account).received]
    if not where:
        raise HTTPException(status_code=409, detail="Площадка не ведёт полученные возвраты — акт составить не из чего")
    day = _valid_day(str(payload.get("day") or ""))
    found = {account["id"]: _source(account).received(account["id"], day) for account in where}
    if bool(payload.get("dry_run")):
        total = sum(len(ids) for ids in found.values())
        return {
            "status": "ok" if total else "warning",
            "found": total,
            "day": day,
            "message": (f"В акт попадёт возвратов: {total}" if total else
                        "За это число полученных возвратов без акта нет"),
        }
    made = [return_acts.from_received(_source(account), account["id"], day, user=user)
            for account in where if found[account["id"]]]
    made = [result for result in made if result.get("act_id")]
    if not made:
        return {"status": "warning", "act_id": None, "message": "За это число полученных возвратов без акта нет"}
    if len(made) == 1:
        return made[0]
    return {"status": "ok", "act_id": made[0]["act_id"], "acts": [result["act_id"] for result in made],
            "message": f"Составлено актов: {len(made)} — по одному на кабинет"}


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
    params = {key: value for key, value in request.query_params.items() if key not in ("scope", "shop")}
    # Без выбранного кабинета лист общий: «текущего кабинета» больше нет.
    shop = request.query_params.get("shop") or ""
    everywhere = scope == ALL_ACCOUNTS or shop in ("", shops.ALL)

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
        account = _one(shop)
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

    # Кабинет строки называет запрос: в акте «Все кабинеты» рядом лежат
    # возвраты разных магазинов. Не назван — ищем строку по всем кабинетам
    # этой площадки; нашлась в двух — пусть назовут.
    ids = [a["id"] for a in returns_shops() if a["marketplace"] == code]
    wanted = payload.get("account_id")
    if wanted:
        ids = [account_id for account_id in ids if str(account_id) == str(wanted)]
    rows = db.query(
        f"SELECT account_id, id, act_id FROM {table} WHERE id = ? AND account_id IN ({','.join('?' for _ in ids)})",
        (return_id, *ids),
    ) if ids else []
    if not rows:
        raise HTTPException(status_code=404, detail=f"Возврат {return_id} не найден")
    if len(rows) > 1:
        raise HTTPException(status_code=409, detail=f"Возврат {return_id} есть в нескольких кабинетах — укажите кабинет")
    row = rows[0]
    account = accounts.get(row["account_id"])

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
