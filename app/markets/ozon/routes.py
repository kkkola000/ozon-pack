"""Разделы Ozon: рабочее место сборщика и «Заказы FBS».

Сборка: сканирование, печать стикеров, завершение. Заказы: списки по
вкладкам, сборка на стороне Ozon, печать стикеров пачкой.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ...core import access, db, labels, options, store, sync
from ...core.config import settings
from ...core.deps import check_csrf, require_section, require_ozon_account, safe_filename, templates
from . import pack as packing
from .client import OzonError

router = APIRouter()

# Сколько стикеров печатается одним запросом.
MAX_LABELS = 50


@router.get("/pack", response_class=HTMLResponse)
def pack_page(request: Request, user: dict = Depends(require_section("pack")),
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
    # «Возвраты к выдаче» — прямо по отмеченным статусам, см. options.pickup_sql.
    ready_sql, ready_params = options.pickup_sql()
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
            f"SELECT COUNT(*) AS c FROM returns WHERE account_id = ? AND {ready_sql}",
            [account_id] + list(ready_params),
        )["c"],
    }


@router.get("/api/state")
def api_state(user: dict = Depends(require_section("pack")), account: dict = Depends(require_ozon_account)):
    return {
        "state": packing.load_state(account, user),
        "counters": _counters(account),
        "labels": labels.ozon_state(account["id"]),
    }


@router.post("/api/scan")
def api_scan(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("pack")),
             account: dict = Depends(require_ozon_account)):
    check_csrf(request)
    result = packing.scan(account, user, str(payload.get("code") or ""))
    result["counters"] = _counters(account)
    return result


@router.post("/api/release")
def api_release(request: Request, user: dict = Depends(require_section("pack")),
                account: dict = Depends(require_ozon_account)):
    check_csrf(request)
    result = packing.release(account, user)
    result["counters"] = _counters(account)
    return result


@router.post("/api/complete")
def api_complete(request: Request, payload: dict = Body(default={}), user: dict = Depends(require_section("pack")),
                 account: dict = Depends(require_ozon_account)):
    """Ручное завершение — например, если стикер не читается сканером."""
    check_csrf(request)
    state = packing.load_state(account, user)
    if not state["active"]:
        raise HTTPException(status_code=400, detail="Нет активного отправления")
    if settings.require_all_items and not state["complete"] and not access.is_manager(user):
        # Говорим, чего именно не хватает: у набора — недостающие части, а не
        # его название. К полке с названием набора не пойдёшь.
        raise HTTPException(
            status_code=400,
            detail="Сначала отсканируйте все товары. Осталось: "
                   + "; ".join(packing.missing_items(state)),
        )
    result = packing.complete(
        account, user, state["active"]["posting_number"], code=payload.get("reason") or "ручное завершение"
    )
    result["counters"] = _counters(account)
    return result


@router.get("/api/label/{posting_number}.pdf")
def api_label(posting_number: str, user: dict = Depends(require_section("pack")),
              account: dict = Depends(require_ozon_account)):
    try:
        pdf, filename = packing.label_pdf(account, user, [posting_number])
    except OzonError as exc:
        raise HTTPException(status_code=502, detail=f"Ozon не отдал стикер: {exc.message}") from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename(filename)}"',
                 "Cache-Control": "no-store"},
    )


@router.post("/api/labels/archive.zip")
def api_labels_archive(request: Request, user: dict = Depends(require_section("pack")),
                       account: dict = Depends(require_ozon_account)):
    """Стикеры всех отправлений, ждущих выгрузки, — архивом на компьютер.

    Панель файл у себя не оставляет: архив уходит в браузер, на диске сервера
    не остаётся ничего. В базе появляется только отметка о выгрузке — по ней
    открывается сканирование.
    """
    check_csrf(request)
    numbers = labels.pending_ozon(account["id"])
    if not numbers:
        raise HTTPException(status_code=400, detail="Все стикеры уже выгружены")
    numbers = numbers[: labels.MAX_AT_ONCE]
    archive, saved = labels.build_archive(
        numbers, lambda batch: packing.label_pdf(account, user, batch)[0], prefix="стикеры",
    )
    if not saved:
        raise HTTPException(status_code=502, detail="Ozon не отдал ни одного стикера")
    labels.mark_saved("postings", account["id"], saved, "posting_number")
    db.log_event(
        "labels_archive", account_id=account["id"], user=user,
        message=f"Выгружены стикеры: {len(saved)} шт.",
    )
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{_archive_name(account)}"',
                 "Cache-Control": "no-store"},
    )


def _archive_name(account: dict) -> str:
    """Имя архива: по нему на компьютере видно, чьи это стикеры и за когда."""
    stamp = store.local_time(db.now_iso(), "%Y-%m-%d_%H-%M")
    return safe_filename(f"stickers-{account.get('title') or account['id']}-{stamp}.zip")


@router.post("/api/labels.pdf")
def api_labels(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("pack")),
               account: dict = Depends(require_ozon_account)):
    """Пачка стикеров — для печати нескольких отправлений сразу."""
    check_csrf(request)
    numbers = [str(n) for n in (payload.get("posting_numbers") or []) if n]
    if not numbers:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного отправления")
    # Без потолка один запрос уносит в Ozon сколько угодно номеров и выбирает
    # лимиты кабинета на всех сразу. Столько же, сколько печатает Avito.
    if len(numbers) > MAX_LABELS:
        raise HTTPException(
            status_code=400, detail=f"За один раз печатается не больше {MAX_LABELS} стикеров"
        )
    try:
        pdf, filename = packing.label_pdf(account, user, numbers)
    except OzonError as exc:
        raise HTTPException(status_code=502, detail=f"Ozon не отдал стикеры: {exc.message}") from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename(filename)}"',
                 "Cache-Control": "no-store"},
    )


# ================================================================== заказы FBS

# «Собранные» — это очередь на отгрузку, а не архив: как только Ozon переводит
# отправление дальше (отгружено, доставляется, доставлено), оно уходит из списка.
# История сборки при этом остаётся в журнале и в самой записи отправления.
TABS = {
    "packaging": ("Ожидает сборки", "p.status = 'awaiting_packaging'"),
    "deliver": ("Ожидает отгрузки", "p.status = 'awaiting_deliver' AND p.local_state = 'new'"),
    "packed": ("Собранные", "p.local_state = 'packed' AND p.status = 'awaiting_deliver'"),
}


def _list_postings(account: dict, tab: str, search: str = "", limit: int = 300) -> list[dict]:
    _title, condition = TABS.get(tab, TABS["packaging"])
    params: list = [account["id"]]
    sql = f"SELECT p.* FROM postings p WHERE p.account_id = ? AND {condition}"
    if search:
        like = f"%{search.strip()}%"
        sql += """
            AND (p.posting_number LIKE ? OR p.order_number LIKE ? OR p.city LIKE ?
                 OR EXISTS (SELECT 1 FROM posting_items i WHERE i.posting_number = p.posting_number
                            AND i.account_id = p.account_id
                            AND (i.name LIKE ? OR i.offer_id LIKE ? OR i.sku LIKE ?)))
        """
        params += [like] * 6
    order = "p.packed_at DESC" if tab == "packed" else "(p.shipment_date IS NULL), p.shipment_date"
    sql += f" ORDER BY {order} LIMIT ?"
    params.append(limit)
    return [store.posting_view(row) for row in db.query(sql, params)]


@router.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, tab: str = "packaging", q: str = "", user: dict = Depends(require_section("orders")),
                account: dict = Depends(require_ozon_account)):
    if tab not in TABS:
        tab = "packaging"
    postings = _list_postings(account, tab, q)
    counts = {
        key: db.query_one(
            f"SELECT COUNT(*) AS c FROM postings p WHERE p.account_id = ? AND {condition}", (account["id"],)
        )["c"]
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
            "account": account,
            "search": q,
            "sync": sync.status(),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "orders",
        },
    )


@router.get("/api/orders")
def api_orders(tab: str = "packaging", q: str = "", user: dict = Depends(require_section("orders")),
               account: dict = Depends(require_ozon_account)):
    return {"postings": _list_postings(account, tab, q)}


@router.post("/api/ship")
def api_ship(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("orders")),
             account: dict = Depends(require_ozon_account)):
    """Перевести отправления в «Ожидает отгрузки»."""
    check_csrf(request)
    numbers = [str(n) for n in (payload.get("posting_numbers") or []) if n]
    if not numbers:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного отправления")
    results = [packing.ship_posting(account, user, number) for number in numbers]
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
def api_sync(request: Request, user: dict = Depends(require_section("orders")),
             account: dict = Depends(require_ozon_account)):
    """Обновить данные текущего кабинета по кнопке."""
    check_csrf(request)
    try:
        result = sync.run_once(returns=True, account=account)
    except Exception as exc:  # noqa: BLE001 - показываем причину оператору
        raise HTTPException(status_code=502, detail=f"Синхронизация не удалась: {exc}") from exc
    return {"status": "ok", "message": "Данные обновлены", "result": result, "sync": sync.status()}
