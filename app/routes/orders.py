"""Раздел «Заказы» — один на все кабинеты.

Сверху фильтр кабинетов («Все заказы» или один кабинет), под ним статусы
склада: «Ожидает сборки», «Ожидает отгрузки», «Собран». Что с ними считать и
как показывать — в core/board.py, что значит статус у каждой площадки — в её
объявлении OrdersBoard.

Кабинет в шапке тут ни при чём, и это нарочно: переключатель скоро уйдёт.
Каждое действие — собрать в Ozon, подтвердить в Avito, наклейки, снять
отметку — несёт кабинет своего заказа, и выполняется в нём.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ..core import board as core_board
from ..core import printers as core_printers
from ..core import store as core_store
from ..core import sync as core_sync
from ..core.deps import check_csrf, require_manager, require_section, safe_filename, templates
from ..markets.base import MarketError

router = APIRouter()

ALL = core_board.ALL


def _synced(where: list[dict]) -> str | None:
    """«Обновлено» — самый старый из заходов на площадку по кабинетам под фильтром."""
    stamps = [core_sync.synced_at(shop["id"]) for shop in where]
    if not stamps or any(stamp is None for stamp in stamps):
        return None
    return min(stamps)


@router.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, shop: str = ALL, status: str = "", tab: str = "", q: str = "",
                user: dict = Depends(require_section("orders"))):
    """Страница раздела. tab — старое имя статуса в ссылках «Заказов FBS»."""
    picked = core_board.filter_of(shop)
    wanted = core_board.status_of(status or tab)
    data = core_board.page(picked, wanted, q)
    return templates.TemplateResponse(
        request,
        "orders.html",
        {
            "request": request,
            "user": user,
            **data,
            "picked": picked,
            "status": wanted,
            "search": q,
            "synced_at": core_store.local_time(_synced(data["where"])),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "orders",
        },
    )


# ------------------------------------------------------------------ действия
def _shop(payload: dict) -> dict:
    try:
        return core_board.shop(payload.get("account_id"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _ids(payload: dict) -> list[str]:
    ids = [str(value) for value in (payload.get("ids") or []) if str(value or "").strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного заказа")
    return ids


@router.post("/api/orders/action")
def api_action(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("orders"))):
    """Действие площадки над заказами одного кабинета: «Собрать в Ozon», «Подтвердить»…"""
    check_csrf(request)
    shop = _shop(payload)
    ids = _ids(payload)
    try:
        action = core_board.action_of(shop, str(payload.get("action") or ""))
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        result = action.run(shop, user, ids)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MarketError as exc:
        raise HTTPException(status_code=502, detail=f"Площадка отказала: {exc.message}") from exc
    return {**result, "shop": shop["title"]}


@router.post("/api/orders/reset")
def api_reset(request: Request, payload: dict = Body(...), admin: dict = Depends(require_manager)):
    """Снять отметку «Собран» — только администратор и владелец.

    Отметка — результат работы сборщика, и снимать её должен тот, кто отвечает
    за склад, а не тот, кто ошибся.
    """
    check_csrf(request)
    shop = _shop(payload)
    try:
        message = core_board.board_of(shop).reset(shop, admin, str(payload.get("id") or ""))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "message": message}


@router.post("/api/orders/labels.pdf")
def api_labels(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("orders"))):
    """Наклейки заказов одного кабинета — файл площадки как есть.

    Выбраны заказы нескольких кабинетов — браузер спрашивает по кабинету:
    у каждой площадки свой файл и свой принтер (см. «Принтеры»).
    """
    check_csrf(request)
    shop = _shop(payload)
    ids = _ids(payload)
    board = core_board.board_of(shop)
    if len(ids) > board.max_labels:
        raise HTTPException(
            status_code=400, detail=f"За один раз площадка отдаёт не больше {board.max_labels} наклеек"
        )
    try:
        pdf, filename = board.labels(shop, user, ids)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MarketError as exc:
        raise HTTPException(status_code=502, detail=f"Площадка не отдала наклейки: {exc.message}") from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename(filename)}"',
                 "Cache-Control": "no-store",
                 # Размер листа — по нему браузер выбирает принтер (см. «Принтеры»).
                 **core_printers.size_header(pdf)},
    )


@router.post("/api/orders/sync")
def api_sync(request: Request, user: dict = Depends(require_section("orders"))):  # noqa: ARG001 - доступ
    """«Обновить заказы» — все кабинеты под фильтром."""
    check_csrf(request)
    where = core_board.shops(core_board.filter_of(request.query_params.get("shop")))
    if not where:
        raise HTTPException(status_code=400, detail="Нет ни одного кабинета с ключами")
    done, failed = core_sync.sync_many(where)
    if not done:
        raise HTTPException(status_code=502, detail="; ".join(failed))
    message = f"Обновлено кабинетов: {len(done)}"
    if failed:
        message += f". Не ответили: {', '.join(name.split(':')[0] for name in failed)}"
    return {"status": "ok", "message": message, "updated": done, "failed": failed}
