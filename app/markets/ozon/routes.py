"""Разделы Ozon: рабочее место сборщика и «Заказы FBS».

Сборка: сканирование, печать стикеров, завершение. Заказы: списки по
вкладкам, сборка на стороне Ozon, печать стикеров пачкой.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ...core import db, return_acts, sync
from ...core import printers as core_printers
from ...core import store as core_store
from ...core.deps import check_csrf, require_manager, require_market, require_section, safe_filename, templates
from ..base import NavItem, Workspace
from . import pack as packing
from . import returns, store
from .client import OzonError

router = APIRouter()

# Сколько стикеров печатается одним запросом.
MAX_LABELS = 50


def _counters(account: dict) -> dict:
    account_id = account["id"]
    # «Возвраты к выдаче» — прямо по отмеченным статусам, см. returns.pickup_sql.
    ready_sql, ready_params = returns.pickup_sql()
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


@router.get("/api/label/{posting_number}.pdf")
def api_label(posting_number: str, user: dict = Depends(require_section("pack")),
              account: dict = Depends(require_market("ozon"))):
    try:
        pdf, filename = packing.label_pdf(account, user, [posting_number])
    except OzonError as exc:
        raise HTTPException(status_code=502, detail=f"Ozon не отдал стикер: {exc.message}") from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename(filename)}"',
                 "Cache-Control": "no-store",
                 # Размер листа — по нему браузер выбирает принтер (см. «Принтеры»).
                 **core_printers.size_header(pdf)},
    )


def _archive_name(account: dict) -> str:
    """Имя архива: по нему на компьютере видно, чьи это стикеры и за когда."""
    stamp = core_store.local_time(db.now_iso(), "%Y-%m-%d_%H-%M")
    return safe_filename(f"stickers-{account.get('title') or account['id']}-{stamp}.zip")


@router.post("/api/labels.pdf")
def api_labels(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("pack")),
               account: dict = Depends(require_market("ozon"))):
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
                 "Cache-Control": "no-store",
                 # Размер листа — по нему браузер выбирает принтер (см. «Принтеры»).
                 **core_printers.size_header(pdf)},
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
                account: dict = Depends(require_market("ozon"))):
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
        "ozon/orders.html",
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
               account: dict = Depends(require_market("ozon"))):
    return {"postings": _list_postings(account, tab, q)}


@router.post("/api/ship")
def api_ship(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("orders")),
             account: dict = Depends(require_market("ozon"))):
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
             account: dict = Depends(require_market("ozon"))):
    """Обновить данные текущего кабинета по кнопке."""
    check_csrf(request)
    try:
        result = sync.run_once(returns=True, account=account)
    except Exception as exc:  # noqa: BLE001 - показываем причину оператору
        raise HTTPException(status_code=502, detail=f"Синхронизация не удалась: {exc}") from exc
    return {"status": "ok", "message": "Данные обновлены", "result": result, "sync": sync.status()}


# ------------------------------------------------------------------ для реестра площадок
# Рабочее место сборщика: страница одна на все площадки, слова — свои.
WORKSPACE = Workspace(
    placeholder="Сканируйте штрихкод товара или стикер отправления…",
    banner="Отсканируйте штрихкод товара — система сама найдёт отправление и отправит стикер на печать.",
    url="/pack",
    tab="pack",
    load_state=packing.load_state,
    count_queue=lambda account: _counters(account),
    counters=(
        ("c-packaging", "awaiting_packaging", "Ожидает сборки", ""),
        ("c-deliver", "awaiting_deliver", "Ожидает отгрузки", ""),
        ("c-packed", "packed_today", "Собрано сегодня", "ok"),
        ("c-returns", "returns_ready", "Возвраты к выдаче", ""),
    ),
    owner=packing.owner,
    label=lambda account, user, number: packing.label_pdf(account, user, [number]),
    scan=packing.scan,
    release=packing.release,
    complete=packing.complete_active,
)

def _count(sql: str, params: tuple) -> int:
    row = db.query_one(sql, params)
    return row["c"] if row else 0


def nav_items(account: dict) -> list[NavItem]:
    """Меню кабинета Ozon. Значки — сколько работы осталось, собранное не в счёт."""
    aid = (account["id"],)
    ready_sql, ready_params = returns.pickup_sql()
    return [
        NavItem("/pack", "Сборка", "pack", "pack"),
        NavItem("/orders?tab=packaging", "Заказы FBS", "orders", "orders", (
            (_count("SELECT COUNT(*) AS c FROM postings WHERE account_id = ? AND status = 'awaiting_packaging'", aid),
             "warn", "Ожидает сборки"),
            (_count("SELECT COUNT(*) AS c FROM postings WHERE account_id = ? AND status = 'awaiting_deliver' "
                    "AND local_state = 'new'", aid),
             "accent", "Ожидает отгрузки"),
        )),
        # Раздел возвратов один на все площадки — адрес общий, счётчики свои.
        NavItem("/returns", "Возвраты", "returns", "returns", (
            (_count(f"SELECT COUNT(*) AS c FROM returns WHERE account_id = ? AND {ready_sql}",
                    (account["id"], *ready_params)),
             "", "К выдаче"),
            (return_acts.pending_count([account["id"]]), "warn", "Акты ждут подтверждения"),
        )),
    ]


def settings_stats(account_id: int) -> dict[str, int]:
    """Плитки кабинета в «Настройках»."""
    aid = (account_id,)
    return {
        "Отправлений": _count("SELECT COUNT(*) AS c FROM postings WHERE account_id = ?", aid),
        "Собрано": _count("SELECT COUNT(*) AS c FROM postings WHERE account_id = ? AND local_state = 'packed'", aid),
        "Товаров": _count("SELECT COUNT(*) AS c FROM products WHERE account_id = ?", aid),
        "Штрихкодов": _count("SELECT COUNT(*) AS c FROM product_barcodes WHERE account_id = ?", aid),
        "Возвратов": _count("SELECT COUNT(*) AS c FROM returns WHERE account_id = ?", aid),
    }


@router.post("/api/postings/{posting_number}/reset")
def api_reset_posting(posting_number: str, request: Request, admin: dict = Depends(require_manager),
                      account: dict = Depends(require_market("ozon"))):
    """Снять отметку «собрано» — например, если сборку закрыли по ошибке."""
    check_csrf(request)
    row = db.query_one(
        "SELECT * FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting_number)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Отправление не найдено")
    state = "cancelled" if row["status"] == "cancelled" else "new"
    db.execute(
        "UPDATE postings SET local_state = ?, packed_at = NULL, packed_by = NULL,"
        " claim_user_id = NULL, claim_login = NULL, claim_at = NULL WHERE account_id = ? AND posting_number = ?",
        (state, account["id"], posting_number),
    )
    db.log_event(
        "posting_reset", level="warn", account_id=account["id"], user=admin,
        posting_number=posting_number, message="Сброшена отметка сборки",
    )
    return {"status": "ok", "message": f"{posting_number}: отметка сборки снята"}


@router.post("/api/returns/statuses")
def api_returns_statuses(request: Request, payload: dict = Body(...), admin: dict = Depends(require_manager),
                         account: dict = Depends(require_market("ozon"))):
    """Какие статусы возвратов панель загружает и показывает как доступные."""
    check_csrf(request)
    raw = payload.get("statuses") or []
    known = {code for code, _label, _hint in returns.RETURN_STATUS_CHOICES}
    statuses = [str(s).strip() for s in raw if str(s).strip() in known]
    if not statuses:
        raise HTTPException(status_code=400, detail="Выберите хотя бы один статус")

    returns.set_returns_statuses(statuses, user=admin)
    try:
        result = returns.sync_returns(account)
    except Exception as exc:  # noqa: BLE001 - причину показываем оператору
        raise HTTPException(status_code=502, detail=f"Статусы сохранены, но обновить возвраты не удалось: {exc}") from exc
    names = ", ".join(returns.status_label(code) for code in statuses)
    return {
        "status": "ok",
        "message": f"Загружаются возвраты в статусах: {names}. Обновлено: {result.get('returns', 0)}.",
        "result": result,
    }


@router.post("/api/returns/received-statuses")
def api_received_statuses(request: Request, payload: dict = Body(...), admin: dict = Depends(require_manager),
                          account: dict = Depends(require_market("ozon"))):
    """В каких статусах возврат считается полученным — из них собирается акт.

    Пустой список разрешён: это «акты не вести». Отказывать здесь, как в списке
    к выдаче, нельзя — иначе выключить акты было бы невозможно.
    """
    check_csrf(request)
    raw = payload.get("statuses") or []
    known = {code for code, _label, _hint in returns.RETURN_STATUS_CHOICES}
    unknown = [str(s).strip() for s in raw if str(s).strip() not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Неизвестный статус: {', '.join(unknown)}")
    statuses = [str(s).strip() for s in raw if str(s).strip()]

    # Глубина окна сохраняется вместе со статусами: обе настройки про одно и то
    # же — что панель считает полученным и за какой срок это забирает.
    days = payload.get("days")
    if days is not None:
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Срок указывается числом дней") from None
        returns.set_received_days(days, user=admin)

    returns.set_received_statuses(statuses, user=admin)
    try:
        result = returns.sync_returns(account)
    except Exception as exc:  # noqa: BLE001 - причину показываем оператору
        raise HTTPException(status_code=502, detail=f"Статусы сохранены, но обновить возвраты не удалось: {exc}") from exc
    if not statuses:
        return {"status": "ok", "result": result,
                "message": "Акты больше не из чего составлять: полученные возвраты не загружаются."}
    names = ", ".join(returns.status_label(code) for code in statuses)
    return {
        "status": "ok",
        "message": f"Полученными считаются возвраты в статусах: {names}. "
                   f"Обновлено возвратов: {result.get('returns', 0)}.",
        "result": result,
    }
