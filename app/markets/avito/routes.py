"""Раздел «Заказы Avito»: подтверждение, отправка, этикетки и возвраты.

Сборщику нужны ровно два действия — «Подтвердите заказ» и «Отправьте заказ».
Остальные возможности Avito (отмена, маркировка «Честный знак», трек-номера,
интервалы курьера, споры) в интерфейс не выводятся: лишняя кнопка на складе —
это лишняя ошибка. В возвратах то же правило: показываем только те, что уже
лежат в пункте выдачи и которые можно забрать.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from . import client as avito, pack as avito_pack
from ...core import db, labels, return_acts
from .client import AvitoError
from ...core.deps import check_csrf, require_manager, require_market, require_section, safe_filename, templates
from ..base import NavItem, Workspace
from ...core import store as core_store
from ...core import sync as core_sync
from . import store, sync

log = logging.getLogger("avito")

router = APIRouter()

# Вкладки соответствуют задачам сборщика; всё остальное в панель не попадает.
#
# «Собранные» отделены от «Отправьте заказ» так же, как в Ozon: площадка держит
# их в одном статусе (собрали мы у себя, Avito об этом не знает), и без деления
# собранное лежало бы вперемешку с несобранным — по списку не видно, сколько
# работы осталось.
TABS = {
    "confirm": ("Подтвердите заказ", avito.STATUS_ON_CONFIRMATION, ""),
    "ship": ("Отправьте заказ", avito.STATUS_READY_TO_SHIP, "local_state != 'packed'"),
    "packed": ("Собранные", avito.STATUS_READY_TO_SHIP, "local_state = 'packed'"),
}


def _list_orders(account: dict, tab: str, search: str = "", limit: int = 300) -> list[dict]:
    _title, status, extra = TABS.get(tab, TABS["confirm"])
    params: list = [account["id"], status]
    sql = "SELECT * FROM avito_orders WHERE account_id = ? AND status = ?"
    if extra:
        sql += f" AND {extra}"
    if search:
        like = f"%{search.strip()}%"
        sql += """
            AND (id LIKE ? OR marketplace_id LIKE ? OR tracking_number LIKE ? OR buyer_name LIKE ?
                 OR EXISTS (SELECT 1 FROM avito_order_items i
                            WHERE i.account_id = avito_orders.account_id AND i.order_id = avito_orders.id
                            AND (i.title LIKE ? OR i.avito_id LIKE ? OR i.seller_id LIKE ?)))
        """
        params += [like] * 7
    # Сначала то, что горит: срок подтверждения или отправки.
    deadline = "confirm_till" if status == avito.STATUS_ON_CONFIRMATION else "ship_till"
    sql += f" ORDER BY ({deadline} IS NULL), {deadline} LIMIT ?"
    params.append(limit)
    return [store.avito_view(row) for row in db.query(sql, params)]


def _counts(account: dict) -> dict:
    return {
        key: db.query_one(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = ?"
            + (f" AND {extra}" if extra else ""),
            (account["id"], status),
        )["c"]
        for key, (_title, status, extra) in TABS.items()
    }


@router.get("/avito", response_class=HTMLResponse)
def avito_page(request: Request, tab: str = "confirm", q: str = "", user: dict = Depends(require_section("orders")),
               account: dict = Depends(require_market("avito"))):
    if tab not in TABS:
        tab = "confirm"
    return templates.TemplateResponse(
        request,
        "avito/orders.html",
        {
            "request": request,
            "user": user,
            "account": account,
            "tab": tab,
            "tabs": TABS,
            "counts": _counts(account),
            "orders": _list_orders(account, tab, q),
            "search": q,
            "sync": core_sync.status(),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "avito",
        },
    )


# ------------------------------------------------------------------ сборка заказа
def _pack_counters(account: dict) -> dict:
    aid = (account["id"],)

    def count(sql: str, params=aid) -> int:
        row = db.query_one(sql, params)
        return row["c"] if row else 0

    return {
        "to_pack": count(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = ? "
            "AND local_state != 'packed'", (account["id"], avito.STATUS_READY_TO_SHIP)),
        "packed_today": count(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND local_state = 'packed' "
            "AND packed_at >= date('now')"),
        "confirm": count(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = ?",
            (account["id"], avito.STATUS_ON_CONFIRMATION)),
    }


@router.get("/api/avito/pack/state")
def api_avito_pack_state(user: dict = Depends(require_section("pack")),
                         account: dict = Depends(require_market("avito"))):
    return {
        "state": avito_pack.load_state(account, user),
        "counters": _pack_counters(account),
        "labels": labels.state(avito_pack.pending_labels(account["id"])),
    }


@router.post("/api/avito/pack/scan")
def api_avito_pack_scan(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("pack")),
                        account: dict = Depends(require_market("avito"))):
    check_csrf(request)
    result = avito_pack.scan(account, user, str(payload.get("code") or ""))
    result["counters"] = _pack_counters(account)
    return result


@router.post("/api/avito/pack/release")
def api_avito_pack_release(request: Request, user: dict = Depends(require_section("pack")),
                           account: dict = Depends(require_market("avito"))):
    check_csrf(request)
    result = avito_pack.release(account, user)
    result["counters"] = _pack_counters(account)
    return result


@router.post("/api/avito/pack/open")
def api_avito_pack_open(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("pack")),
                        account: dict = Depends(require_market("avito"))):
    """Открыть сборку без сканера — если стикер не читается."""
    check_csrf(request)
    order = avito_pack.find_order(account["id"], str(payload.get("order_id") or ""))
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    result = avito_pack.open_order(account, user, order)
    result["counters"] = _pack_counters(account)
    return result


@router.get("/api/avito/orders")
def api_avito_orders(tab: str = "confirm", q: str = "", user: dict = Depends(require_section("orders")),
                     account: dict = Depends(require_market("avito"))):
    return {"orders": _list_orders(account, tab, q), "counts": _counts(account)}


def _order_row(account: dict, order_id: str) -> dict:
    row = db.query_one(
        "SELECT * FROM avito_orders WHERE account_id = ? AND id = ?", (account["id"], order_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Заказ {order_id} не найден в этом кабинете")
    return dict(row)


def _forget(account: dict, order_id: str) -> None:
    """Убрать заказ из панели: он вышел из рабочих статусов."""
    with db.write() as conn:
        conn.execute(
            "DELETE FROM avito_order_items WHERE account_id = ? AND order_id = ?", (account["id"], order_id)
        )
        conn.execute("DELETE FROM avito_orders WHERE account_id = ? AND id = ?", (account["id"], order_id))


def _refresh(account: dict, order_id: str) -> str | None:
    """Перечитать заказ у Avito после действия.

    Иначе в панели останется прежний список availableActions, и у только что
    подтверждённого заказа будет висеть кнопка «Подтвердить заказ».
    Возвращает актуальный статус или None, если заказ ушёл из рабочих.
    """
    try:
        raw = avito.get_client(account).order(order_id)
    except AvitoError as exc:
        log.warning("Заказ %s не перечитан: %s", order_id, exc)
        return None
    if not raw:
        _forget(account, order_id)
        return None
    if raw.get("status") not in avito.WORK_STATUSES:
        _forget(account, order_id)
        return raw.get("status")
    with db.write() as conn:
        store.upsert_avito_order(conn, account["id"], raw)
    return raw.get("status")


def _apply(account: dict, user: dict, order_id: str, transition: str) -> dict:
    """Один переход заказа + обновление локальной копии по ответу Avito."""
    order = _order_row(account, order_id)
    number = order.get("marketplace_id") or order_id
    client = avito.get_client(account)
    try:
        client.apply_transition(order_id, transition)
    except AvitoError as exc:
        db.log_event(
            "avito_error", level="error", account_id=account["id"], user=user,
            posting_number=number, message=f"{transition}: {exc}",
        )
        return {"status": "error", "order_id": order_id, "message": f"Avito отклонил действие: {exc.message}"}

    now = db.now_iso()
    if transition == avito.TRANSITION_CONFIRM:
        db.execute(
            "UPDATE avito_orders SET status = ?, confirmed_at = ?, confirmed_by = ?, updated_at = ? "
            "WHERE account_id = ? AND id = ?",
            (avito.STATUS_READY_TO_SHIP, now, user["login"], now, account["id"], order_id),
        )
        kind, text = "avito_confirm", "Заказ подтверждён"
    else:
        db.execute(
            "UPDATE avito_orders SET status = ?, shipped_at = ?, shipped_by = ?, updated_at = ? "
            "WHERE account_id = ? AND id = ?",
            (avito.STATUS_IN_TRANSIT, now, user["login"], now, account["id"], order_id),
        )
        kind, text = "avito_ship", "Отправка заказа подтверждена"
    # Локальную копию приводим к тому, что теперь говорит площадка.
    _refresh(account, order_id)
    db.log_event(kind, account_id=account["id"], user=user, posting_number=number, message=text)
    return {"status": "ok", "order_id": order_id, "message": f"{number}: {text.lower()}"}


@router.post("/api/avito/confirm")
def api_avito_confirm(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("orders")),
                      account: dict = Depends(require_market("avito"))):
    """«Подтвердите заказ» — переход confirm в Avito."""
    check_csrf(request)
    return _bulk(account, user, payload, avito.TRANSITION_CONFIRM, "подтверждено")


@router.post("/api/avito/ship")
def api_avito_ship(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("orders")),
                   account: dict = Depends(require_market("avito"))):
    """«Отправьте заказ» — переход perform. Доступен для доставки курьером продавца."""
    check_csrf(request)
    return _bulk(account, user, payload, avito.TRANSITION_PERFORM, "отправлено")


def _bulk(account: dict, user: dict, payload: dict, transition: str, verb: str) -> dict:
    ids = [str(i) for i in (payload.get("order_ids") or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного заказа")
    results = [_apply(account, user, order_id, transition) for order_id in ids]
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    message = f"{verb.capitalize()}: {len(ok)}"
    if failed:
        message += f", с ошибкой: {len(failed)} — {failed[0]['message']}"
    return {
        "status": "ok" if not failed else ("warning" if ok else "error"),
        "message": message,
        "results": results,
        "done": [r["order_id"] for r in ok],
    }


@router.post("/api/avito/orders/{order_id}/reset")
def api_avito_reset_order(order_id: str, request: Request, admin: dict = Depends(require_manager),
                          account: dict = Depends(require_market("avito"))):
    """Снять отметку «собрано» — например, если сборку закрыли по ошибке.

    Только админу и владельцу: отметка — это результат работы сборщика, и
    снимать её должен тот, кто отвечает за склад, а не тот, кто ошибся.
    """
    check_csrf(request)
    order = _order_row(account, order_id)
    db.execute(
        "UPDATE avito_orders SET local_state = 'new', packed_at = NULL, packed_by = NULL, "
        "claim_user_id = NULL, claim_login = NULL, claim_at = NULL "
        "WHERE account_id = ? AND id = ?",
        (account["id"], order_id),
    )
    db.log_event(
        "avito_order_reset", level="warn", account_id=account["id"], user=admin,
        posting_number=order.get("marketplace_id") or order_id,
        message="Сброшена отметка сборки",
    )
    return {"status": "ok", "message": f"{order.get('marketplace_id') or order_id}: отметка сборки снята"}


@router.post("/api/avito/labels/archive.zip")
def api_avito_labels_archive(request: Request, user: dict = Depends(require_section("pack")),
                             account: dict = Depends(require_market("avito"))):
    """Этикетки всех заказов, ждущих выгрузки, — архивом на компьютер.

    Сам файл панель не хранит: архив уезжает в браузер, на диске сервера ничего
    не остаётся. В базе только отметка о выгрузке, по ней открывается сборка.
    """
    check_csrf(request)
    ids = avito_pack.pending_labels(account["id"])
    if not ids:
        raise HTTPException(status_code=400, detail="Все этикетки уже выгружены")
    ids = ids[: labels.MAX_AT_ONCE]
    # Avito выдаёт этикетку по номеру из сервиса сделок, а помечаем свой id.
    rows = {row["id"]: (row["marketplace_id"] or row["id"]) for row in db.query(
        f"SELECT id, marketplace_id FROM avito_orders WHERE account_id = ? "
        f"AND id IN ({','.join('?' for _ in ids)})", [account["id"]] + ids)}

    def fetch(batch: list[str]) -> bytes:
        return avito.get_client(account).label_pdf([rows.get(i, i) for i in batch])[0]

    archive, saved = labels.build_archive(ids, fetch, prefix="этикетки")
    if not saved:
        raise HTTPException(status_code=502, detail="Avito не отдал ни одной этикетки")
    labels.mark_saved("avito_orders", account["id"], saved, "id")
    db.log_event(
        "avito_labels_archive", account_id=account["id"], user=user,
        message=f"Выгружены этикетки: {len(saved)} шт.",
    )
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{_avito_archive_name(account)}"',
                 "Cache-Control": "no-store"},
    )


def _avito_archive_name(account: dict) -> str:
    stamp = core_store.local_time(db.now_iso(), "%Y-%m-%d_%H-%M")
    return safe_filename(f"avito-labels-{account.get('title') or account['id']}-{stamp}.zip")


@router.get("/api/avito/label/{order_id}.pdf")
def api_avito_label(order_id: str, user: dict = Depends(require_section("orders")),
                    account: dict = Depends(require_market("avito"))):
    """Оригинальный PDF-файл этикетки от Avito — без нашего редактирования."""
    order = _order_row(account, order_id)
    return _label_response(account, user, [order])


@router.post("/api/avito/labels.pdf")
def api_avito_labels(request: Request, payload: dict = Body(...), user: dict = Depends(require_section("orders")),
                     account: dict = Depends(require_market("avito"))):
    """Пачка этикеток: Avito принимает до 50 номеров за раз."""
    check_csrf(request)
    ids = [str(i) for i in (payload.get("order_ids") or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного заказа")
    if len(ids) > 50:
        raise HTTPException(status_code=400, detail="За один раз Avito печатает не больше 50 этикеток")
    return _label_response(account, user, [_order_row(account, order_id) for order_id in ids])


def _label_response(account: dict, user: dict, orders: list[dict]) -> Response:
    # Этикетки Avito запрашиваются по номеру из сервиса сделок (marketplaceId).
    numbers = [order.get("marketplace_id") or order["id"] for order in orders]
    try:
        pdf, filename = avito.get_client(account).label_pdf(numbers)
    except AvitoError as exc:
        raise HTTPException(status_code=502, detail=f"Avito не отдал этикетку: {exc.message}") from exc
    now = db.now_iso()
    with db.write() as conn:
        for order in orders:
            conn.execute(
                "UPDATE avito_orders SET printed_at = ?, print_count = print_count + 1 "
                "WHERE account_id = ? AND id = ?",
                (now, account["id"], order["id"]),
            )
            db.log_event(
                "avito_label_print", account_id=account["id"], user=user,
                posting_number=order.get("marketplace_id") or order["id"],
                message="Этикетка отправлена на печать", conn=conn,
            )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename(filename)}"',
                 "Cache-Control": "no-store"},
    )


@router.post("/api/avito/sync")
def api_avito_sync(request: Request, user: dict = Depends(require_section("orders")),
                   account: dict = Depends(require_market("avito"))):
    check_csrf(request)
    try:
        result = sync.sync_avito(account)
    except AvitoError as exc:
        raise HTTPException(status_code=502, detail=f"Avito недоступен: {exc.message}") from exc
    return {
        "status": "ok",
        "message": f"Загружено заказов: {result.get('avito', 0)}",
        "result": result,
        "counts": _counts(account),
    }


# ================================================================== возвраты
# «Возврат: заберите заказ» — заказ вернулся и лежит в пункте выдачи. Только
# такие возвраты панель и хранит: пока посылка едет обратно, забирать нечего.
# Забирать вручную ничего не отмечают: как только возврат получен, Avito
# переводит заказ дальше, и ближайшая синхронизация убирает его из панели.
@router.get("/api/avito/returns/{order_id}/raw")
def api_avito_return_raw(order_id: str, request: Request, admin: dict = Depends(require_manager),
                         account: dict = Depends(require_market("avito"))):
    """Ответ Avito по возврату как есть — чтобы видеть, что площадка реально прислала.

    Нужен, когда чего-то не хватает на экране: например, Avito не отдал адрес ПВЗ.
    """
    row = _order_row(account, order_id)
    try:
        raw = json.loads(row.get("raw") or "{}")
    except ValueError:
        raw = {}
    return {
        "order_id": order_id,
        "pickup_address": avito.pickup_address(raw),
        "pickup_code": avito.pickup_code(raw),
        "raw": raw,
    }


# ------------------------------------------------------------------ для реестра площадок
# Рабочее место сборщика: страница одна на все площадки, слова — свои.
WORKSPACE = Workspace(
    placeholder="Сканируйте стикер отправления, затем штрихкоды товаров…",
    banner="Отсканируйте стикер отправления — откроется сборка заказа.",
    gate_title="Скачайте этикетки",
    download="Скачать этикетки",
    gate_template="avito/pack_gate.html",
    url="/avito/pack",
    tab="avito_pack",
    load_state=avito_pack.load_state,
    count_queue=lambda account: _pack_counters(account),
    counters=(
        ("c-to-pack", "to_pack", "К сборке", ""),
        ("c-packed", "packed_today", "Собрано сегодня", "ok"),
        ("c-confirm", "confirm", "Ждут подтверждения", ""),
    ),
)

def _count(sql: str, params: tuple) -> int:
    row = db.query_one(sql, params)
    return row["c"] if row else 0


def nav_items(account: dict) -> list[NavItem]:
    """Меню кабинета Avito. Собранные не в счёт, как и у Ozon: значок — сколько дел осталось."""
    aid = (account["id"],)
    return [
        NavItem("/avito/pack", "Сборка", "avito_pack", "pack"),
        NavItem("/avito?tab=confirm", "Заказы Avito", "avito", "orders", (
            (_count("SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = 'on_confirmation'", aid),
             "warn", "Подтвердите заказ"),
            (_count("SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? "
                    "AND status = 'ready_to_ship' AND local_state != 'packed'", aid),
             "accent", "Отправьте заказ"),
        )),
        # Раздел возвратов один на все площадки — адрес общий, счётчики свои.
        # В таблице лежат только возвраты, готовые к выдаче, — фильтровать ещё
        # и по return_status незачем: написание значения у Avito плавает.
        # Полученные (пропавшие из выдачи) в счётчик не идут: забирать нечего,
        # они ждут акта.
        NavItem("/returns", "Возвраты", "returns", "returns", (
            (_count("SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? "
                    "AND status = 'on_return' AND received_at IS NULL", aid),
             "", "Заберите заказ"),
            (return_acts.pending_count([account["id"]]), "warn", "Акты ждут подтверждения"),
        )),
    ]


def settings_stats(account_id: int) -> dict[str, int]:
    aid = (account_id,)
    return {
        "Ждут подтверждения": _count(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = 'on_confirmation'", aid),
        "Ждут отправки": _count(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = 'ready_to_ship'", aid),
        "Позиций в заказах": _count("SELECT COUNT(*) AS c FROM avito_order_items WHERE account_id = ?", aid),
    }
