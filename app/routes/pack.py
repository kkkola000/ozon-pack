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

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from ..core import accounts, db
from ..core import labels as core_labels
from ..core import orders as core_orders
from ..core import store as core_store
from ..core import sync as core_sync
from ..core.deps import (ACCOUNT_COOKIE, check_csrf, require_account, require_section,
                         safe_filename, templates)

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


def _switch(code: str, current: dict, *, keep: str = ALL) -> RedirectResponse | None:
    """Нужна площадка, а кабинет другой — переключаем на её первый кабинет.

    Без этого фильтр обманывал бы: список показывал бы заказы Avito, а
    сканирование продолжало искать их в кабинете Ozon. Тем же путём открывается
    и адрес чужой площадки: «Сборка» одна, и её адреса — просто разные двери.
    """
    if code == ALL or code == current.get("marketplace"):
        return None
    theirs = [account for account in accounts.all_accounts(active_only=True)
              if account["marketplace"] == code]
    if not theirs:
        return None
    market = _registry().get(code)
    # Фильтр сохраняем как есть: открыли адрес площадки со списком «Все заказы»
    # — список и останется общим, переехал только кабинет.
    where = market.workspace.url + (f"?market={keep}" if keep != ALL else "")
    answer = RedirectResponse(where, status_code=303)
    answer.set_cookie(ACCOUNT_COOKIE, str(theirs[0]["id"]), httponly=True, samesite="lax")
    return answer


def page(request: Request, user: dict, account: dict, code: str, market: str = ALL):
    """Собрать страницу рабочего места кабинета."""
    declared = _registry().get(code)
    # Куда нужно попасть: выбранная фильтром площадка, иначе — площадка адреса.
    wanted = market if market != ALL else code
    moved = _switch(wanted, account, keep=market)
    if moved is not None:
        return moved
    if account["marketplace"] != code:
        raise HTTPException(
            status_code=409,
            detail=f"Кабинета площадки «{declared.title}» в панели нет — заведите его в «Настройках»",
        )
    workspace = declared.workspace
    # Открыли рабочее место — сходили на площадку за свежими заказами. Не чаще
    # раза в SYNC_INTERVAL: F5 не должен становиться запросом к API.
    core_sync.freshen(account)
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
            # «Обновлено» — время последнего похода на площадку, а не опроса панели.
            "synced_at": core_store.local_time(core_sync.synced_at(account["id"])),
            "csrf": request.state.session.get("csrf"),
            "active_tab": workspace.tab,
        },
    )


def _shops(picked: str) -> list[dict]:
    """Кабинеты под фильтром: все включённые или только выбранной площадки."""
    live = [account for account in accounts.all_accounts(active_only=True)
            if accounts.is_configured(account)]
    if picked == ALL:
        return live
    return [account for account in live if account["marketplace"] == picked]


def _picked_of(request: Request) -> str:
    """Какая площадка выбрана фильтром — по адресу страницы, с которой пришёл запрос."""
    wanted = str(request.query_params.get("market") or ALL)
    return wanted if wanted != ALL and _registry().get(wanted) else ALL


def _endpoint(code: str):
    """Обработчик адреса одной площадки: тот же page(), своя площадка.

    Кабинет специально не требуем жёстко: открыли адрес чужой площадки —
    панель переключит кабинет сама, как это делает чип фильтра. Отказ остаётся
    только там, где кабинета такой площадки нет вовсе.
    """

    def pack_page(request: Request, market: str = ALL,
                  user: dict = Depends(require_section("pack")),
                  account: dict = Depends(require_account)):
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


# ------------------------------------------------------------------ наклейки
@router.get("/api/pack/state")
def api_state(request: Request, user: dict = Depends(require_section("pack")),
              account: dict = Depends(require_account)):
    """Состояние рабочего места: сборка, очередь и замок на наклейки.

    Замок общий на кабинеты под фильтром: сборка объединена, и начинать её,
    выгрузив наклейки одного магазина, нельзя — посреди смены окажется, что у
    соседнего заказа наклейки нет и взять её уже негде.
    """
    market = _registry().get(account["marketplace"])
    workspace = market.workspace if market else None
    if workspace is None:
        raise HTTPException(status_code=409, detail="У этой площадки нет рабочего места")
    waiting = core_labels.pending_everywhere(_shops(_picked_of(request)))
    return {
        "state": workspace.load_state(account, user),
        "counters": workspace.count_queue(account),
        "labels": core_labels.state_everywhere(waiting),
    }


@router.post("/api/pack/labels.zip")
def api_labels(request: Request, user: dict = Depends(require_section("pack")),
               account: dict = Depends(require_account)):  # noqa: ARG001 - нужен для проверки доступа
    """Наклейки всех кабинетов одним архивом — то, с чего начинается смена."""
    check_csrf(request)
    waiting = core_labels.pending_everywhere(_shops(_picked_of(request)))
    if not waiting:
        raise HTTPException(status_code=400, detail="Все наклейки уже выгружены")
    archive, total, refused = core_labels.archive_everywhere(waiting, user)
    if not total:
        raise HTTPException(
            status_code=502,
            detail="Площадка не отдала ни одной наклейки: " + ", ".join(refused),
        )
    stamp = core_store.local_time(db.now_iso(), "%Y-%m-%d_%H-%M")
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename(f"naklejki-{stamp}.zip")}"',
            "Cache-Control": "no-store",
            # Сколько выгружено и кто отказал. Названия кабинетов русские, а в
            # заголовок HTTP кириллица не лезет — отдаём в процентах.
            "X-Labels-Saved": str(total),
            "X-Labels-Refused": quote(", ".join(refused)),
        },
    )
