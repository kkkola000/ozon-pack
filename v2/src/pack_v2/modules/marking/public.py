"""A code's identity, current assignment and provider acceptance are separate facts."""

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from pack_v2.infrastructure.models import AuditEvent
from pack_v2.shared.errors import Conflict, DomainError

from .codes import equivalent, looks_like, parse
from .models import MarkingAssignment, MarkingTransmission, MarkingUnit


@dataclass(frozen=True)
class UnitBinding:
    account_id: UUID
    order_id: UUID
    shipment_id: UUID
    unit_id: UUID
    allocation_id: UUID
    posting_number: str


@dataclass(frozen=True)
class MarkValue:
    assignment_id: UUID
    unit_id: UUID
    fingerprint: str
    raw: str = field(repr=False)


class Marking:
    def __init__(self, box, secret):
        self.box, self.secret = box, secret.encode()

    def _digest(self, value):
        return hmac.new(
            self.secret, json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode(), hashlib.sha256
        ).hexdigest()

    def capture(self, tx, actor_id, binding, raw, *, expected_gtins=(), may_replace=False):
        try:
            parsed = parse(raw)
        except ValueError as error:
            raise DomainError(str(error)) from None
        if expected_gtins and parsed.gtin not in expected_gtins:
            raise DomainError("GTIN Честного знака не соответствует товару")
        identity = self._digest([parsed.gtin, parsed.serial])
        fingerprint = self._digest(parsed.fields)
        tx.execute(
            insert(MarkingUnit)
            .values(
                id=uuid4(),
                identity=identity,
                full_fingerprint=fingerprint,
                gtin=parsed.gtin,
                serial=parsed.serial,
                encrypted_code=self.box.seal(parsed.raw.encode()),
            )
            .on_conflict_do_nothing(index_elements=[MarkingUnit.identity])
        )
        mark = tx.scalar(select(MarkingUnit).where(MarkingUnit.identity == identity).with_for_update())
        if mark.full_fingerprint != fingerprint:
            raise Conflict("У Честного знака с таким GTIN и серийным номером отличаются полные данные")
        occupied = tx.scalar(
            select(MarkingAssignment).where(
                MarkingAssignment.mark_id == mark.id, MarkingAssignment.released_at.is_(None)
            )
        )
        if occupied:
            if occupied.unit_id == binding.unit_id and occupied.shipment_id == binding.shipment_id:
                if occupied.retired_at is None:
                    return occupied.id, False
            else:
                raise Conflict("Данный Честный знак уже используется в заказе: " + occupied.posting_number)
        previous = tx.scalar(
            select(MarkingAssignment)
            .where(
                MarkingAssignment.unit_id == binding.unit_id,
                MarkingAssignment.released_at.is_(None),
                MarkingAssignment.retired_at.is_(None),
            )
            .with_for_update()
        )
        if previous:
            if not may_replace:
                raise Conflict(
                    "К этой единице уже привязан Честный знак. Сначала требуется сверка прежнего кода"
                )
            self.retire(tx, previous.id, actor_id, "replacement_pending")
        if occupied:
            occupied.retired_at = None
            occupied.revision += 1
            tx.add(AuditEvent(actor_id=actor_id, kind="marking_reactivated", subject_id=occupied.id))
            tx.flush()
            return occupied.id, True
        assignment = MarkingAssignment(
            id=uuid4(),
            mark_id=mark.id,
            account_id=binding.account_id,
            order_id=binding.order_id,
            shipment_id=binding.shipment_id,
            unit_id=binding.unit_id,
            allocation_id=binding.allocation_id,
            posting_number=binding.posting_number,
            actor_id=actor_id,
        )
        tx.add(assignment)
        tx.add(
            AuditEvent(
                actor_id=actor_id,
                kind="marking_assigned",
                subject_id=assignment.id,
                details={"shipment_id": str(binding.shipment_id), "unit_id": str(binding.unit_id)},
            )
        )
        tx.flush()
        return assignment.id, True

    def values(self, tx, shipment_id, unit_ids):
        rows = tx.execute(
            select(MarkingAssignment, MarkingUnit)
            .join(MarkingUnit, MarkingUnit.id == MarkingAssignment.mark_id)
            .where(
                MarkingAssignment.shipment_id == shipment_id,
                MarkingAssignment.unit_id.in_(unit_ids),
                MarkingAssignment.released_at.is_(None),
                MarkingAssignment.retired_at.is_(None),
            )
        ).all()
        return {
            a.unit_id: MarkValue(
                a.id, a.unit_id, m.full_fingerprint, self.box.open(m.encrypted_code).decode()
            )
            for a, m in rows
        }

    @staticmethod
    def retire(tx, assignment_id, actor_id, reason):
        """Remove from the current packet; keep the global reservation until proof."""
        row = tx.get(MarkingAssignment, assignment_id, with_for_update=True)
        if row.released_at is not None or row.retired_at is not None:
            return
        row.retired_at = datetime.now(UTC)
        row.revision += 1
        tx.add(
            AuditEvent(
                actor_id=actor_id,
                kind="marking_retired",
                subject_id=row.id,
                details={"reason": reason, "shipment_id": str(row.shipment_id)},
            )
        )
        tx.flush()

    def finalize_replacements(self, tx, shipment_id, assignment_ids, payload):
        """Called only after exact provider acceptance of the complete packet."""
        current = list(
            tx.scalars(
                select(MarkingAssignment).where(
                    MarkingAssignment.id.in_(assignment_ids),
                    MarkingAssignment.shipment_id == shipment_id,
                    MarkingAssignment.retired_at.is_(None),
                    MarkingAssignment.released_at.is_(None),
                )
            )
        )
        native_codes = [
            m["mark"]
            for p in payload["products"]
            for e in p["exemplars"]
            for m in e.get("marks", [])
            if m["mark_type"] == "mandatory_mark"
        ]
        old = tx.execute(
            select(MarkingAssignment, MarkingUnit)
            .join(MarkingUnit, MarkingUnit.id == MarkingAssignment.mark_id)
            .where(
                MarkingAssignment.shipment_id == shipment_id,
                MarkingAssignment.unit_id.in_([a.unit_id for a in current]),
                MarkingAssignment.retired_at.is_not(None),
                MarkingAssignment.released_at.is_(None),
            )
            .with_for_update(of=MarkingAssignment)
        ).all()
        for row, mark in old:
            raw = self.box.open(mark.encrypted_code).decode()
            if any(equivalent(raw, code) for code in native_codes):
                continue
            row.released_at, row.release_reason = datetime.now(UTC), "replacement_confirmed"
            row.revision += 1
            tx.add(
                AuditEvent(
                    kind="marking_released",
                    subject_id=row.id,
                    details={"reason": row.release_reason, "shipment_id": str(shipment_id)},
                )
            )

    @staticmethod
    def accepted(tx, assignment_id):
        return (
            tx.scalar(
                select(MarkingTransmission.id)
                .join(MarkingAssignment, MarkingAssignment.id == MarkingTransmission.assignment_id)
                .where(
                    MarkingTransmission.assignment_id == assignment_id,
                    MarkingTransmission.status == "accepted",
                    MarkingTransmission.assignment_revision == MarkingAssignment.revision,
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    def relocate(tx, binding):
        """Only a confirmed split may transfer the reservation of the same physical unit."""
        old = tx.scalar(
            select(MarkingAssignment)
            .where(
                MarkingAssignment.unit_id == binding.unit_id,
                MarkingAssignment.released_at.is_(None),
                MarkingAssignment.retired_at.is_(None),
            )
            .with_for_update()
        )
        if old is None:
            return None
        if old.account_id != binding.account_id or old.order_id != binding.order_id:
            raise Conflict("Нельзя перенести Честный знак в другой заказ или кабинет")
        if old.shipment_id == binding.shipment_id and old.allocation_id == binding.allocation_id:
            return old.id
        old.released_at, old.release_reason = datetime.now(UTC), "shipment_split"
        tx.flush()
        row = MarkingAssignment(
            id=uuid4(),
            mark_id=old.mark_id,
            account_id=binding.account_id,
            order_id=binding.order_id,
            shipment_id=binding.shipment_id,
            unit_id=binding.unit_id,
            allocation_id=binding.allocation_id,
            posting_number=binding.posting_number,
            actor_id=old.actor_id,
        )
        tx.add(row)
        tx.flush()
        tx.add(
            AuditEvent(
                kind="marking_relocated",
                subject_id=row.id,
                details={
                    "previous_assignment_id": str(old.id),
                    "shipment_id": str(row.shipment_id),
                    "unit_id": str(row.unit_id),
                },
            )
        )
        return row.id

    @staticmethod
    def transmissions(tx, batch_id, assignment_ids, status):
        for assignment_id in assignment_ids:
            revision = tx.get(MarkingAssignment, assignment_id).revision
            tx.execute(
                insert(MarkingTransmission)
                .values(
                    id=uuid4(),
                    assignment_id=assignment_id,
                    batch_id=batch_id,
                    status=status,
                    assignment_revision=revision,
                )
                .on_conflict_do_update(
                    index_elements=[MarkingTransmission.assignment_id, MarkingTransmission.batch_id],
                    set_={"status": status, "assignment_revision": revision},
                )
            )

    @staticmethod
    def proven(tx, batch_ids, assignment_ids):
        return set(
            tx.scalars(
                select(MarkingTransmission.assignment_id)
                .join(MarkingAssignment, MarkingAssignment.id == MarkingTransmission.assignment_id)
                .where(
                    MarkingTransmission.batch_id.in_(batch_ids),
                    MarkingTransmission.assignment_id.in_(assignment_ids),
                    MarkingTransmission.status == "accepted",
                    MarkingTransmission.assignment_revision == MarkingAssignment.revision,
                )
            )
        )

    @staticmethod
    def equivalent(left, right):
        return equivalent(left, right)

    @staticmethod
    def validate(raw):
        return parse(raw)

    @staticmethod
    def looks_like(raw):
        return looks_like(raw)

    @staticmethod
    def gtins(values):
        result = set()
        for value in values:
            if value.isascii() and value.isdigit() and len(value) in (8, 12, 13, 14):
                expected = (
                    10
                    - sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(value[:-1]))) % 10
                ) % 10
                if expected == int(value[-1]):
                    result.add(value.zfill(14))
        return tuple(sorted(result))


__all__ = ["Marking", "UnitBinding", "MarkValue"]
