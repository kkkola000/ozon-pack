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
                return occupied.id, False
            raise Conflict("Данный Честный знак уже используется в заказе: " + occupied.posting_number)
        previous = tx.scalar(
            select(MarkingAssignment)
            .where(MarkingAssignment.unit_id == binding.unit_id, MarkingAssignment.released_at.is_(None))
            .with_for_update()
        )
        if previous:
            if not may_replace:
                raise Conflict(
                    "К этой единице уже привязан Честный знак. Сначала требуется сверка прежнего кода"
                )
            previous.released_at, previous.release_reason = datetime.now(UTC), "provider_rejected"
            previous.revision += 1
            tx.flush()
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
            )
        ).all()
        return {
            a.unit_id: MarkValue(
                a.id, a.unit_id, m.full_fingerprint, self.box.open(m.encrypted_code).decode()
            )
            for a, m in rows
        }

    @staticmethod
    def relocate(tx, binding):
        """Only a confirmed split may transfer the reservation of the same physical unit."""
        old = tx.scalar(
            select(MarkingAssignment)
            .where(MarkingAssignment.unit_id == binding.unit_id, MarkingAssignment.released_at.is_(None))
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
            tx.execute(
                insert(MarkingTransmission)
                .values(id=uuid4(), assignment_id=assignment_id, batch_id=batch_id, status=status)
                .on_conflict_do_update(
                    index_elements=[MarkingTransmission.assignment_id, MarkingTransmission.batch_id],
                    set_={"status": status},
                )
            )

    @staticmethod
    def proven(tx, batch_ids, assignment_ids):
        return set(
            tx.scalars(
                select(MarkingTransmission.assignment_id).where(
                    MarkingTransmission.batch_id.in_(batch_ids),
                    MarkingTransmission.assignment_id.in_(assignment_ids),
                    MarkingTransmission.status == "accepted",
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
