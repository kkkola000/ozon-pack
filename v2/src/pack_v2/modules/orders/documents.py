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
                    if r.kind in ("posting_number", "label_barcode")
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
            if r.kind in ("posting_number", "label_barcode")
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
