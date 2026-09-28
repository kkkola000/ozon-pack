"""Возвраты Яндекс Маркета: невыкупы и возвраты покупателей в пункте выдачи.

Метод один — GET /v2/campaigns/{campaignId}/returns (getReturns), по магазину.
Статусов у панели два, и они фиксированные — логистические (shipmentStatus):

* READY_FOR_PICKUP — лежит в пункте и готов к выдаче магазину: это «К выдаче»,
  лист для поездки;
* PICKED — выдан магазину: получили, нужна отметка, из таких за число
  составляется акт — как у Ozon по статусу «Получен».

Вид (невыкуп или возврат покупателя) не фильтруем: в пункт приезжают оба.
Строка — возврат целиком, товары внутри: на посылке один номер возврата, по
нему её и выдают, и узнаёт сканер.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from ...core import db, linked, return_acts
from ...core import sync as core_sync
from ...core.codes import barcode_variants
from ...core.returns_pdf import barcode_svg, cut, mark_cell
from ...core.store import _DAY, _moment, _num, _raw_json, _text, _with_mark, local_day, local_time
from ..base import ReturnsSource
from . import client as yandex
from .client import YandexError

log = logging.getLogger("yandex.returns")

SCHEMA = """
CREATE TABLE IF NOT EXISTS yandex_returns (
    account_id      INTEGER NOT NULL,
    -- Номер возврата (id из getReturns): он напечатан на посылке, по нему её
    -- выдают в пункте и узнаёт сканер.
    id              TEXT NOT NULL,
    campaign_id     TEXT,
    order_id        TEXT,
    -- UNREDEEMED — невыкуп, RETURN — возврат покупателя.
    return_type     TEXT,
    -- Логистический статус: READY_FOR_PICKUP — в пункте, PICKED — выдан нам.
    shipment_status TEXT,
    refund_status   TEXT,
    place_name      TEXT,
    place_address   TEXT,
    -- pickupTillDate: до какого числа возврат можно забрать.
    pickup_till     TEXT,
    created_at_api  TEXT,
    updated_at_api  TEXT,
    amount          REAL,
    currency        TEXT,
    -- Товары возврата JSON-списком: [{"offer_id", "market_sku", "count"}].
    items           TEXT,
    items_count     INTEGER NOT NULL DEFAULT 0,
    -- Трек-номера через запятую и с запятыми по краям: «,T1,T2,» — чтобы
    -- искать номер целиком, а не кусок чужого.
    tracks          TEXT,
    raw             TEXT,
    printed_at      TEXT,
    -- Отметка, акт и день получения — панели: площадка о них не знает, и
    -- загрузка их не трогает (см. returns у Ozon).
    mark            TEXT,
    note            TEXT,
    mark_at         TEXT,
    mark_by         TEXT,
    act_id          TEXT,
    received_at     TEXT,
    received_day    TEXT,
    first_seen_at   TEXT,
    updated_at      TEXT,
    PRIMARY KEY (account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_yandex_returns_status ON yandex_returns(account_id, shipment_status);
CREATE INDEX IF NOT EXISTS idx_yandex_returns_received ON yandex_returns(account_id, received_day, act_id);
CREATE INDEX IF NOT EXISTS idx_yandex_returns_act ON yandex_returns(act_id);
"""

READY = yandex.RETURN_READY
PICKED = yandex.RETURN_PICKED
WANTED = (READY, PICKED)
# Полученные грузятся окном за последние дни: по статусу «выдан» Маркет отдал
# бы весь архив. «К выдаче» — без окна: возврат лежит в пункте неделями.
RECEIVED_DAYS = 14


def _moment_of(value) -> str | None:
    """Дата Маркета к ISO-8601 UTC: в возвратах она ISO со смещением, в заказах — «ДД-ММ-ГГГГ»."""
    from .store import _yandex_dt

    return _moment(value) or _moment(_yandex_dt(value))


def _address(point: dict) -> str | None:
    address = point.get("address") or {}
    if isinstance(address, str):
        return _text(address)
    parts = [address.get(key) for key in ("city", "street", "house", "houseNumber")]
    return ", ".join(str(part) for part in parts if part) or None


def upsert_return(conn: sqlite3.Connection, account_id: int, campaign_id: str, raw: dict) -> str:
    """Возврат из getReturns, не трогая отметок, акта и дня получения.

    День получения ставится один раз — когда панель впервые видит возврат
    выданным (PICKED). Момент — updateDate площадки, а не «сейчас»: первое
    обновление после выходных не должно объявить всё полученным сегодня. Потом
    число не двигается: updateDate меняется и от решения по деньгам, а к
    поездке в пункт это отношения не имеет.
    """
    return_id = _text(raw.get("id"))
    if not return_id:
        raise ValueError("В ответе Маркета нет id возврата")
    status = _text(raw.get("shipmentStatus")) or ""
    point = raw.get("logisticPickupPoint") or {}
    amount = raw.get("amount") or {}
    items = []
    tracks: list[str] = []
    for item in raw.get("items") or []:
        items.append({
            "offer_id": _text(item.get("shopSku")),
            "market_sku": _text(item.get("marketSku")),
            "count": max(1, int(item.get("count") or 1)),
        })
        tracks += [str(track.get("trackCode")) for track in item.get("tracks") or [] if track.get("trackCode")]
    tracks = list(dict.fromkeys(tracks))

    now = db.now_iso()
    received_at = None
    if status == PICKED:
        received_at = _moment_of(raw.get("updateDate")) or now
        received_at = min(received_at, now)
    received_day = local_day(received_at) if received_at else None
    if received_at and not _DAY.fullmatch(received_day or ""):
        received_day = local_day(now)

    existing = conn.execute(
        "SELECT first_seen_at FROM yandex_returns WHERE account_id = ? AND id = ?", (account_id, return_id)
    ).fetchone()
    conn.execute(
        """
        INSERT INTO yandex_returns (
            account_id, id, campaign_id, order_id, return_type, shipment_status, refund_status,
            place_name, place_address, pickup_till, created_at_api, updated_at_api, amount, currency,
            items, items_count, tracks, raw, received_at, received_day, first_seen_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, id) DO UPDATE SET
            campaign_id = excluded.campaign_id, order_id = excluded.order_id,
            return_type = excluded.return_type, shipment_status = excluded.shipment_status,
            refund_status = excluded.refund_status, place_name = excluded.place_name,
            place_address = excluded.place_address, pickup_till = excluded.pickup_till,
            created_at_api = excluded.created_at_api, updated_at_api = excluded.updated_at_api,
            amount = excluded.amount, currency = excluded.currency, items = excluded.items,
            items_count = excluded.items_count, tracks = excluded.tracks, raw = excluded.raw,
            updated_at = excluded.updated_at,
            received_at = COALESCE(yandex_returns.received_at, excluded.received_at),
            received_day = COALESCE(yandex_returns.received_day, excluded.received_day)
        """,
        (
            account_id, return_id, str(campaign_id), _text(raw.get("orderId")), _text(raw.get("returnType")),
            status or None, _text(raw.get("refundStatus")),
            _text(point.get("name")), _address(point), _moment_of(raw.get("pickupTillDate")),
            _moment_of(raw.get("creationDate")), _moment_of(raw.get("updateDate")),
            # Сумма — из amount: refundAmount Маркет отключает 12.10.2026.
            _num(amount.get("value")), _text(amount.get("currencyId") or amount.get("currency")),
            json.dumps(items, ensure_ascii=False), sum(item["count"] for item in items),
            ("," + ",".join(tracks) + ",") if tracks else None,
            _raw_json(raw), received_at, received_day,
            (existing["first_seen_at"] if existing else now) or now, now,
        ),
    )
    return return_id


def _goods(account_id: int, row: dict) -> list[dict]:
    """Товары возврата для экрана: название и фото из каталога, артикул — основной карточки."""
    goods = []
    for item in db.json_list(row.get("items")):
        if not isinstance(item, dict):
            continue
        offer = item.get("offer_id") or ""
        card = db.query_one("SELECT name, image FROM products WHERE account_id = ? AND sku = ?",
                            (account_id, offer)) if offer else None
        name = card["name"] if card and card["name"] else None
        if not name and offer:
            ordered = db.query_one("SELECT name FROM yandex_order_items WHERE account_id = ? AND offer_id = ? "
                                   "AND name IS NOT NULL LIMIT 1", (account_id, offer))
            name = ordered["name"] if ordered else None
        image = linked.extras(account_id, offer, image=card["image"] if card else None)[1] if offer else None
        goods.append({
            "offer_id": offer,
            "article": linked.article(account_id, offer, offer) if offer else None,
            "name": name or (f"Артикул {offer}" if offer else "Без названия"),
            "image": image,
            "count": int(item.get("count") or 1),
        })
    return goods


def return_view(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    data.pop("raw", None)
    data["goods"] = _goods(int(data["account_id"]), data)
    data["type_label"] = yandex.RETURN_TYPE_LABELS.get(data.get("return_type") or "", "Возврат")
    data["status_label"] = yandex.RETURN_STATUS_LABELS.get(data.get("shipment_status") or "", "—")
    data["track_list"] = [code for code in str(data.get("tracks") or "").split(",") if code]
    data["pickup_till_local"] = local_time(data.get("pickup_till"), "%d.%m.%Y") if data.get("pickup_till") else ""
    return _with_mark(data)


# ------------------------------------------------------------------ загрузка
def campaign_ids(account: dict, client) -> list[str]:
    """Магазины кабинета: из GET /v2/campaigns и те, что уже встречались в заказах.

    Метод магазинов может быть закрыт ключу — тогда хватит известных по заказам
    и прежним возвратам: возвраты без магазина не запросить вовсе.
    """
    found: list[str] = []
    try:
        found += [str(campaign.get("id")) for campaign in client.campaigns() if campaign.get("id")]
    except YandexError as exc:
        log.warning("Магазины кабинета Маркета недоступны: %s", exc)
    for table in ("yandex_orders", "yandex_returns"):
        found += [str(row["campaign_id"]) for row in db.query(
            f"SELECT DISTINCT campaign_id FROM {table} WHERE account_id = ? AND campaign_id IS NOT NULL",
            (account["id"],),
        )]
    return list(dict.fromkeys(campaign for campaign in found if campaign and campaign != "None"))


def _walk(client, campaign: str, status: str, store_page, **window) -> bool:
    """Пролистать возвраты магазина в статусе. False — список прочитан не до конца."""
    token: str | None = None
    for _page in range(core_sync.RETURNS_MAX_PAGES):
        try:
            page, token = client.returns(campaign, shipment_status=status, page_token=token, **window)
        except YandexError as exc:
            log.warning("Возвраты Маркета (%s, магазин %s) недоступны: %s", status, campaign, exc)
            return False
        store_page(campaign, page)
        if not token:
            return True
    log.warning("Возвраты Маркета (%s): упёрлись в потолок страниц", status)
    return False


def sync_returns(account: dict | None = None) -> dict:
    """«К выдаче» целиком и выданные за последние дни — по всем магазинам кабинета.

    Статус уходит в запрос, но на него не полагаемся: всё, что пришло в другом
    статусе, отбрасываем у себя.
    """
    account = core_sync._account(account)
    if account is None:
        return {"yandex_returns": 0}
    account_id = account["id"]
    client = yandex.get_client(account)
    campaigns = campaign_ids(account, client)
    seen: set[str] = set()
    counts = {READY: 0, PICKED: 0, "skipped": 0}

    def store_page(campaign: str, page: list[dict]) -> None:
        keep = [raw for raw in page if str(raw.get("shipmentStatus") or "") in WANTED]
        counts["skipped"] += len(page) - len(keep)
        if not keep:
            return
        with db.write() as conn:
            for raw in keep:
                seen.add(upsert_return(conn, account_id, campaign, raw))
                counts[str(raw.get("shipmentStatus"))] += 1

    ready_complete = bool(campaigns)
    today = datetime.now(timezone.utc).date()
    window = {"from_date": (today - timedelta(days=RECEIVED_DAYS)).isoformat(),
              "to_date": (today + timedelta(days=1)).isoformat()}
    for campaign in campaigns:
        ready_complete = _walk(client, campaign, READY, store_page) and ready_complete
        _walk(client, campaign, PICKED, store_page, **window)

    gone = _rebuild_ready(account_id, seen) if ready_complete else 0
    # Строки не в наших статусах и без работы — лишний кэш. С отметкой, в акте
    # или полученные — остаются: там работа сборщика.
    db.execute(
        "DELETE FROM yandex_returns WHERE account_id = ? "
        "AND (shipment_status IS NULL OR shipment_status NOT IN (?, ?)) "
        "AND mark IS NULL AND note IS NULL AND act_id IS NULL AND received_at IS NULL",
        (account_id, READY, PICKED),
    )
    return_acts.drop_empty(account_id)
    _fill_cards(account)
    result = {"yandex_returns": counts[READY], "yandex_received": counts[PICKED]}
    if gone:
        result["yandex_returns_gone"] = gone
    if counts["skipped"]:
        result["yandex_returns_skipped"] = counts["skipped"]
    if not campaigns:
        result["yandex_returns_note"] = "у кабинета не нашлось ни одного магазина"
    return result


def _rebuild_ready(account_id: int, seen: set[str]) -> int:
    """«К выдаче» — снимок последней загрузки: чего в ответе нет, того в пункте нет.

    Только после полного обхода: на оборванном списке это вычистило бы всё, до
    чего не дочитали. Строку с работой не удаляем, а снимаем с неё статус.
    """
    left = [row["id"] for row in db.query(
        "SELECT id FROM yandex_returns WHERE account_id = ? AND shipment_status = ?", (account_id, READY),
    ) if row["id"] not in seen]
    for start in range(0, len(left), 400):
        batch = left[start : start + 400]
        marks = ",".join("?" for _ in batch)
        db.execute(
            f"DELETE FROM yandex_returns WHERE account_id = ? AND id IN ({marks}) "
            "AND mark IS NULL AND note IS NULL AND act_id IS NULL",
            [account_id] + batch,
        )
        db.execute(
            f"UPDATE yandex_returns SET shipment_status = NULL, updated_at = ? "
            f"WHERE account_id = ? AND id IN ({marks})",
            [db.now_iso(), account_id] + batch,
        )
    return len(left)


def _fill_cards(account: dict) -> None:
    """Карточки товаров из возвратов, которых нет в каталоге: без них нет названия и штрихкода."""
    from . import sync as yandex_sync

    offers: list[str] = []
    for row in db.query("SELECT items FROM yandex_returns WHERE account_id = ?", (account["id"],)):
        offers += [str(item.get("offer_id")) for item in db.json_list(row["items"])
                   if isinstance(item, dict) and item.get("offer_id")]
    yandex_sync.fill_cards(account, list(dict.fromkeys(offers)))


def sync(account: dict, full: bool = False) -> dict:  # noqa: ARG001 - полного режима у Маркета нет
    result = sync_returns(account)
    parts = [f"Готово к выдаче: {result.get('yandex_returns', 0)}",
             f"выдано магазину за {RECEIVED_DAYS} дн.: {result.get('yandex_received', 0)}"]
    if result.get("yandex_returns_gone"):
        parts.append(f"ушло из выдачи: {result['yandex_returns_gone']}")
    if result.get("yandex_returns_note"):
        parts.append(result["yandex_returns_note"])
    result["message"] = ". ".join(parts)
    return result


# ------------------------------------------------------- раздел «Возвраты»
def _ready_where(account_ids: list[int], params: dict | None) -> tuple[str, list]:
    """Условие «лежит в пункте» с фильтрами раздела — одно для списка и для числа."""
    params = params or {}
    marks = ",".join("?" for _ in account_ids)
    conditions = [f"r.account_id IN ({marks})", "r.shipment_status = ?"]
    args: list = list(account_ids) + [READY]
    place = str(params.get("place") or "")
    if place:
        conditions.append("r.place_name = ?")
        args.append(place)
    q = str(params.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        conditions.append(
            "(r.id LIKE ? OR r.order_id LIKE ? OR r.tracks LIKE ? OR r.items LIKE ?"
            " OR EXISTS (SELECT 1 FROM products p WHERE p.account_id = r.account_id"
            "            AND r.items LIKE '%\"' || p.sku || '\"%' AND lower_ru(p.name) LIKE ?))"
        )
        args += [like] * 4 + [f"%{q.lower()}%"]
    return " AND ".join(conditions), args


def ready(account_ids: list[int], params: dict | None = None, limit: int = 1000) -> list[dict]:
    """Возвраты, готовые к выдаче: пункт впереди — на листе строки одного пункта идут подряд."""
    if not account_ids:
        return []
    where, args = _ready_where(account_ids, params)
    rows = db.query(
        f"SELECT r.*, a.title AS account_title FROM yandex_returns r "
        f"LEFT JOIN accounts a ON a.id = r.account_id WHERE {where} "
        f"ORDER BY (r.place_name IS NULL), r.place_name, a.title, (r.pickup_till IS NULL), r.pickup_till, r.id "
        f"LIMIT ?",
        args + [limit],
    )
    return [return_view(row) for row in rows]


def count_ready(account_ids: list[int], params: dict | None = None) -> int:
    if not account_ids:
        return 0
    where, args = _ready_where(account_ids, params)
    return db.query_one(f"SELECT COUNT(*) AS c FROM yandex_returns r WHERE {where}", args)["c"]


def places(account_ids: list[int]) -> list[str]:
    """Пункты, где лежат возвраты, — варианты фильтра «Пункт выдачи»."""
    if not account_ids:
        return []
    marks = ",".join("?" for _ in account_ids)
    rows = db.query(
        f"SELECT DISTINCT place_name FROM yandex_returns WHERE account_id IN ({marks}) "
        "AND shipment_status = ? AND place_name IS NOT NULL AND place_name != '' ORDER BY place_name",
        list(account_ids) + [READY],
    )
    return [row["place_name"] for row in rows]


def page(account: dict, params: dict) -> dict:
    """Контекст вкладки «К выдаче» кабинета Маркета: строки и плитки без фильтров."""
    ids = [account["id"]]
    kinds = {
        kind: db.query_one("SELECT COUNT(*) AS c FROM yandex_returns WHERE account_id = ? "
                           "AND shipment_status = ? AND return_type = ?", (account["id"], READY, kind))["c"]
        for kind in yandex.RETURN_TYPE_LABELS
    }
    return {
        "items": ready(ids, params=params),
        "stats": [("Готовы к выдаче", count_ready(ids)),
                  ("Невыкупы", kinds["UNREDEEMED"]),
                  ("Возвраты", kinds["RETURN"])],
    }


def act_rows(act_id: str) -> list[dict]:
    rows = db.query(
        "SELECT r.*, a.title AS account_title FROM yandex_returns r "
        "LEFT JOIN accounts a ON a.id = r.account_id WHERE r.act_id = ? ORDER BY a.title, r.id",
        (act_id,),
    )
    return [return_view(row) for row in rows]


def quantity(row: dict) -> int:
    return int(row.get("items_count") or 0)


def received(account_id: int, day: str) -> list[str]:
    """Выданные магазину за число и ещё ни в один акт не попавшие."""
    return [row["id"] for row in db.query(
        "SELECT id FROM yandex_returns WHERE account_id = ? AND received_day = ? AND act_id IS NULL "
        "AND shipment_status = ? ORDER BY received_at, id",
        (account_id, day, PICKED),
    )]


def claim(conn, act_id: str, account_id: int, return_ids: list[str]) -> int:
    """Забрать возвраты в акт. Только свободные: второй акт на ту же работу не заводим."""
    marks = ",".join("?" for _ in return_ids)
    cursor = conn.execute(
        f"UPDATE yandex_returns SET act_id = ? WHERE account_id = ? AND id IN ({marks}) AND act_id IS NULL",
        [act_id, account_id] + list(return_ids),
    )
    return cursor.rowcount or 0


# ------------------------------------------------------- приёмка сканером
def find(account_ids: list[int], code: str) -> list[tuple[int, str]]:
    """Возвраты по посылке: номер возврата — он напечатан на ней, — заказ или трек."""
    variants = barcode_variants(code)
    if not variants or not account_ids:
        return []
    shops = ",".join("?" for _ in account_ids)
    marks = ",".join("?" for _ in variants)
    tracks = " OR ".join("r.tracks LIKE ?" for _ in variants)
    rows = db.query(
        f"SELECT r.account_id, r.id FROM yandex_returns r WHERE r.account_id IN ({shops}) "
        f"AND (r.id IN ({marks}) OR r.order_id IN ({marks}) OR {tracks}) ORDER BY r.account_id, r.id",
        list(account_ids) + variants * 2 + [f"%,{variant},%" for variant in variants],
    )
    return [(row["account_id"], row["id"]) for row in rows]


def scan_view(row: dict) -> dict:
    """Возврат в окне приёмки: номер, вид и товары, которые должны в нём лежать."""
    data = return_view(row)
    goods = data["goods"]
    facts = [
        ("Возврат", data.get("id")),
        ("Вид", data["type_label"]),
        ("Заказ", data.get("order_id")),
        ("Трек", ", ".join(data["track_list"])),
        ("Пункт", data.get("place_name")),
    ]
    return {
        "title": goods[0]["name"] if len(goods) == 1 else f"Возврат {data['id']}",
        "number": str(data["id"]),
        "facts": [(label, str(value)) for label, value in facts if value],
        # Карточка товара Маркета в каталоге панели — его артикул (offerId):
        # по ней ядро берёт штрихкоды товара и сопоставленных карточек.
        "goods": [{
            "key": good["offer_id"] or str(index),
            "name": good["name"],
            "need": good["count"],
            "sku": good["offer_id"],
            "offer_id": good["article"],
            "image": good["image"],
        } for index, good in enumerate(goods)],
    }


def pdf_table(pdf, rows: list[dict], everywhere: bool) -> None:
    """Таблица возвратов Маркета на листе PDF — те же колонки, что в HTML-листе."""
    import io

    headings = ["№"]
    widths = [8.0]
    if everywhere:
        headings.append("Кабинет")
        widths.append(22.0)
    headings += ["Возврат", "Штрихкод", "Товары", "Кол-во", "Пункт выдачи", "Отметка", "Комментарий"]
    widths += [28.0, 34.0, 60.0, 14.0, 44.0, 20.0, 33.0]
    free = pdf.w - pdf.l_margin - pdf.r_margin
    scale = free / sum(widths)
    widths = [width * scale for width in widths]

    pdf.set_font("sheet", "", 7)
    with pdf.table(col_widths=tuple(widths), first_row_as_headings=True,
                   line_height=4, padding=1.2, repeat_headings=1) as table:
        head = table.row()
        for name in headings:
            head.cell(name)
        for index, item in enumerate(rows, start=1):
            row = table.row()
            row.cell(str(index))
            if everywhere:
                row.cell(cut(item.get("account_title") or "—", 22))
            # Номер возврата напечатан на посылке — его и штрихкод. Рядом ещё
            # цифрами: не считался сканером — в пункте наберут руками.
            number = str(item.get("id") or "")
            row.cell("\n".join(x for x in (number, item.get("type_label"),
                                           f"заказ {item['order_id']}" if item.get("order_id") else "") if x))
            if number:
                row.cell(img=io.BytesIO(barcode_svg(number)), img_fill_width=True)
            else:
                row.cell("—")
            goods = "\n".join(f"{good['count']} × {cut(good['name'], 52)}" for good in item.get("goods") or [])
            row.cell(goods or "—")
            row.cell(str(item.get("items_count") or ""))
            where = [item.get("place_name"), item.get("place_address")]
            if item.get("pickup_till_local"):
                where.append(f"забрать до {item['pickup_till_local']}")
            row.cell(cut(" · ".join(x for x in where if x) or "—", 70))
            row.cell(mark_cell(item))
            row.cell(cut(item.get("note"), 60))


SOURCE = ReturnsSource(
    table="yandex_returns",
    label="Яндекс Маркет",
    unit="возвр.",
    ready=ready,
    count_ready=count_ready,
    page=page,
    act_rows=act_rows,
    quantity=quantity,
    filters=("q", "place"),
    places=places,
    list_template="yandex/returns_list.html",
    sheet_template="yandex/returns_sheet.html",
    act_template="yandex/returns_act_rows.html",
    pdf_table=pdf_table,
    sync=sync,
    received=received,
    claim=claim,
    find=find,
    scan_view=scan_view,
)
