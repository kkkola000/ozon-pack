from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from pack_v2.integrations.models import MarketplaceAccount
from pack_v2.modules.catalog.models import Offer, OfferRef, Product, ProductCode
from pack_v2.modules.orders.models import (
    Order,
    OrderLine,
    OrderUnit,
    Package,
    PackageContent,
    Shipment,
    ShipmentRef,
    ShipmentUnit,
)


def seed(tx, account=None):
    account = account or MarketplaceAccount(marketplace="ozon", title="Кабинет")
    tx.add(account)
    tx.flush()
    order = Order(account_id=account.id)
    tx.add(order)
    tx.flush()
    line = OrderLine(
        account_id=account.id,
        order_id=order.id,
        name="Товар",
        quantity=1,
        price=Decimal("123.4500"),
        currency="RUB",
    )
    tx.add(line)
    tx.flush()
    unit = OrderUnit(order_id=order.id, line_id=line.id, ordinal=1)
    shipment = Shipment(account_id=account.id, order_id=order.id)
    tx.add_all([unit, shipment])
    tx.flush()
    allocation = ShipmentUnit(order_id=order.id, shipment_id=shipment.id, unit_id=unit.id)
    tx.add(allocation)
    tx.flush()
    return account, order, line, unit, shipment, allocation


def test_one_item_in_two_boxes_keeps_one_unit(db):
    with db.transaction() as tx:
        account, order, line, unit, shipment, allocation = seed(tx)
        boxes = [Package(account_id=account.id, shipment_id=shipment.id) for _ in range(2)]
        tx.add_all(boxes)
        tx.flush()
        tx.add_all(
            PackageContent(
                shipment_id=shipment.id,
                package_id=box.id,
                shipment_unit_id=allocation.id,
                part_index=n,
                part_count=2,
            )
            for n, box in enumerate(boxes, 1)
        )
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(OrderUnit)) == 1
        assert tx.scalar(select(func.count()).select_from(PackageContent)) == 2
        assert tx.get(OrderLine, line.id).price == Decimal("123.4500")


def test_unit_cannot_belong_to_two_active_shipments(db):
    with db.transaction() as tx:
        account, order, _, unit, _, allocation = seed(tx)
        other = Shipment(account_id=account.id, order_id=order.id)
        tx.add(other)
        tx.flush()
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(ShipmentUnit(order_id=order.id, shipment_id=other.id, unit_id=unit.id))
    from datetime import UTC, datetime

    with db.transaction() as tx:
        tx.get(ShipmentUnit, allocation.id).released_at = datetime.now(UTC)
        tx.flush()
        tx.add(ShipmentUnit(order_id=order.id, shipment_id=other.id, unit_id=unit.id))


def test_cannot_allocate_unit_from_another_order(db):
    with db.transaction() as tx:
        _, _, _, unit, _, _ = seed(tx)
        _, order, _, _, shipment, _ = seed(tx)
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(ShipmentUnit(order_id=order.id, shipment_id=shipment.id, unit_id=unit.id))


def test_package_cannot_contain_allocation_from_other_shipment(db):
    with db.transaction() as tx:
        _, _, _, _, _, allocation = seed(tx)
        account, _, _, _, shipment, _ = seed(tx)
        box = Package(account_id=account.id, shipment_id=shipment.id)
        tx.add(box)
        tx.flush()
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(PackageContent(shipment_id=shipment.id, package_id=box.id, shipment_unit_id=allocation.id))


def test_part_numbers_cannot_repeat_or_exceed_total(db):
    with db.transaction() as tx:
        account, _, _, _, shipment, allocation = seed(tx)
        box = Package(account_id=account.id, shipment_id=shipment.id)
        tx.add(box)
        tx.flush()
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(
            PackageContent(
                shipment_id=shipment.id,
                package_id=box.id,
                shipment_unit_id=allocation.id,
                part_index=3,
                part_count=2,
            )
        )
    with db.transaction() as tx:
        tx.add(
            PackageContent(
                shipment_id=shipment.id,
                package_id=box.id,
                shipment_unit_id=allocation.id,
                part_index=1,
                part_count=2,
            )
        )
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(
            PackageContent(
                shipment_id=shipment.id,
                package_id=box.id,
                shipment_unit_id=allocation.id,
                part_index=1,
                part_count=2,
            )
        )


def test_external_refs_preserve_zeros_and_ii_and_allow_ambiguous_alias(db):
    with db.transaction() as tx:
        account, order, _, _, shipment, _ = seed(tx)
        second = Shipment(account_id=account.id, order_id=order.id)
        tx.add(second)
        tx.flush()
        tx.add(
            ShipmentRef(
                account_id=account.id,
                shipment_id=shipment.id,
                kind="posting_number",
                value="010033881-0157-1",
                authoritative=True,
            )
        )
        tx.add_all(
            ShipmentRef(account_id=account.id, shipment_id=s.id, kind="label", value="ii50082908916")
            for s in (shipment, second)
        )
    with db.transaction() as tx:
        found = tx.scalars(select(ShipmentRef).where(ShipmentRef.value == "ii50082908916")).all()
        assert len(found) == 2
        assert (
            tx.scalar(select(ShipmentRef.value).where(ShipmentRef.kind == "posting_number"))
            == "010033881-0157-1"
        )
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(
            ShipmentRef(
                account_id=account.id,
                shipment_id=second.id,
                kind="posting_number",
                value="010033881-0157-1",
                authoritative=True,
            )
        )


def test_cross_account_ref_and_offer_are_rejected(db):
    with db.transaction() as tx:
        account, _, _, _, shipment, _ = seed(tx)
        other, other_order, _, _, _, _ = seed(tx)
        offer = Offer(account_id=other.id, name="Чужая карточка")
        tx.add(offer)
        tx.flush()
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(ShipmentRef(account_id=other.id, shipment_id=shipment.id, kind="posting", value="123"))
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(OrderLine(account_id=account.id, order_id=other_order.id, name="Неверный кабинет", quantity=1))
    with db.transaction() as tx:
        own_order = Order(account_id=account.id)
        tx.add(own_order)
        tx.flush()
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(
            OrderLine(
                account_id=account.id,
                order_id=own_order.id,
                offer_id=offer.id,
                name="Чужая карточка",
                quantity=1,
            )
        )


def test_offer_id_unique_per_account_but_barcode_not_globally_unique(db):
    with db.transaction() as tx:
        account = MarketplaceAccount(marketplace="ozon", title="Кабинет")
        tx.add(account)
        tx.flush()
        offers = [Offer(account_id=account.id, name="Карточка") for _ in range(2)]
        products = [Product(name="Товар") for _ in range(2)]
        tx.add_all([*offers, *products])
        tx.flush()
        tx.add(OfferRef(account_id=account.id, offer_id=offers[0].id, kind="sku", value="00123"))
        tx.add_all(
            ProductCode(product_id=p.id, kind="gtin", value="04660291070982", origin="marketplace")
            for p in products
        )
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(OfferRef(account_id=account.id, offer_id=offers[1].id, kind="sku", value="00123"))


def test_nonpositive_quantity_is_rejected(db):
    with db.transaction() as tx:
        account, order, _, _, _, _ = seed(tx)
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(OrderLine(account_id=account.id, order_id=order.id, name="Товар", quantity=0))
