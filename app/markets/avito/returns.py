"""Возвраты Avito: заказ в статусе «возврат», лежащий в пункте выдачи.

Отдельного метода у Avito нет — возврат приходит внутри заказа. Забранный
возврат площадка переводит дальше и перестаёт отдавать; для панели это и есть
факт получения: такому заказу ставится день получения, и из полученных за
день составляется акт — так же, как у Ozon по статусу «Получен».
"""
from __future__ import annotations

import logging

from ...core import db
from ...core.returns_pdf import barcode_svg, cut, mark_cell
from ..base import ReturnsSource
from . import client as avito
from . import store

log = logging.getLogger("avito.returns")


def _search(q: str) -> tuple[str, list]:
    if not q:
        return "", []
    like = f"%{q.strip()}%"
    return (
        " AND (o.id LIKE ? OR o.marketplace_id LIKE ? OR o.return_tracking LIKE ? OR o.tracking_number LIKE ?"
        " OR o.buyer_name LIKE ?"
        " OR EXISTS (SELECT 1 FROM avito_order_items i"
        "            WHERE i.account_id = o.account_id AND i.order_id = o.id"
        "            AND (i.title LIKE ? OR i.avito_id LIKE ? OR i.seller_id LIKE ?)))",
        [like] * 8,
    )


def ready(account_ids: list[int], params: dict | None = None, limit: int = 500) -> list[dict]:
    """Возвраты, которые ещё лежат в пункте выдачи, — только их и можно забрать.

    Строка — заказ целиком, с вложенными товарами. Полученные (пропавшие из
    выдачи) сюда не попадают: они ждут акта.
    """
    if not account_ids:
        return []
    marks = ",".join("?" for _ in account_ids)
    search_sql, search_params = _search(str((params or {}).get("q") or ""))
    rows = db.query(
        f"SELECT o.*, a.title AS account_title FROM avito_orders o "
        f"LEFT JOIN accounts a ON a.id = o.account_id "
        f"WHERE o.account_id IN ({marks}) AND o.status = ? AND o.received_at IS NULL{search_sql} "
        f"ORDER BY a.title, (o.updated_at_api IS NULL), o.updated_at_api DESC LIMIT ?",
        list(account_ids) + [avito.STATUS_ON_RETURN] + search_params + [limit],
    )
    return [store.avito_view(row) for row in rows]


def count_ready(account_ids: list[int]) -> int:
    if not account_ids:
        return 0
    marks = ",".join("?" for _ in account_ids)
    return db.query_one(
        f"SELECT COUNT(*) AS c FROM avito_orders WHERE status = ? AND received_at IS NULL "
        f"AND account_id IN ({marks})",
        [avito.STATUS_ON_RETURN] + list(account_ids),
    )["c"]


def page(account: dict, params: dict) -> dict:
    """Контекст вкладки «К выдаче» для кабинета Avito."""
    q = str(params.get("q") or "")
    items = ready([account["id"]], params={"q": q})
    return {
        "items": items,
        "stats": [("Заберите заказ", count_ready([account["id"]]))],
        "q": q,
        "sheet_subtitle": "",
    }


def act_rows(act_id: str) -> list[dict]:
    rows = db.query(
        "SELECT o.*, a.title AS account_title FROM avito_orders o "
        "LEFT JOIN accounts a ON a.id = o.account_id WHERE o.act_id = ? ORDER BY a.title, o.id",
        (act_id,),
    )
    return [store.avito_view(row) for row in rows]


def received(account_id: int, day: str) -> list[str]:
    """Полученные за число возвраты, ещё ни в один акт не попавшие."""
    return [
        row["id"] for row in db.query(
            "SELECT id FROM avito_orders WHERE account_id = ? AND received_day = ? AND act_id IS NULL "
            "ORDER BY received_at, id",
            (account_id, day),
        )
    ]


def claim(conn, act_id: str, account_id: int, order_ids: list[str]) -> int:
    """Забрать заказы в акт. Возвращает, сколько реально переехало."""
    marks = ",".join("?" for _ in order_ids)
    cursor = conn.execute(
        f"UPDATE avito_orders SET act_id = ? WHERE account_id = ? AND id IN ({marks}) AND act_id IS NULL",
        [act_id, account_id] + list(order_ids),
    )
    return cursor.rowcount or 0


def mark_received(conn, account_id: int, order_ids: list[str]) -> int:
    """Возврат пропал из выдачи — значит, его забрали. Ставим день получения один раз."""
    if not order_ids:
        return 0
    from ...core.store import local_day

    now = db.now_iso()
    marks = ",".join("?" for _ in order_ids)
    cursor = conn.execute(
        f"UPDATE avito_orders SET received_at = ?, received_day = ? "
        f"WHERE account_id = ? AND id IN ({marks}) AND status = ? AND received_at IS NULL",
        [now, local_day(now), account_id] + list(order_ids) + [avito.STATUS_ON_RETURN],
    )
    return cursor.rowcount or 0


def sync(account: dict, full: bool = False) -> dict:  # noqa: ARG001 - у Avito полного и быстрого режима нет
    from . import sync as avito_sync

    result = avito_sync.sync_avito(account)
    parts = [f"Готово к выдаче: {result.get('avito_returns', 0)}"]
    if result.get("avito_received"):
        parts.append(f"получено (пропало из выдачи): {result['avito_received']}")
    result["message"] = ". ".join(parts)
    return result


def quantity(row: dict) -> int:
    return int(row.get("items_count") or 0)


def pdf_table(pdf, orders: list[dict], everywhere: bool) -> None:
    """Таблица заказов Avito на листе PDF — те же колонки, что в HTML-листе."""
    headings = ["№"]
    widths = [8.0]
    if everywhere:
        headings.append("Кабинет")
        widths.append(22.0)
    headings += ["Заказ", "Трек возврата", "Товары", "Кол-во", "Куда ехать", "Отметка", "Комментарий"]
    widths += [30.0, 32.0, 62.0, 14.0, 42.0, 20.0, 33.0]
    free = pdf.w - pdf.l_margin - pdf.r_margin
    scale = free / sum(widths)
    widths = [width * scale for width in widths]

    pdf.set_font("sheet", "", 7)
    with pdf.table(col_widths=tuple(widths), first_row_as_headings=True,
                   line_height=4, padding=1.2, repeat_headings=1) as table:
        head = table.row()
        for name in headings:
            head.cell(name)
        for index, order in enumerate(orders, start=1):
            row = table.row()
            row.cell(str(index))
            if everywhere:
                row.cell(cut(order.get("account_title") or "—", 22))
            tracking = str(order.get("return_tracking") or "").strip()
            row.cell("\n".join(
                x for x in (str(order.get("marketplace_id") or order.get("id")),
                            order.get("buyer_name"), tracking) if x
            ))
            if tracking:
                import io

                row.cell(img=io.BytesIO(barcode_svg(tracking)), img_fill_width=True)
            else:
                row.cell("—")
            goods = "\n".join(
                f"{item.get('quantity')} × {cut(item.get('title') or 'Без названия', 52)}"
                for item in (order.get("items") or [])
            )
            row.cell(goods or "—")
            row.cell(str(order.get("items_count") or ""))
            where = [order.get("service_name") or order.get("service_label"), order.get("terminal_address")]
            if order.get("terminal_code"):
                where.append(f"ПВЗ {order['terminal_code']}")
            row.cell(cut(" · ".join(x for x in where if x) or "адрес Avito не прислал", 70))
            row.cell(mark_cell(order))
            row.cell(cut(order.get("note"), 60))


SOURCE = ReturnsSource(
    table="avito_orders",
    label="Avito",
    unit="заказ(ов)",
    ready=ready,
    count_ready=count_ready,
    page=page,
    act_rows=act_rows,
    quantity=quantity,
    list_template="avito/returns_list.html",
    sheet_template="avito/returns_sheet.html",
    act_template="avito/returns_act_rows.html",
    pdf_table=pdf_table,
    sync=sync,
    received=received,
    claim=claim,
)
