"""Stable document identity uses provider composition and current label aliases."""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from pack_v2.shared.errors import Conflict, NotFound

from .models import Shipment, ShipmentItem, ShipmentRef


@dataclass(frozen=True)
class LabelTarget:
    account_id: UUID
    shipment_id: UUID
    posting_number: str
    basis: str
    state: str
    eligible: bool
    products: tuple
    references: tuple
    package_count: int | None

    def matches(self, snapshot):
        products = tuple(
            sorted(
                (
                    i.source_line_id,
                    i.quantity,
                    tuple(sorted({(r.kind, r.value) for r in i.product_references})),
                )
                for i in snapshot.items
            )
        )
        refs = tuple(
            sorted(
                {
                    (r.kind, r.value)
                    for r in snapshot.references
                    if r.kind in ("posting_number", "label_barcode", "layout_digest")
                }
            )
        )
        return (
            snapshot.source.account_id == self.account_id
            and snapshot.source_id == self.posting_number
            and products == self.products
            and refs == self.references
            and snapshot.package_count_hint == self.package_count
        )


def target(tx, shipment_id):
    shipment = tx.get(Shipment, shipment_id)
    if shipment is None:
        raise NotFound("Отправление не найдено")
    refs = tuple(
        sorted(
            (r.kind, r.value)
            for r in tx.scalars(select(ShipmentRef).where(ShipmentRef.shipment_id == shipment_id))
            if r.kind in ("posting_number", "label_barcode", "layout_digest")
        )
    )
    numbers = [value for kind, value in refs if kind == "posting_number"]
    if len(numbers) != 1:
        raise Conflict("Не подтверждён номер отправления для стикера")
    products = tuple(
        sorted(
            (i.source_line_id, i.quantity, tuple(sorted({(r["kind"], r["value"]) for r in i.references})))
            for i in tx.scalars(
                select(ShipmentItem).where(
                    ShipmentItem.shipment_id == shipment_id, ShipmentItem.source_present.is_(True)
                )
            )
        )
    )
    basis = hashlib.sha256(
        json.dumps([numbers[0], products, refs, shipment.package_count_hint], separators=(",", ":")).encode()
    ).hexdigest()
    return LabelTarget(
        shipment.account_id,
        shipment_id,
        numbers[0],
        basis,
        shipment.state,
        bool(products)
        and not shipment.needs_review
        and not shipment.layout_review
        and not shipment.transition_failed
        and shipment.state in ("awaiting_assembly", "awaiting_dispatch"),
        products,
        refs,
        shipment.package_count_hint,
    )


def archive_info(tx, shipment_ids):
    from .models import OrderLine, OrderRef, OrderUnit, ShipmentUnit

    ids = list(shipment_ids)
    shipments = {row.id: row for row in tx.scalars(select(Shipment).where(Shipment.id.in_(ids)))}
    refs = {}
    for row in tx.scalars(select(ShipmentRef).where(ShipmentRef.shipment_id.in_(ids))):
        refs.setdefault(row.shipment_id, []).append([row.kind, row.value])
    orders = {}
    for row in tx.scalars(
        select(OrderRef).where(OrderRef.order_id.in_([s.order_id for s in shipments.values()]))
    ):
        orders.setdefault(row.order_id, {})[row.kind] = row.value
    items, item_index = {}, {}
    for row in tx.scalars(
        select(ShipmentItem).where(ShipmentItem.shipment_id.in_(ids)).order_by(ShipmentItem.source_line_id)
    ):
        value = {"id": str(row.id), "name": row.name, "article": row.article, "quantity": row.quantity}
        item_index[(row.shipment_id, "packing:" + row.source_line_id)] = value
        if row.source_present:
            items.setdefault(row.shipment_id, []).append(value)
    units = {}
    for row, line in tx.execute(
        select(ShipmentUnit, OrderLine)
        .join(OrderUnit, OrderUnit.id == ShipmentUnit.unit_id)
        .join(OrderLine, OrderLine.id == OrderUnit.line_id)
        .where(ShipmentUnit.shipment_id.in_(ids))
    ):
        value = item_index.get((row.shipment_id, line.external_line_id))
        if value:
            units.setdefault(row.shipment_id, {})[str(row.unit_id)] = value
    result = {}
    for sid, shipment in shipments.items():
        references = sorted(refs.get(sid, []))
        identity = orders.get(shipment.order_id, {})
        number = next((v for k, v in references if k == "posting_number"), "")
        order = identity.get("order_number") or identity.get("order_id") or ""
        lines = items.get(sid, [])
        result[sid] = {
            "posting_number": number,
            "order_number": order,
            "references": references,
            "items": lines,
            "units": units.get(sid, {}),
            "search_text": " ".join(
                [
                    number,
                    order,
                    *(v for _, v in references),
                    *(str(i.get(k) or "") for i in lines for k in ("name", "article")),
                ]
            ),
        }
    return result


def archive_search(query):
    from sqlalchemy import or_

    from .models import OrderRef

    return select(Shipment.id).where(
        or_(
            Shipment.id.in_(
                select(ShipmentRef.shipment_id).where(ShipmentRef.value.icontains(query, autoescape=True))
            ),
            Shipment.order_id.in_(
                select(OrderRef.order_id).where(OrderRef.value.icontains(query, autoescape=True))
            ),
        )
    )
