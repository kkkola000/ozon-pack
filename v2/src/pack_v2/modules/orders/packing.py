"""Public order-module ports used by assembly, with no scanner/session ownership here."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import exists, func, select

from pack_v2.infrastructure.jobs import enqueue
from pack_v2.infrastructure.models import Job
from pack_v2.shared.errors import Conflict, NotFound

from .contracts import PackingCandidate, PackingLine, PackingManifest, PlannedUnit
from .models import Order, OrderLine, OrderSync, OrderUnit, Shipment, ShipmentItem, ShipmentRef, ShipmentUnit

ELIGIBLE = ("awaiting_assembly", "awaiting_dispatch")


def lock_shipment(tx, shipment_id):
    row = tx.get(Shipment, shipment_id)
    if row is None:
        raise NotFound("Отправление не найдено")
    tx.scalar(select(Order).where(Order.id == row.order_id).with_for_update())
    return tx.scalar(
        select(Shipment)
        .where(Shipment.id == shipment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def manifest(tx, shipment_id, catalog, *, allow_ship_failed=False):
    shipment = tx.get(Shipment, shipment_id)
    if shipment is None:
        raise NotFound("Отправление не найдено")
    number = tx.scalar(
        select(ShipmentRef.value).where(
            ShipmentRef.shipment_id == shipment_id,
            ShipmentRef.kind == "posting_number",
            ShipmentRef.authoritative.is_(True),
        )
    )
    if not number:
        raise Conflict("Не подтверждён номер отправления")
    items = tx.scalars(
        select(ShipmentItem)
        .where(ShipmentItem.shipment_id == shipment_id, ShipmentItem.source_present.is_(True))
        .order_by(ShipmentItem.source_line_id)
    ).all()
    cards = catalog.packing_cards(tx, {i.offer_id for i in items if i.offer_id})
    lines = []
    for row in items:
        card = cards.get(row.offer_id)
        lines.append(
            PackingLine(
                row.id,
                row.source_line_id,
                row.offer_id,
                row.name,
                row.article,
                row.quantity,
                tuple((r["kind"], r["value"]) for r in row.references),
                card["codes"] if card else (),
                card["card"].image_url if card else None,
                "cis" in row.required_fields or "cis" in row.optional_fields,
                card["has_components"] if card else False,
                tuple(row.required_fields),
                tuple(row.optional_fields),
                card["gtins"] if card else (),
            )
        )
    physical = [
        (str(i.item_id), i.source_line_id, str(i.offer_id), i.quantity, i.has_components) for i in lines
    ]
    basis = hashlib.sha256(json.dumps(physical, separators=(",", ":")).encode()).hexdigest()
    return PackingManifest(
        shipment.id,
        shipment.order_id,
        shipment.account_id,
        number,
        shipment.assembly_revision,
        basis,
        shipment.state,
        not shipment.needs_review
        and (
            (shipment.state in ELIGIBLE and not shipment.transition_failed)
            or allow_ship_failed
            and shipment.source_substatus == "ship_failed"
            and shipment.source_status in ("awaiting_packaging", "awaiting_deliver")
        ),
        shipment.deadline,
        shipment.warehouse_name,
        tuple(lines),
    )


def queue_refresh(tx, source, shipment_id, actor_id=None):
    saved = lock_shipment(tx, shipment_id)
    if saved.account_id != source.account_id:
        raise Conflict("Кабинет отправления не совпадает с запросом")
    now = datetime.now(UTC)
    running = tx.scalar(
        select(OrderSync)
        .where(
            OrderSync.account_id == source.account_id,
            OrderSync.target_shipment_id == shipment_id,
            OrderSync.mode == "point",
            OrderSync.status.in_(("queued", "running", "retrying")),
        )
        .with_for_update()
    )
    if running:
        job = tx.get(Job, running.job_id)
        if (
            running.credential_revision == source.credential_revision
            and job.status in ("pending", "running")
            and not (job.status == "running" and job.lease_until <= now and job.attempts >= job.max_attempts)
        ):
            return running.id
        running.status, running.finished_at = "stale", now
        tx.flush()
    run_id = uuid4()
    job_id = enqueue(tx, "orders.sync", "orders.sync:" + str(run_id), {"run_id": str(run_id)})
    tx.add(
        OrderSync(
            id=run_id,
            account_id=source.account_id,
            credential_revision=source.credential_revision,
            job_id=job_id,
            requested_by=actor_id,
            mode="point",
            since=now,
            until=now,
            target_shipment_id=shipment_id,
            expected_shipment_revision=saved.revision,
        )
    )
    tx.flush()
    return run_id


def refresh_status(tx, run_id, account_id, shipment_id):
    run = tx.get(OrderSync, run_id)
    if (
        run is None
        or run.account_id != account_id
        or run.target_shipment_id != shipment_id
        or run.mode != "point"
    ):
        raise Conflict("Проверка отправления не найдена")
    job = tx.get(Job, run.job_id)
    failed = run.status in ("queued", "running", "retrying") and (
        job.status in ("failed", "succeeded")
        or job.status == "running"
        and job.lease_until <= datetime.now(UTC)
        and job.attempts >= job.max_attempts
    )
    return ("failed", "Проверка прервана. Повторите её") if failed else (run.status, run.error)


def candidates(
    tx, catalog, *, code=None, account_id=None, account_ids=None, proofs=None, busy=None, limit=30
):
    query = select(Shipment).where(
        Shipment.state.in_(ELIGIBLE), Shipment.needs_review.is_(False), Shipment.transition_failed.is_(False)
    )
    if account_id:
        query = query.where(Shipment.account_id == account_id)
    if account_ids is not None:
        query = query.where(Shipment.account_id.in_(account_ids))
    kind = "queue"
    if code:
        labels = tx.scalars(
            select(ShipmentRef.shipment_id).where(
                ShipmentRef.value == code,
                ShipmentRef.kind == "posting_number",
                ShipmentRef.authoritative.is_(True),
            )
        ).all()
        if not labels:
            labels = tx.scalars(select(ShipmentRef.shipment_id).where(ShipmentRef.value == code)).all()
        if labels:
            kind = "label"
            query = query.where(Shipment.id.in_(labels))
        else:
            kind = "product"
            offers = catalog.packing_code_offers(tx, code, account_id)
            query = query.where(
                exists(
                    select(ShipmentItem.id).where(
                        ShipmentItem.shipment_id == Shipment.id,
                        ShipmentItem.source_present.is_(True),
                        ShipmentItem.offer_id.in_(offers),
                    )
                )
            )
            if busy is not None:
                query = query.where(Shipment.id.not_in(busy))
            if proofs is not None:
                from sqlalchemy import tuple_

                query = query.where(tuple_(Shipment.id, Shipment.assembly_revision).not_in(proofs))
    rows = tx.scalars(
        query.order_by(
            Shipment.deadline.asc().nulls_last(), Shipment.processing_started_at, Shipment.id
        ).limit(limit)
    ).all()
    ids = [r.id for r in rows]
    numbers = dict(
        tx.execute(
            select(ShipmentRef.shipment_id, ShipmentRef.value).where(
                ShipmentRef.shipment_id.in_(ids),
                ShipmentRef.kind == "posting_number",
                ShipmentRef.authoritative.is_(True),
            )
        ).all()
    )
    quantities = dict(
        tx.execute(
            select(ShipmentItem.shipment_id, func.sum(ShipmentItem.quantity))
            .where(ShipmentItem.shipment_id.in_(ids), ShipmentItem.source_present.is_(True))
            .group_by(ShipmentItem.shipment_id)
        ).all()
    )
    return kind, tuple(
        PackingCandidate(
            r.id,
            r.account_id,
            r.state,
            r.assembly_revision,
            numbers.get(r.id, str(r.id)),
            quantities.get(r.id, 0),
            r.deadline,
            r.warehouse_name,
        )
        for r in rows
    )


def plan_units(tx, manifest):
    if not manifest.eligible or not manifest.items:
        raise Conflict("Отправление сейчас недоступно для сборки")
    if any(i.offer_id is None or not i.codes for i in manifest.items):
        raise Conflict("Для товара не найдены штрихкоды. Обновите каталог и сопоставление")
    if any(i.has_components for i in manifest.items):
        raise Conflict("У товара задан состав. Требуется сборка комплекта")
    if sum(i.quantity for i in manifest.items) > 1000:
        raise Conflict("В отправлении слишком много единиц для этой операции")
    # Serialize allocation of the same physical order positions across sibling shipments.
    tx.scalar(select(Order).where(Order.id == manifest.order_id).with_for_update())
    old_manual = tx.scalar(
        select(OrderUnit.id)
        .join(OrderLine, OrderLine.id == OrderUnit.line_id)
        .where(OrderLine.order_id == manifest.order_id, OrderLine.external_line_id.is_(None))
        .limit(1)
    )
    if old_manual:
        raise Conflict("Единицы заказа требуют сопоставления перед сборкой")
    planned = []
    for item in manifest.items:
        key = "packing:" + item.source_line_id
        line = tx.scalar(
            select(OrderLine)
            .where(OrderLine.order_id == manifest.order_id, OrderLine.external_line_id == key)
            .with_for_update()
        )
        if line is None:
            line = OrderLine(
                id=uuid4(),
                account_id=manifest.account_id,
                order_id=manifest.order_id,
                offer_id=item.offer_id,
                quantity_source="allocated",
                external_line_id=key,
                name=item.name,
                quantity=item.quantity,
            )
            tx.add(line)
            tx.flush()
        if line.offer_id != item.offer_id:
            raise Conflict("Сопоставление единиц изменилось. Требуется сверка распределения")
        current = tx.execute(
            select(ShipmentUnit, OrderUnit)
            .join(OrderUnit, OrderUnit.id == ShipmentUnit.unit_id)
            .where(
                ShipmentUnit.shipment_id == manifest.shipment_id,
                ShipmentUnit.released_at.is_(None),
                OrderUnit.line_id == line.id,
            )
            .order_by(OrderUnit.ordinal)
        ).all()
        if len(current) > item.quantity:
            raise Conflict("Состав изменился. Требуется сверка распределения единиц")
        other = tx.execute(
            select(ShipmentUnit, Shipment)
            .join(OrderUnit, OrderUnit.id == ShipmentUnit.unit_id)
            .join(Shipment, Shipment.id == ShipmentUnit.shipment_id)
            .where(
                OrderUnit.line_id == line.id,
                ShipmentUnit.released_at.is_(None),
                ShipmentUnit.shipment_id != manifest.shipment_id,
            )
        ).all()
        if any(
            s.state not in ("awaiting_assembly", "awaiting_dispatch", "in_delivery", "delivered")
            or s.needs_review
            for _, s in other
        ):
            raise Conflict("Единицы связаны с изменённым отправлением. Требуется сверка распределения")
        other_ids = {s.id for _, s in other}
        quantities = dict(
            tx.execute(
                select(ShipmentItem.shipment_id, ShipmentItem.quantity).where(
                    ShipmentItem.shipment_id.in_(other_ids),
                    ShipmentItem.source_line_id == item.source_line_id,
                    ShipmentItem.source_present.is_(True),
                )
            ).all()
        )
        if any(sum(1 for _, s in other if s.id == key) > quantities.get(key, 0) for key in other_ids):
            raise Conflict("Состав другого отправления изменился. Требуется сверка распределения")
        maximum = tx.scalar(select(func.max(OrderUnit.ordinal)).where(OrderUnit.line_id == line.id)) or 0
        for _ in range(item.quantity - len(current)):
            maximum += 1
            unit = OrderUnit(id=uuid4(), order_id=manifest.order_id, line_id=line.id, ordinal=maximum)
            allocation = ShipmentUnit(
                id=uuid4(), order_id=manifest.order_id, shipment_id=manifest.shipment_id, unit_id=unit.id
            )
            tx.add_all([unit, allocation])
            current.append((allocation, unit))
        line.quantity = max(line.quantity, maximum)
        planned.extend(
            PlannedUnit(unit.id, allocation.id, item.item_id, index)
            for index, (allocation, unit) in enumerate(current, 1)
        )
    # A removed position must not be silently reassigned to another product or shipment.
    allowed = {p.allocation_id for p in planned}
    extra = tx.scalar(
        select(ShipmentUnit.id)
        .where(
            ShipmentUnit.shipment_id == manifest.shipment_id,
            ShipmentUnit.released_at.is_(None),
            ShipmentUnit.id.not_in(allowed),
        )
        .limit(1)
    )
    if extra:
        raise Conflict("Состав изменился. Требуется сверка распределения единиц")
    tx.flush()
    return tuple(planned)
