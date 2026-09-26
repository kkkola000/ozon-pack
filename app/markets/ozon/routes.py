"""Разделы Ozon: рабочее место сборщика и его часть общих «Заказов».

Сборка: сканирование, печать стикеров, завершение. Заказы: какие отправления
в каком статусе склада, сборка на стороне Ozon, стикеры пачкой, снятие
отметки. Сам раздел «Заказы» общий — его рисует ядро.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response

from ...core import db, return_acts
from ...core import printers as core_printers
from ...core.deps import check_csrf, require_manager, require_market, require_section, safe_filename
from ..base import NavItem, OrderAction, OrdersBoard, Workspace
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


# ================================================================== заказы FBS
# Сам раздел «Заказы» общий на все площадки (core/board.py, routes/orders.py).
# Здесь то, что в нём значит Ozon: какие отправления в каком статусе склада и
# что с ними можно сделать. Кабинет каждый раз приходит от ядра — тот, чей это
# заказ, а не тот, что выбран в шапке.
def _known(account: dict, numbers: list[str]) -> None:
    """Все номера — отправления этого кабинета. Чужие не трогаем."""
    for number in numbers:
        if not db.query_one(
            "SELECT 1 FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], number)
        ):
            raise LookupError(f"Отправление {number} не найдено в кабинете «{account['title']}»")


def ship_many(account: dict, user: dict, numbers: list[str]) -> dict:
    """«Собрать в Ozon»: перевести отправления в «Ожидает отгрузки»."""
    _known(account, numbers)
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


def print_labels(account: dict, user: dict, numbers: list[str]) -> tuple[bytes, str]:
    """Стикеры отправлений на печать — файл Ozon как есть."""
    _known(account, numbers)
    return packing.label_pdf(account, user, numbers)


def reset_mark(account: dict, user: dict, number: str) -> str:
    """Снять отметку «собрано» — например, если сборку закрыли по ошибке."""
    row = db.query_one(
        "SELECT * FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], number)
    )
    if not row:
        raise LookupError(f"Отправление {number} не найдено в кабинете «{account['title']}»")
    state = "cancelled" if row["status"] == "cancelled" else "new"
    db.execute(
        "UPDATE postings SET local_state = ?, packed_at = NULL, packed_by = NULL,"
        " claim_user_id = NULL, claim_login = NULL, claim_at = NULL WHERE account_id = ? AND posting_number = ?",
        (state, account["id"], number),
    )
    db.log_event(
        "posting_reset", level="warn", account_id=account["id"], user=user,
        posting_number=number, message="Сброшена отметка сборки",
    )
    return f"{number}: отметка сборки снята"


ORDERS = OrdersBoard(
    table="postings",
    status_sql=store.BOARD_STATUS_SQL,
    deadline_sql="o.shipment_date",
    search_sql=store.BOARD_SEARCH_SQL,
    card=store.board_card,
    label="Стикер",
    printed="Стикер печатался",
    labels=print_labels,
    reset=reset_mark,
    max_labels=MAX_LABELS,
    actions=(
        OrderAction(
            key="ship",
            title="Собрать в Ozon → «Ожидает отгрузки»",
            short="Собрать в Ozon",
            run=ship_many,
            ask="Собрать {n} отправл. в Ozon? Они перейдут в статус «Ожидает отгрузки».",
        ),
    ),
)


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
    ready_sql, ready_params = returns.pickup_sql()
    return [
        NavItem("/pack", "Сборка", "pack", "pack"),
        # «Заказов» здесь нет: пункт общий, со счётчиками по всем кабинетам, —
        # его ставит ядро (core/deps.market_nav).
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


def _resync_returns() -> dict:
    """Перечитать возвраты всех кабинетов Ozon: настройка статусов общая на панель."""
    from ...core import shops

    total, errors = 0, []
    for account in shops.live(lambda market: market.code == "ozon"):
        try:
            total += int(returns.sync_returns(account).get("returns", 0))
        except Exception as exc:  # noqa: BLE001 - отказ одного кабинета не отменяет остальных
            errors.append(f"{account['title']}: {exc}")
    if errors and not total:
        raise RuntimeError("; ".join(errors))
    return {"returns": total, "errors": errors}


@router.post("/api/returns/statuses")
def api_returns_statuses(request: Request, payload: dict = Body(...), admin: dict = Depends(require_manager)):
    """Какие статусы возвратов панель загружает и показывает как доступные."""
    check_csrf(request)
    raw = payload.get("statuses") or []
    known = {code for code, _label, _hint in returns.RETURN_STATUS_CHOICES}
    statuses = [str(s).strip() for s in raw if str(s).strip() in known]
    if not statuses:
        raise HTTPException(status_code=400, detail="Выберите хотя бы один статус")

    returns.set_returns_statuses(statuses, user=admin)
    try:
        result = _resync_returns()
    except Exception as exc:  # noqa: BLE001 - причину показываем оператору
        raise HTTPException(status_code=502, detail=f"Статусы сохранены, но обновить возвраты не удалось: {exc}") from exc
    names = ", ".join(returns.status_label(code) for code in statuses)
    return {
        "status": "ok",
        "message": f"Загружаются возвраты в статусах: {names}. Обновлено: {result.get('returns', 0)}.",
        "result": result,
    }


@router.post("/api/returns/received-statuses")
def api_received_statuses(request: Request, payload: dict = Body(...), admin: dict = Depends(require_manager)):
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
        result = _resync_returns()
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
