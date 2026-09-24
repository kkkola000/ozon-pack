"""Рабочее место сборщика — один маршрут на все площадки.

Страница «Сборка» одна: поле сканирования, очередь со счётчиками, последние
сканы и список заказов всех кабинетов. Площадка отличается только словами и
двумя числами, поэтому обработчик здесь общий, а площадка объявляет своё в
Workspace: подписи, плитки очереди, откуда взять состояние сборщика и счётчики.

Адрес у каждой площадки свой (`/pack`, `/avito/pack`, `/yandex/pack`) — она
объявляет его в `Workspace.url`, и по нему маршрут регистрируется. Адрес требует
кабинета своей площадки: рабочее место всегда про конкретный магазин, собирают в
нём по одному.

Фильтр площадок вверху страницы переводит рабочее место на выбранную площадку:
выбрали «Avito» — панель переключает кабинет на её и собирает её заказы. «Все
заказы» ничего не переключает и показывает список целиком.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core import accounts
from ..core import orders as core_orders
from ..core.deps import ACCOUNT_COOKIE, require_market, require_section, templates

router = APIRouter()

# «Все заказы»: фильтр не выбран, список показывается целиком.
ALL = "all"


def _registry():
    from ..markets import registry

    return registry


def markets_filter(current: dict, orders: list[dict], picked: str) -> list[dict]:
    """Чипы фильтра: площадка, сколько у неё заказов и куда ведёт выбор.

    Считаем по тому же списку, что показан на странице, — иначе число на чипе
    и число строк под ним разойдутся, и это первое, что заметят.
    """
    counts: dict[str, int] = {}
    for order in orders:
        counts[order["market"]] = counts.get(order["market"], 0) + 1
    live = {account["marketplace"] for account in accounts.all_accounts(active_only=True)}
    chips = [{
        "code": ALL, "title": "Все заказы", "count": len(orders),
        "href": _home_of(current) + f"?market={ALL}", "active": picked == ALL,
    }]
    for market in _registry().all_markets():
        # Площадка без кабинета никуда не ведёт: её рабочее место откажет.
        if market.workspace is None or market.code not in live:
            continue
        chips.append({
            "code": market.code,
            "title": market.title,
            "count": counts.get(market.code, 0),
            # Ссылка остаётся на текущей странице: адрес чужой площадки
            # отказал бы раньше, чем панель успела переключить кабинет.
            # Переключит и уведёт куда надо обработчик — см. _switch().
            "href": _home_of(current) + f"?market={market.code}",
            "active": picked == market.code,
        })
    return chips


def _home_of(account: dict | None) -> str:
    """Адрес рабочего места кабинета."""
    market = _registry().get((account or {}).get("marketplace"))
    return market.workspace.url if market and market.workspace else "/pack"


def _switch(code: str, current: dict) -> RedirectResponse | None:
    """Выбрали площадку, а кабинет другой — переключаем на её первый кабинет.

    Без этого фильтр обманывал бы: список показывал бы заказы Avito, а
    сканирование продолжало искать их в кабинете Ozon.
    """
    if code == ALL or code == current.get("marketplace"):
        return None
    theirs = [account for account in accounts.all_accounts(active_only=True)
              if account["marketplace"] == code]
    if not theirs:
        return None
    market = _registry().get(code)
    answer = RedirectResponse(f"{market.workspace.url}?market={code}", status_code=303)
    answer.set_cookie(ACCOUNT_COOKIE, str(theirs[0]["id"]), httponly=True, samesite="lax")
    return answer


def page(request: Request, user: dict, account: dict, code: str, market: str = ALL):
    """Собрать страницу рабочего места кабинета."""
    moved = _switch(market, account)
    if moved is not None:
        return moved
    declared = _registry().get(code)
    workspace = declared.workspace
    orders = core_orders.everywhere(account)
    picked = market if market != ALL and _registry().get(market) else ALL
    # Список идёт под фильтром, а счётчики на чипах — по всему списку: иначе
    # выбранная площадка показывала бы сама себе ноль у соседей.
    shown = [order for order in orders if picked == ALL or order["market"] == picked]
    return templates.TemplateResponse(
        request,
        "market_pack.html",
        {
            "request": request,
            "user": user,
            "account": account,
            "state": workspace.load_state(account, user),
            "counters": workspace.count_queue(account),
            "orders": shown,
            "market_chips": markets_filter(account, orders, picked),
            "picked_market": picked,
            "workspace": workspace,
            "csrf": request.state.session.get("csrf"),
            "active_tab": workspace.tab,
        },
    )


def _endpoint(code: str):
    """Обработчик адреса одной площадки: тот же page(), свой кабинет."""

    def pack_page(request: Request, market: str = ALL,
                  user: dict = Depends(require_section("pack")),
                  account: dict = Depends(require_market(code))):
        return page(request, user, account, code, market)

    return pack_page


def register() -> APIRouter:
    """Зарегистрировать рабочее место по адресу каждой площадки из реестра."""
    for market in _registry().all_markets():
        if market.workspace is None:
            continue
        router.add_api_route(
            market.workspace.url, _endpoint(market.code), methods=["GET"],
            response_class=HTMLResponse, name=f"pack_{market.code}",
        )
    return router
