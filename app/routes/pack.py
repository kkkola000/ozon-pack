"""Рабочее место сборщика — один раздел на все площадки.

Страница «Сборка» одна: поле сканирования, очередь со счётчиками, последние
сканы и список заказов всех кабинетов. Обработчик общий, а площадка объявляет
своё в Workspace: подписи, плитки очереди, как сканировать и чей это код.

Кабинет в шапке тут ничего не решает. Решает **фильтр кабинетов** вверху —
чипы по одному на каждый кабинет, заведённый в панель:

* «Все заказы» — сборка идёт по всем кабинетам сразу. Стоите в Ozon, в руках
  этикетка Avito — панель сама найдёт её кабинет и откроет заказ.
* выбран кабинет — сборка в его границах, и наклейка другого кабинета честно
  не откроется, даже если он той же площадки.

Кто и как выбирает кабинет под скан — в `core/packing.py`. Адреса `/pack`,
`/avito/pack`, `/yandex/pack` — просто разные двери в один и тот же раздел:
каждая площадка объявляет свой в `Workspace.url`, и по нему регистрируется
маршрут.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from ..core import db
from ..core import labels as core_labels
from ..core import orders as core_orders
from ..core import packing as core_packing
from ..core import printers as core_printers
from ..core import store as core_store
from ..core import sync as core_sync
from ..core.deps import check_csrf, require_account, require_section, safe_filename, templates
from ..markets.base import MarketError

router = APIRouter()

# «Все заказы»: фильтр не выбран, сборка идёт по всем кабинетам.
ALL = core_packing.ALL

# Подписи поля сканирования, когда фильтра нет: кабинет заранее неизвестен.
ALL_PLACEHOLDER = "Сканируйте штрихкод товара или наклейку заказа…"
ALL_BANNER = "Отсканируйте штрихкод товара или наклейку — панель найдёт заказ в любом кабинете."


def _registry():
    from ..markets import registry

    return registry


def _picked(request: Request) -> str:
    """Какой кабинет выбран фильтром. Неизвестный считаем за «Все заказы»."""
    return core_packing.filter_of(request.query_params.get("shop"))


def shops_filter(path: str, orders: list[dict], picked: str) -> list[dict]:
    """Чипы фильтра: кабинет, сколько у него заказов и куда ведёт выбор.

    Чип на каждый кабинет, заведённый в панель, — в том же порядке, что в
    «Настройках». Кабинет без ключей не показываем: собирать в нём нечего.
    Цветная точка — площадка кабинета, чтобы два «Магазина» разных площадок не
    путались.

    Считаем по тому же списку, что показан на странице, — иначе число на чипе
    и число строк под ним разойдутся, и это первое, что заметят.

    Ссылка остаётся на текущем адресе: кабинет в шапке фильтр не переключает,
    он только сужает — и список, и наклейки, и сборку.
    """
    counts: dict[int, int] = {}
    for order in orders:
        counts[order["account_id"]] = counts.get(order["account_id"], 0) + 1
    chips = [{
        "id": ALL, "title": "Все заказы", "market": None, "market_title": "",
        "count": len(orders), "href": f"{path}?shop={ALL}", "active": picked == ALL,
    }]
    for shop in core_packing.shops(ALL):
        market = _registry().get(shop["marketplace"])
        if market is None or market.workspace is None:
            continue
        chips.append({
            "id": str(shop["id"]),
            "title": shop["title"],
            "market": market.code,
            "market_title": market.title,
            "count": counts.get(shop["id"], 0),
            "href": f"{path}?shop={shop['id']}",
            "active": picked == str(shop["id"]),
        })
    return chips


def _words(picked: str, where: list[dict]) -> tuple[str, str]:
    """Подписи поля сканирования: под фильтром — слова площадки кабинета, иначе общие."""
    if picked == ALL or not where:
        return ALL_PLACEHOLDER, ALL_BANNER
    workspace = core_packing.workspace_of(where[0])
    if workspace is None:
        return ALL_PLACEHOLDER, ALL_BANNER
    return workspace.placeholder, workspace.banner


def _freshened(where: list[dict]) -> str | None:
    """Сходить за свежими заказами во все кабинеты под фильтром.

    Не чаще раза в SYNC_INTERVAL на кабинет — F5 не должен становиться запросом
    к API, а отказавшая площадка не должна задерживать страницу. «Обновлено» —
    самый старый из успешных заходов: список свежий ровно настолько, насколько
    свеж самый отставший кабинет.
    """
    stamps = []
    for shop in where:
        core_sync.freshen(shop)
        stamps.append(core_sync.synced_at(shop["id"]))
    if not stamps or any(stamp is None for stamp in stamps):
        return None
    return min(stamps)


def _tabs(account: dict) -> str:
    """Какой пункт меню подсветить: «Сборка» той площадки, чей кабинет в шапке."""
    market = _registry().get(account.get("marketplace"))
    return market.workspace.tab if market and market.workspace else "pack"


def page(request: Request, user: dict, account: dict):
    """Собрать страницу рабочего места. Площадка адреса роли не играет."""
    picked = _picked(request)
    where = core_packing.shops(picked)
    synced_at = _freshened(where)
    orders = core_orders.everywhere()
    shown = [order for order in orders if picked == ALL or str(order["account_id"]) == picked]
    tiles, counters = core_packing.tiles(picked, where)
    placeholder, banner = _words(picked, where)
    return templates.TemplateResponse(
        request,
        "market_pack.html",
        {
            "request": request,
            "user": user,
            "account": account,
            "placeholder": placeholder,
            "banner": banner,
            "tiles": tiles,
            "counters": counters,
            "orders": shown,
            # Список идёт под фильтром, а счётчики на чипах — по всему списку:
            # иначе выбранный кабинет показывал бы соседям ноль.
            "shop_chips": shops_filter(request.url.path, orders, picked),
            "picked_shop": picked,
            # Карточку открытой сборки рисует площадка её заказа, а он может
            # быть из любого кабинета — подключаем все.
            "pack_markets": [market.code for market in _registry().all_markets() if market.workspace],
            # «Обновлено» — время последнего похода на площадку, а не опроса панели.
            "synced_at": core_store.local_time(synced_at),
            "csrf": request.state.session.get("csrf"),
            "active_tab": _tabs(account),
        },
    )


def _endpoint():
    """Обработчик адреса площадки: страница одна, площадка адреса роли не играет."""

    def pack_page(request: Request,
                  shop: str = ALL,  # noqa: ARG001 - читается из query_params, нужен для /docs
                  user: dict = Depends(require_section("pack")),
                  account: dict = Depends(require_account)):
        return page(request, user, account)

    return pack_page


def register() -> APIRouter:
    """Зарегистрировать рабочее место по адресу каждой площадки из реестра."""
    for market in _registry().all_markets():
        if market.workspace is None:
            continue
        router.add_api_route(
            market.workspace.url, _endpoint(), methods=["GET"],
            response_class=HTMLResponse, name=f"pack_{market.code}",
        )
    return router


# ------------------------------------------------------------------ сканирование
def _answer(request: Request, result: dict) -> dict:
    """Дописать к ответу плитки очереди — они считаются по фильтру."""
    picked = _picked(request)
    _tiles, counters = core_packing.tiles(picked, core_packing.shops(picked))
    result["counters"] = counters
    return result


@router.post("/api/pack/scan")
def api_scan(request: Request, payload: dict = Body(...),
             user: dict = Depends(require_section("pack")),
             account: dict = Depends(require_account)):  # noqa: ARG001 - нужен для проверки доступа
    """Один скан. В каком кабинете искать код — решает packing, а не шапка."""
    check_csrf(request)
    picked = _picked(request)
    result = core_packing.scan(core_packing.shops(picked), user, str(payload.get("code") or ""))
    return _answer(request, result)


@router.post("/api/pack/release")
def api_release(request: Request, user: dict = Depends(require_section("pack")),
                account: dict = Depends(require_account)):  # noqa: ARG001 - нужен для проверки доступа
    """Отменить начатую сборку — в том кабинете, где она открыта."""
    check_csrf(request)
    return _answer(request, core_packing.release(user))


@router.post("/api/pack/complete")
def api_complete(request: Request, payload: dict = Body(default={}),
                 user: dict = Depends(require_section("pack")),
                 account: dict = Depends(require_account)):  # noqa: ARG001 - нужен для проверки доступа
    """«Завершить без скана наклейки» — например, если она не читается."""
    check_csrf(request)
    try:
        result = core_packing.complete(user, str(payload.get("reason") or "ручное завершение"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _answer(request, result)


@router.post("/api/pack/sync")
def api_sync(request: Request, user: dict = Depends(require_section("pack")),
             account: dict = Depends(require_account)):  # noqa: ARG001 - нужен для проверки доступа
    """«Обновить заказы» — все кабинеты под фильтром, как и всё остальное здесь."""
    check_csrf(request)
    where = core_packing.shops(_picked(request))
    if not where:
        raise HTTPException(status_code=400, detail="Нет ни одного кабинета с ключами")
    done, failed = [], []
    for shop in where:
        try:
            core_sync.sync_account(shop, returns=False)
            done.append(shop["title"])
        except Exception as exc:  # noqa: BLE001 - отказ одного кабинета не отменяет остальных
            failed.append(f"{shop['title']}: {exc}")
    if not done:
        raise HTTPException(status_code=502, detail="; ".join(failed))
    message = f"Обновлено кабинетов: {len(done)}"
    if failed:
        message += f". Не ответили: {', '.join(name.split(':')[0] for name in failed)}"
    return {"status": "ok", "message": message, "updated": done, "failed": failed}


# ------------------------------------------------------------------ состояние
@router.get("/api/pack/state")
def api_state(request: Request, user: dict = Depends(require_section("pack")),
              account: dict = Depends(require_account)):  # noqa: ARG001 - нужен для проверки доступа
    """Состояние рабочего места: начатая сборка, очередь и замок на наклейки.

    Замок общий на кабинеты под фильтром: сборка объединена, и начинать её,
    выгрузив наклейки одного магазина, нельзя — посреди смены окажется, что у
    соседнего заказа наклейки нет и взять её уже негде.
    """
    picked = _picked(request)
    where = core_packing.shops(picked)
    _tiles, counters = core_packing.tiles(picked, where)
    waiting = core_labels.pending_everywhere(where)
    return {
        "state": core_packing.state(user),
        "counters": counters,
        "labels": core_labels.state_everywhere(waiting),
    }


# ------------------------------------------------------------------ наклейки
@router.get("/api/pack/label/{code}/{order_id}.pdf")
def api_label(code: str, order_id: str, user: dict = Depends(require_section("pack")),
              account: dict = Depends(require_account)):  # noqa: ARG001 - нужен для проверки доступа
    """Наклейка одного заказа на печать — из того кабинета, где заказ лежит.

    Кабинет в шапке тут ни при чём: сборщик печатает наклейку того заказа, что
    у него открыт, а открыть он мог заказ любого магазина.
    """
    shop = core_packing.shop_of(code, order_id, user)
    workspace = core_packing.workspace_of(shop) if shop else None
    if workspace is None or workspace.label is None:
        raise HTTPException(status_code=404, detail=f"Заказ {order_id} не найден ни в одном кабинете")
    try:
        pdf, filename = workspace.label(shop, user, order_id)
    except MarketError as exc:
        raise HTTPException(status_code=502, detail=f"Площадка не отдала наклейку: {exc.message}") from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename(filename)}"',
                 "Cache-Control": "no-store",
                 # Размер листа — по нему браузер выбирает принтер (см. «Принтеры»).
                 **core_printers.size_header(pdf)},
    )


@router.post("/api/pack/labels.zip")
def api_labels(request: Request, user: dict = Depends(require_section("pack")),
               account: dict = Depends(require_account)):  # noqa: ARG001 - нужен для проверки доступа
    """Наклейки всех кабинетов одним архивом — то, с чего начинается смена."""
    check_csrf(request)
    waiting = core_labels.pending_everywhere(core_packing.shops(_picked(request)))
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
