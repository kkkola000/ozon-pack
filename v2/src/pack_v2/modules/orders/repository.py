from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import or_, select, tuple_, update

from pack_v2.infrastructure.models import AuditEvent
from pack_v2.integrations.public import ExternalReference, ReadError
from pack_v2.shared.errors import Conflict

from .models import Order, OrderRef, Shipment, ShipmentItem, ShipmentRef, ShipmentRelation

TERMINAL = ("delivered", "cancelled")


def _assign(row, name, value):
    if getattr(row, name) != value:
        setattr(row, name, value)
        return True
    return False


def _price(value):
    if value is None:
        return None
    try:
        if (
            not value.is_finite()
            or value < 0
            or value >= Decimal("100000000000000")
            or value.quantize(Decimal(".0001")) != value
        ):
            raise InvalidOperation
    except (InvalidOperation, AttributeError):
        raise ReadError("invalid_response") from None
    return value


def _aliases(tx, account_id, shipment_id, rows, snapshot):
    incoming = {}
    for ref in snapshot.references:
        key = (ref.kind, ref.value)
        incoming.setdefault(key, set()).add(ref.origin or "")
    observed = {v.removeprefix("reference:") for v in snapshot.fields_present if v.startswith("reference:")}
    known = {(r.kind, r.value): r for r in rows}
    changed = False
    for key, row in known.items():
        if row.authoritative:
            continue
        old = set(row.origins or [""])
        values = (old - observed) | incoming.get(key, set())
        if not values:
            tx.delete(row)
            changed = True
        elif values != old:
            row.origins = sorted(values)
            changed = True
    for key, origins in incoming.items():
        if key not in known:
            ref = next(r for r in snapshot.references if (r.kind, r.value) == key)
            tx.add(
                ShipmentRef(
                    account_id=account_id,
                    shipment_id=shipment_id,
                    kind=ref.kind,
                    value=ref.value,
                    authoritative=ref.authoritative,
                    origins=sorted(origins),
                )
            )
            changed = True
    return changed


def _physical_eligible(row):
    # A confirmed shipping failure is retryable; it does not erase product scans.
    # Normal scanner eligibility still rejects transition_failed in packing.manifest.
    return not row.needs_review and (
        row.state in ("awaiting_assembly", "awaiting_dispatch")
        or row.transition_failed
        and row.source_substatus == "ship_failed"
        and row.source_status in ("awaiting_packaging", "awaiting_deliver")
    )


def apply_page(tx, run, snapshots, catalog):
    account_id = run.account_id
    numbers = [s.source_id for s in snapshots]
    if len(numbers) != len(set(numbers)):
        raise ReadError("invalid_response")
    existing = dict(
        tx.execute(
            select(ShipmentRef.value, Shipment)
            .join(Shipment, Shipment.id == ShipmentRef.shipment_id)
            .where(
                ShipmentRef.account_id == account_id,
                ShipmentRef.kind == "posting_number",
                ShipmentRef.authoritative.is_(True),
                ShipmentRef.value.in_(numbers),
            )
        ).all()
    )
    identities = {(r.kind, r.value) for s in snapshots for r in s.order_references if r.authoritative}
    known_order_refs = list(
        tx.scalars(
            select(OrderRef).where(
                OrderRef.account_id == account_id,
                OrderRef.authoritative.is_(True),
                tuple_(OrderRef.kind, OrderRef.value).in_(identities),
            )
        )
    )
    order_index = {(r.kind, r.value): r.order_id for r in known_order_refs}
    order_ids = set(order_index.values()) | {s.order_id for s in existing.values()}
    orders = {o.id: o for o in tx.scalars(select(Order).where(Order.id.in_(order_ids)))}
    all_order_refs = {
        (r.order_id, r.kind, r.value)
        for r in tx.scalars(select(OrderRef).where(OrderRef.order_id.in_(order_ids)))
    }
    refs, items, relations = {}, {}, {}
    shipment_ids = [s.id for s in existing.values()]
    for row in tx.scalars(select(ShipmentRef).where(ShipmentRef.shipment_id.in_(shipment_ids))):
        refs.setdefault(row.shipment_id, []).append(row)
    for row in tx.scalars(select(ShipmentItem).where(ShipmentItem.shipment_id.in_(shipment_ids))):
        items.setdefault(row.shipment_id, {})[row.source_line_id] = row
    for row in tx.scalars(select(ShipmentRelation).where(ShipmentRelation.shipment_id.in_(shipment_ids))):
        relations.setdefault(row.shipment_id, []).append(row)
    groups = {
        (s.source_id, line.source_line_id): line.product_references for s in snapshots for line in s.items
    }
    matches = catalog.match_offers(tx, account_id, groups)
    changed_orders = set()
    for snapshot in snapshots:
        if snapshot.source.account_id != account_id or not any(
            r.kind == "posting_number" and r.value == snapshot.source_id and r.authoritative
            for r in snapshot.references
        ):
            raise ReadError("invalid_response")
        previous = existing.get(snapshot.source_id)
        if previous and previous.observed_at and snapshot.observed_at < previous.observed_at:
            # A slow list/get started before readiness must not roll back a newer GET.
            continue
        identity_refs = [r for r in snapshot.order_references if r.authoritative]
        if not identity_refs:
            raise ReadError("incomplete")
        candidates = {
            order_index[(r.kind, r.value)] for r in identity_refs if (r.kind, r.value) in order_index
        }
        if len(candidates) > 1:
            raise Conflict("Идентификаторы Ozon указывают на разные заказы")
        saved = existing.get(snapshot.source_id)
        if saved and candidates != {saved.order_id}:
            raise Conflict("Ozon изменил принадлежность отправления. Нужна проверка заказа")
        if candidates:
            order = orders[next(iter(candidates))]
        else:
            order = Order(id=uuid4(), account_id=account_id, revision=0)
            orders[order.id] = order
            tx.add(order)
        for ref in identity_refs:
            key = (order.id, ref.kind, ref.value)
            if key not in all_order_refs:
                tx.add(
                    OrderRef(
                        account_id=account_id,
                        order_id=order.id,
                        kind=ref.kind,
                        value=ref.value,
                        authoritative=True,
                    )
                )
                all_order_refs.add(key)
                order_index[(ref.kind, ref.value)] = order.id
                changed_orders.add(order.id)
        fresh = saved is None
        if fresh:
            saved = Shipment(
                id=uuid4(),
                account_id=account_id,
                order_id=order.id,
                revision=0,
                state="unknown",
                needs_review=False,
                transition_failed=False,
                requirements_complete=False,
            )
            tx.add(saved)
        old_eligible = _physical_eligible(saved)
        was_split = saved.state == "split"
        before_status = (saved.source_status, saved.source_substatus, saved.state)
        changed = fresh
        changed |= _assign(saved, "source_status", snapshot.source_status)
        if "substatus" in snapshot.fields_present or fresh:
            changed |= _assign(saved, "source_substatus", snapshot.source_substatus)
            changed |= _assign(saved, "transition_failed", snapshot.transition_failed)
        state = (
            "unknown" if saved.transition_failed and snapshot.state == "awaiting_dispatch" else snapshot.state
        )
        changed |= _assign(saved, "state", state)
        if fresh or before_status != (saved.source_status, saved.source_substatus, saved.state):
            tx.add(
                AuditEvent(
                    kind="shipment_status_observed",
                    subject_id=saved.id,
                    details={
                        "account_id": str(account_id),
                        "order_id": str(order.id),
                        "run_id": str(run.id),
                        "source_status": saved.source_status,
                        "source_substatus": saved.source_substatus,
                        "state": saved.state,
                        "observed_at": snapshot.observed_at.isoformat(),
                    },
                )
            )
        for field in (
            "deadline",
            "processing_started_at",
            "warehouse_id",
            "warehouse_name",
            "package_count_hint",
        ):
            if field in snapshot.fields_present:
                changed |= _assign(saved, field, getattr(snapshot, field))
        if "parent" in snapshot.fields_present:
            parent = snapshot.parent_reference.value if snapshot.parent_reference else None
            if parent == snapshot.source_id:
                raise Conflict("Отправление не может быть родителем самого себя")
            if _assign(saved, "parent_source_number", parent) or (
                parent is None and saved.parent_id is not None
            ):
                saved.parent_id = None
                changed = True
        changed |= _assign(saved, "needs_review", bool(saved.layout_review))
        changed |= _assign(saved, "requirements_complete", snapshot.requirements_complete)
        changed |= _aliases(tx, account_id, saved.id, refs.get(saved.id, []), snapshot)
        if "related" in snapshot.fields_present:
            incoming = {r.value for r in snapshot.related_references}
            current = {r.related_number for r in relations.get(saved.id, [])}
            if incoming != current:
                for row in relations.get(saved.id, []):
                    if row.related_number not in incoming:
                        tx.delete(row)
                tx.add_all(
                    ShipmentRelation(account_id=account_id, shipment_id=saved.id, related_number=value)
                    for value in incoming - current
                )
                changed = True
        line_ids = [line.source_line_id for line in snapshot.items]
        if not line_ids or len(set(line_ids)) != len(line_ids):
            raise ReadError("invalid_response")
        current_items = items.get(saved.id, {})
        old_physical = sorted(
            (r.source_line_id, r.quantity, str(r.offer_id), r.match_state)
            for r in current_items.values()
            if r.source_present
        )
        for line in snapshot.items:
            item = current_items.get(line.source_line_id)
            new = item is None
            if new:
                item = ShipmentItem(
                    id=uuid4(),
                    account_id=account_id,
                    shipment_id=saved.id,
                    source_line_id=line.source_line_id,
                    name=line.name or "Товар " + line.source_line_id,
                    revision=0,
                    required_fields=[],
                    optional_fields=[],
                    source_present=True,
                )
                tx.add(item)
            dirty = new
            dirty |= _assign(item, "quantity", line.quantity)
            dirty |= _assign(item, "source_present", True)
            for field, attr in (("name", "name"), ("price", "unit_price"), ("currency", "currency")):
                if field in line.fields_present and not (field == "name" and not line.name):
                    value = getattr(line, attr)
                    dirty |= _assign(item, field, _price(value) if field == "price" else value)
            if "article" in line.fields_present:
                article = next((r.value for r in line.product_references if r.kind == "offer_id"), None)
                dirty |= _assign(item, "article", article)
            serialized = sorted(
                [{"kind": r.kind, "value": r.value} for r in line.product_references],
                key=lambda r: (r["kind"], r["value"]),
            )
            if "article" not in line.fields_present and item.article:
                serialized.append({"kind": "offer_id", "value": item.article})
                serialized.sort(key=lambda r: (r["kind"], r["value"]))
            dirty |= _assign(item, "references", serialized)
            match = matches[(snapshot.source_id, line.source_line_id)]
            dirty |= _assign(item, "offer_id", match.offer_id)
            dirty |= _assign(item, "match_state", match.state)
            dirty |= _assign(
                item, "required_fields", sorted(set(item.required_fields) | set(line.required_fields))
            )
            dirty |= _assign(
                item, "optional_fields", sorted(set(item.optional_fields) | set(line.optional_fields))
            )
            if dirty:
                item.revision += 1
                changed = True
        for key, item in current_items.items():
            if key not in line_ids and item.source_present:
                item.source_present = False
                item.revision += 1
                changed = True
        new_physical = sorted(
            (
                r.source_line_id,
                r.quantity,
                str(matches[(snapshot.source_id, r.source_line_id)].offer_id),
                matches[(snapshot.source_id, r.source_line_id)].state,
            )
            for r in snapshot.items
        )
        if fresh or old_physical != new_physical or old_eligible != _physical_eligible(saved):
            saved.assembly_revision = (saved.assembly_revision or 0) + 1
        if not fresh:
            from .layout import observe

            observe(tx, saved, snapshot, was_split=was_split, physical_changed=old_physical != new_physical)
        saved.observed_at = snapshot.observed_at
        saved.checked_at = saved.synced_at = datetime.now(UTC)
        saved.last_seen_run_id = run.id
        if changed:
            saved.revision += 1
            changed_orders.add(order.id)
    for key in changed_orders:
        orders[key].revision += 1
    tx.flush()


def candidates(tx, account_id, run_id, after=None):
    query = (
        select(Shipment.id, ShipmentRef.value)
        .join(ShipmentRef, ShipmentRef.shipment_id == Shipment.id)
        .where(
            Shipment.account_id == account_id,
            Shipment.state.not_in(TERMINAL),
            (Shipment.last_seen_run_id.is_(None)) | (Shipment.last_seen_run_id != run_id),
            ShipmentRef.kind == "posting_number",
            ShipmentRef.authoritative.is_(True),
        )
    )
    if after:
        query = query.where(Shipment.id > after)
    return tx.execute(query.order_by(Shipment.id).limit(100)).all()


def missing(tx, run, shipment_id):
    saved = tx.get(Shipment, shipment_id)
    if saved is None or saved.account_id != run.account_id:
        raise ReadError("invalid_response")
    if not saved.needs_review:
        saved.needs_review = True
        saved.assembly_revision += 1
        saved.revision += 1
        tx.get(Order, saved.order_id).revision += 1
    saved.checked_at = datetime.now(UTC)


def complete(tx, run, order_id=None):
    # Work on relationship columns only; do not materialize the entire order archive.
    rows = tx.execute(
        select(Shipment.id, Shipment.order_id, Shipment.parent_source_number, Shipment.parent_id).where(
            Shipment.account_id == run.account_id,
            True if order_id is None else Shipment.order_id == order_id,
            or_(Shipment.parent_source_number.is_not(None), Shipment.parent_id.is_not(None)),
        )
    ).all()
    wanted = {r.parent_source_number for r in rows if r.parent_source_number is not None}
    targets = {
        r.value: (r.id, r.order_id)
        for r in tx.execute(
            select(ShipmentRef.value, Shipment.id, Shipment.order_id)
            .join(Shipment, Shipment.id == ShipmentRef.shipment_id)
            .where(
                ShipmentRef.account_id == run.account_id,
                ShipmentRef.kind == "posting_number",
                ShipmentRef.authoritative.is_(True),
                ShipmentRef.value.in_(wanted),
            )
        )
    }
    parents = {
        r.id: targets.get(r.parent_source_number, (None, None))[0]
        if r.parent_source_number is not None
        else r.parent_id
        for r in rows
    }
    for row in rows:
        target = targets.get(row.parent_source_number)
        if target and target[1] != row.order_id:
            raise Conflict("Родитель отправления относится к другому заказу")
    done = set()
    for key in parents:
        path = set()
        current = key
        while current is not None and current not in done:
            if current in path:
                raise Conflict("Ozon вернул циклические связи отправлений")
            path.add(current)
            current = parents.get(current)
        done.update(path)
    changed_orders = set()
    for row in rows:
        if row.parent_source_number is not None and row.parent_id != parents[row.id]:
            tx.execute(
                update(Shipment)
                .where(Shipment.id == row.id)
                .values(parent_id=parents[row.id], revision=Shipment.revision + 1)
            )
            changed_orders.add(row.order_id)
    if changed_orders:
        tx.execute(update(Order).where(Order.id.in_(changed_orders)).values(revision=Order.revision + 1))


def relink_batch(tx, run, catalog, after=None):
    query = select(ShipmentItem).where(ShipmentItem.account_id == run.account_id)
    if after:
        query = query.where(ShipmentItem.id > after)
    items = tx.scalars(query.order_by(ShipmentItem.id).limit(200)).all()
    groups = {
        r.id: tuple(ExternalReference(v["kind"], v["value"], True) for v in r.references) for r in items
    }
    matches = catalog.match_offers(tx, run.account_id, groups)
    changed_shipments = set()
    for row in items:
        match = matches[row.id]
        if (row.offer_id, row.match_state) != (match.offer_id, match.state):
            row.offer_id, row.match_state = match.offer_id, match.state
            row.revision += 1
            changed_shipments.add(row.shipment_id)
    changed_orders = set()
    for shipment in tx.scalars(select(Shipment).where(Shipment.id.in_(changed_shipments))):
        shipment.revision += 1
        shipment.assembly_revision += 1
        changed_orders.add(shipment.order_id)
    for order in tx.scalars(select(Order).where(Order.id.in_(changed_orders))):
        order.revision += 1
    return items[-1].id if items else None
