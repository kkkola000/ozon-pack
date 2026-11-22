"""Order-owned drafts distribute stable physical units, never quantities guessed by the browser."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from pack_v2.shared.errors import Conflict, DomainError

from .models import PackingLayout

MAX_PACKAGES = 100


def validate(groups, units):
    if not isinstance(groups, list) or not 1 <= len(groups) <= MAX_PACKAGES:
        raise DomainError("Укажите от одного до ста отправлений")
    expected = {str(u.unit_id) for u in units}
    seen = []
    for group in groups:
        if not isinstance(group, list) or not group or any(not isinstance(v, str) for v in group):
            raise DomainError("Пустое отправление сохранить нельзя")
        seen.extend(group)
    if len(seen) != len(set(seen)) or set(seen) != expected:
        raise DomainError("Каждая единица товара должна находиться ровно в одном отправлении")
    return [sorted(group) for group in groups]


def groups(tx, manifest, units, *, automatic=False):
    row = tx.get(PackingLayout, manifest.shipment_id)
    default = (
        [[str(u.unit_id)] for u in units]
        if automatic and 1 < len(units) <= MAX_PACKAGES
        else [[str(u.unit_id) for u in units]]
    )
    if row and (row.source_revision != manifest.assembly_revision or row.basis != manifest.basis):
        return validate(default, units)  # A fresh assembly never reuses a stale distribution.
    return validate(row.groups if row else default, units)


def save(tx, manifest, units, desired):
    if manifest.state != "awaiting_assembly" or not manifest.eligible or len(units) < 2:
        raise Conflict("Ozon разрешает разделение при сборке отправления с двумя и более единицами")
    value = validate(desired, units)
    row = tx.get(PackingLayout, manifest.shipment_id)
    if row is None:
        row = PackingLayout(shipment_id=manifest.shipment_id, revision=0)
        tx.add(row)
    row.source_revision, row.basis, row.groups = manifest.assembly_revision, manifest.basis, value
    row.revision += 1
    tx.flush()
    return value


def confirm(tx, original, snapshots, mapping, catalog, source):
    """Apply only observed child shipments and move their proven physical allocations atomically."""
    from . import repository
    from .models import OrderRef, OrderSync, Shipment, ShipmentItem, ShipmentRef, ShipmentUnit

    refs = {
        (r.kind, r.value)
        for r in tx.scalars(
            select(OrderRef).where(OrderRef.order_id == original.order_id, OrderRef.authoritative.is_(True))
        )
    }
    if any(
        not ({(r.kind, r.value) for r in s.order_references if r.authoritative} & refs)
        or not {(r.kind, r.value) for r in s.order_references if r.authoritative} <= refs
        or s.source.account_id != original.account_id
        for s in snapshots
    ):
        raise Conflict("Новые отправления принадлежат другому заказу или кабинету")
    run = OrderSync(
        id=uuid4(),
        account_id=original.account_id,
        credential_revision=source.credential_revision,
        job_id=None,
        mode="working",
        since=datetime.now(UTC),
        until=datetime.now(UTC),
        status="succeeded",
    )
    # Reuse the existing source observation operation, with an actual durable sync job.
    from pack_v2.infrastructure.jobs import enqueue
    from pack_v2.infrastructure.models import Job

    run.job_id = enqueue(tx, "orders.sync", "orders.split.observed:" + str(run.id), {"run_id": str(run.id)})
    tx.get(Job, run.job_id).status = "succeeded"
    run.finished_at = datetime.now(UTC)
    tx.add(run)
    tx.flush()
    repository.apply_page(tx, run, snapshots, catalog)
    tx.flush()
    result = []
    for snapshot in snapshots:
        child = tx.scalar(
            select(Shipment)
            .join(ShipmentRef, ShipmentRef.shipment_id == Shipment.id)
            .where(
                Shipment.account_id == original.account_id,
                ShipmentRef.kind == "posting_number",
                ShipmentRef.value == snapshot.source_id,
            )
        )
        if child.order_id != original.order_id:
            raise Conflict("Не подтверждена принадлежность дочернего отправления")
        allocated = {
            a.unit_id: a
            for a in tx.scalars(
                select(ShipmentUnit).where(
                    ShipmentUnit.shipment_id == child.id, ShipmentUnit.released_at.is_(None)
                )
            )
        }
        wanted = mapping[snapshot.source_id]
        wanted_ids = {UUID(u["unit_id"]) for u in wanted}
        original_allocations = {
            a.unit_id: a
            for a in tx.scalars(
                select(ShipmentUnit).where(
                    ShipmentUnit.shipment_id == original.id, ShipmentUnit.released_at.is_(None)
                )
            )
        }
        if child.id != original.id and set(allocated) - wanted_ids:
            raise Conflict("Новые отправления уже получили другое распределение единиц")
        entries = []
        for unit in wanted:
            uid = UUID(unit["unit_id"])
            old = original_allocations.get(uid)
            if old is None:
                raise Conflict("Физическая единица уже перемещена. Требуется сверка")
            item = tx.scalar(
                select(ShipmentItem).where(
                    ShipmentItem.shipment_id == child.id,
                    ShipmentItem.source_line_id == unit["source_line_id"],
                    ShipmentItem.source_present.is_(True),
                )
            )
            if item is None:
                raise Conflict("Не подтверждён товар нового отправления")
            allocation = allocated.get(uid)
            if child.id != original.id:
                old.released_at = datetime.now(UTC)
                tx.flush()
                allocation = ShipmentUnit(
                    id=uuid4(), order_id=original.order_id, shipment_id=child.id, unit_id=uid
                )
                tx.add(allocation)
                tx.flush()
            entries.append({**unit, "allocation_id": str(allocation.id), "item_id": str(item.id)})
        if child.id != original.id:
            child.parent_id = original.id
            child.parent_source_number = tx.scalar(
                select(ShipmentRef.value).where(
                    ShipmentRef.shipment_id == original.id,
                    ShipmentRef.kind == "posting_number",
                    ShipmentRef.authoritative.is_(True),
                )
            )
        point = OrderSync(
            id=uuid4(),
            account_id=original.account_id,
            credential_revision=source.credential_revision,
            mode="point",
            target_shipment_id=child.id,
            expected_shipment_revision=child.revision,
            status="succeeded",
            since=run.since,
            until=run.until,
            finished_at=datetime.now(UTC),
        )
        point.job_id = enqueue(
            tx, "orders.sync", "orders.split.point:" + str(point.id), {"run_id": str(point.id)}
        )
        point_job = tx.get(Job, point.job_id)
        point_job.status, point_job.finished_at = "succeeded", datetime.now(UTC)
        tx.add(point)
        tx.flush()
        result.append((child, snapshot, entries, point.id))
    if original.id not in {child.id for child, *_ in result}:
        original.state, original.needs_review = "split", False
        original.assembly_revision += 1
        original.observed_at = max(s.observed_at for s in snapshots)
    draft = tx.get(PackingLayout, original.id)
    if draft:
        tx.delete(draft)
    tx.flush()
    return result


def observe(tx, saved, snapshot, *, was_split, physical_changed):
    """Revoke old distribution proofs when fresh source facts no longer describe it."""
    from .models import Shipment, ShipmentRef

    root_id = saved.parent_id or saved.id
    peers = list(
        tx.scalars(select(Shipment).where((Shipment.id == root_id) | (Shipment.parent_id == root_id)))
    )
    if len(peers) < 2:
        return
    child_ids = [p.id for p in peers if p.id != root_id or not was_split and p.state != "split"]
    known = set(
        tx.scalars(
            select(ShipmentRef.value).where(
                ShipmentRef.shipment_id.in_(child_ids),
                ShipmentRef.kind == "posting_number",
                ShipmentRef.authoritative.is_(True),
            )
        )
    )
    related = {r.value for r in snapshot.related_references}
    expected = known - {snapshot.source_id}
    if was_split and "related" in snapshot.fields_present and expected <= related:
        saved.state = "split"  # Ozon may retain the aggregate original posting after a successful split.
        return
    missing = "related" in snapshot.fields_present and not expected <= related
    if was_split or physical_changed or missing:
        for peer in peers:
            if not peer.layout_review:
                peer.layout_review = peer.needs_review = True
                peer.assembly_revision += 1
