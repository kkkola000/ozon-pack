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

Кто и как выбирает кабинет под скан — в `core/packing.py`. Адрес один —
`/pack`; старые `/avito/pack` и `/yandex/pack` ведут сюда же (их держат сами
площадки).
"""
from __future__ import annotations

from contextlib import suppress
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from ..core import board as core_board
from ..core import accounts, assembly_stickers, db, marking
from ..core import labels as core_labels
from ..core import orders as core_orders
from ..core import orders_sheet
from ..core import packing as core_packing
from ..core import shops as core_shops
from ..core import store as core_store
from ..core import sync as core_sync
from ..core.deps import check_csrf, pdf_response, require_section, safe_filename, templates
from ..markets.base import MarketError

router = APIRouter()


@router.get('/api/pack/marking/{mark_id}.pdf')
def marking_pdf(mark_id: int, user: dict = Depends(require_section('pack'))):
    try:
        pdf = marking.print_pdf([mark_id])
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, ImportError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return pdf_response(pdf, f'chz-{mark_id}.pdf')


@router.post('/api/pack/marking/poll')
def marking_poll(request: Request, user: dict = Depends(require_section('pack'))):
    check_csrf(request)
    account = core_packing.started(user)
    if account:
        state = core_packing.workspace_of(account).load_state(account, user)
        active = state.get('active') or {}
        number = str(active.get('posting_number') or active.get('id') or '')
        if number:
            # Фоновый поток может уже выполнять ту же операцию.
            with suppress(ValueError):
                marking.process(account, number)
    return {'state': core_packing.state(user)}

# «Все заказы»: фильтр не выбран, сборка идёт по всем кабинетам.
ALL = core_packing.ALL

# Подписи поля сканирования, когда фильтра нет: кабинет заранее неизвестен.
ALL_WORDS = {
    "title": "Сканируйте товар или наклейку заказа",
    "hint": "Панель сама найдёт заказ в любом кабинете и отправит наклейку на печать",
}


def _registry():
    from ..markets import registry

    return registry


def _picked(request: Request) -> str:
    """Какой кабинет выбран фильтром. Неизвестный считаем за «Все заказы»."""
    return core_packing.filter_of(request.query_params.get("shop"))


def shops_filter(path: str, orders: list[dict], picked: str) -> list[dict]:
    """Чипы фильтра: кабинет, сколько у него заказов к сборке и куда ведёт выбор.

    Считаем только «Ожидает отгрузки» — то, что можно собирать прямо сейчас,
    ровно те строки, что видны под галочкой «только к сборке». «Ожидает сборки»
    и собранное в число не входят: на сборку они не попадают.
    """
    counts: dict[int, int] = {}
    for order in orders:
        if order["in_work"]:
            counts[order["account_id"]] = counts.get(order["account_id"], 0) + 1
    return core_shops.chips(core_packing.shops(ALL), picked, counts,
                            lambda value: f"{path}?shop={value}", all_title="Все заказы")


def _words(picked: str, where: list[dict]) -> dict:
    """Подписи поля сканирования: под фильтром — слова площадки кабинета, иначе общие."""
    workspace = core_packing.workspace_of(where[0]) if picked != ALL and where else None
    if workspace is None:
        return ALL_WORDS
    return {"title": workspace.title, "hint": workspace.hint}


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


@router.get("/pack", response_class=HTMLResponse)
def page(request: Request,
         shop: str = ALL,  # noqa: ARG001 - читается из query_params, нужен для /docs
         user: dict = Depends(require_section("pack"))):
    """Страница рабочего места — одна на все кабинеты."""
    picked = _picked(request)
    where = core_packing.shops(picked)
    synced_at = _freshened(where)
    orders = core_orders.everywhere()
    shown = [order for order in orders if picked == ALL or str(order["account_id"]) == picked]
    tiles, counters = core_packing.tiles(picked, where)
    return templates.TemplateResponse(
        request,
        "market_pack.html",
        {
            "request": request,
            "user": user,
            "scan": _words(picked, where),
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
            "active_tab": "pack",
        },
    )


# ------------------------------------------------------------------ сканирование
def _answer(request: Request, result: dict) -> dict:
    """Дописать к ответу плитки очереди по фильтру кабинетов."""
    picked = _picked(request)
    where = core_packing.shops(picked)
    _tiles, counters = core_packing.tiles(picked, where)
    result["counters"] = counters
    return result


@router.post("/api/pack/scan")
def api_scan(request: Request, payload: dict = Body(...),
             user: dict = Depends(require_section("pack"))):
    """Один скан. В каком кабинете искать код — решает packing, а не шапка."""
    check_csrf(request)
    picked = _picked(request)
    result = core_packing.scan(core_packing.shops(picked), user, str(payload.get("code") or ""))
    return _answer(request, result)


@router.post("/api/pack/release")
def api_release(request: Request, user: dict = Depends(require_section("pack"))):
    """Отменить начатую сборку — в том кабинете, где она открыта."""
    check_csrf(request)
    return _answer(request, core_packing.release(user))


@router.post("/api/pack/complete")
def api_complete(request: Request, payload: dict = Body(default={}),
                 user: dict = Depends(require_section("pack"))):
    """«Завершить без скана наклейки» — например, если она не читается."""
    check_csrf(request)
    try:
        result = core_packing.complete(user, str(payload.get("reason") or "ручное завершение"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _answer(request, result)


@router.post("/api/pack/sync")
def api_sync(request: Request, user: dict = Depends(require_section("pack"))):
    """«Обновить заказы» — все кабинеты под фильтром, как и всё остальное здесь."""
    check_csrf(request)
    try:
        return core_sync.refresh(core_packing.shops(_picked(request)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ------------------------------------------------------------------ состояние
@router.get("/api/pack/state")
def api_state(request: Request, user: dict = Depends(require_section("pack"))):
    """Начатая сборка, её стикер для скачивания и очередь заказов."""
    picked = _picked(request)
    where = core_packing.shops(picked)
    _tiles, counters = core_packing.tiles(picked, where)
    return {
        "state": core_packing.state(user),
        "counters": counters,
    }


# ------------------------------------------------------------------ лист с заказами
@router.get("/api/pack/orders-sheet.pdf")
def api_orders_sheet(request: Request, user: dict = Depends(require_section("pack"))):
    """«Лист с заказами»: кабинет, фото, товар, артикул и количество — под заказы к сборке.

    Кабинеты — те, что под фильтром «Сборки»; заказы — «К сборке», как в
    плитке очереди. Одинаковый товар кабинета — одной строкой.

    Лист сразу уходит на печать: через QZ Tray, если для него выбран принтер
    («Принтеры» → «Лист с заказами»), иначе окном печати браузера. Оба берут
    файл простой ссылкой, поэтому GET, а файл — inline: с attachment браузер
    скачал бы его вместо печати.
    """
    picked = _picked(request)
    where = core_packing.shops(picked)
    scope = "все кабинеты" if picked == ALL else (where[0]["title"] if where else "—")
    try:
        pdf = orders_sheet.build(orders_sheet.collect(where), user=user, scope=scope)
    except orders_sheet.NothingToPick as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except orders_sheet.PdfUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    db.log_event("orders_sheet", user=user, message=f"Лист с заказами: {scope}")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{safe_filename(orders_sheet.filename(picked))}"',
            "Cache-Control": "no-store",
        },
    )


# ------------------------------------------------------------------ наклейки
@router.get('/api/pack/sticker/{account_id}/{order_id}.pdf')
def download_sticker(account_id: int, order_id: str, user: dict = Depends(require_section('pack'))):
    """Оригинальный стикер на компьютер; счётчик печати не меняется."""
    account = accounts.get(account_id)
    if not account or account_id not in {shop['id'] for shop in core_packing.shops(ALL)}:
        raise HTTPException(404, 'Кабинет не найден')
    try:
        pdf = assembly_stickers.load(account, order_id, user)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except assembly_stickers.NotReady as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        # Ошибки площадки могут содержать секреты, не отправляем их в браузер.
        raise HTTPException(502, 'Не удалось получить стикер. Загрузка будет повторена автоматически.') from exc
    filename = assembly_stickers.filename(account, order_id)
    return Response(pdf, media_type='application/pdf', headers={
        'Content-Disposition': f'attachment; filename="{filename}"', 'Cache-Control': 'no-store',
    })


@router.get("/api/pack/label/{code}/{order_id}.pdf")
def api_label(code: str, order_id: str, with_products: bool = False, user: dict = Depends(require_section("pack"))):
    """Наклейка одного заказа на печать — из того кабинета, где заказ лежит.

    Кабинет в шапке тут ни при чём: сборщик печатает наклейку того заказа, что
    у него открыт, а открыть он мог заказ любого магазина.
    """
    shop = core_packing.shop_of(code, order_id, user)
    board = core_board.board_of(shop) if shop else None
    if board is None:
        raise HTTPException(status_code=404, detail=f"Заказ {order_id} не найден ни в одном кабинете")
    if core_board.not_ready(shop, [order_id]):
        raise HTTPException(
            status_code=400,
            detail=f"Заказ {order_id} ещё «Ожидает сборки» — наклейки печатаются с «Ожидает отгрузки»",
        )
    # Та же функция площадки, что печатает наклейки в «Заказах»: одна на всё.
    try:
        pdf, filename = board.labels(shop, user, [order_id])
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MarketError as exc:
        raise HTTPException(status_code=502, detail=f"Площадка не отдала наклейку: {exc.message}") from exc
    from ..core import sticker_archive

    sticker_archive.remember_batch(shop, [order_id], pdf)
    if with_products:
        from ..core import product_labels

        try:
            pdf = product_labels.prepend(shop, order_id, pdf)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return pdf_response(pdf, filename)


@router.post("/api/pack/labels.zip")
def api_labels(request: Request, user: dict = Depends(require_section("pack"))):
    """Совместимость старых ссылок на ZIP. Выгрузка больше не блокирует сборку."""
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
