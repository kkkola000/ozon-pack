"""Durable split execution: one write intent, then authoritative child observations."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from pack_v2.infrastructure.jobs import Queue
from pack_v2.infrastructure.models import AuditEvent
from pack_v2.integrations.public import ReadError
from pack_v2.shared.errors import Conflict

from .models import ACTIVE, AssemblyLine, AssemblySession, AssemblyUnit


class SplitExecution:
    def __init__(self, readiness):
        self.r = readiness

    def run(self, job, source, manifest, evidence, started, cancelled, layout, targets):
        if cancelled and not started:
            raise Conflict("Локальная сборка отменена до разделения")
        if not started:
            with self.r.connections.instance_client(source) as client:
                posting = self.r.instances.check_dispatch(client, manifest, evidence)
                self.r.posting_matches(posting, manifest)
            if posting["status"] != "awaiting_packaging":
                raise Conflict("Ozon уже собрал отправление. Разделить его повторной сборкой нельзя")
            packages = self.r.instances.split_products(evidence, layout)
            payload = {"posting_number": manifest.posting_number, "packages": packages}

            def intent(tx, row, session, *args):
                if session.status == "cancelled" or row.ship_started_at:
                    raise Conflict("Повторное разделение без подтверждённого результата запрещено")
                row.ship_started_at, row.send_count = datetime.now(UTC), row.send_count + 1
                tx.add(
                    AuditEvent(
                        kind="assembly_split_started",
                        subject_id=row.id,
                        details={"shipment_id": str(row.shipment_id), "package_count": len(layout)},
                    )
                )

            with self.r.connections.shipping_client(source) as client:
                self.r.write(job, intent)
                try:
                    targets = list(client.split(payload))
                except ReadError as error:
                    if error.status == 429:

                        def rate_limited(tx, row, *args):
                            if row.send_count < 3:
                                row.ship_started_at = None

                        self.r.write(job, rate_limited)
                    raise
            self.r.write(job, lambda tx, row, *args: setattr(row, "split_targets", list(targets)))
            self.r.fail(job, "Ozon разделяет заказ. Проверяем новые отправления", retry=True)
            return
        if not targets:
            # The acknowledgement can be lost. Recover identities from the original posting, never resend.
            def original(tx, row, session, *args):
                return session.posting_number

            original_number = self.r.write(job, original)
            with self.r.connections.instance_client(source) as client:
                posting = client.posting(original_number)
            related = (posting.get("related_postings") or {}).get("related_posting_numbers")
            if not isinstance(related, list):
                raise ReadError("incomplete")
            targets = list(dict.fromkeys(related))
            if len(targets) == len(layout) - 1:
                targets.append(original_number)
            if len(targets) != len(layout) or any(
                not isinstance(v, str) or not v or len(v) > 256 for v in targets
            ):
                raise ReadError("incomplete")
            self.r.write(job, lambda tx, row, *args: setattr(row, "split_targets", list(targets)))
        with self.r.connections.reader_for_sync(source) as reader:
            snapshots = [reader.get_shipment(number) for number in targets]
        if any(s is None for s in snapshots) or len({s.source_id for s in snapshots}) != len(layout):
            raise ReadError("incomplete")
        with self.r.connections.instance_client(source) as client:
            proofs = self.r.instances.prove_split(client, evidence, layout, snapshots)

        def accept(tx, row, session, source, *args):
            numbers = {s.source_id for s in snapshots}
            # A different operator must not receive silently reassigned scans or codes.
            child_ids = self.r.orders.packing_ids(tx, row.account_id, numbers)
            busy = tx.scalar(
                select(AssemblySession.id)
                .where(
                    AssemblySession.shipment_id.in_(child_ids),
                    AssemblySession.id != session.id,
                    AssemblySession.status.in_(ACTIVE),
                )
                .limit(1)
            )
            if busy:
                raise Conflict("Новое отправление уже открыто другим сборщиком. Требуется сверка")
            children = self.r.orders.packing_confirm_split(
                tx,
                row.shipment_id,
                snapshots,
                {number: proof["mapping"] for number, proof in proofs.items()},
                source,
            )
            contexts = self.r.instances.accept_split(tx, children, proofs, source)
            original_checks = {
                u.unit_id: u.scanned_at
                for u in tx.scalars(select(AssemblyUnit).where(AssemblyUnit.session_id == session.id))
            }
            result = []
            for child, observed, units, run_id in children:
                child_manifest = self.r.orders.packing_manifest(tx, child.id)
                proof_session = AssemblySession(
                    id=uuid4(),
                    user_id=session.user_id,
                    account_id=row.account_id,
                    order_id=row.order_id,
                    shipment_id=child.id,
                    credential_revision=row.credential_revision,
                    source_revision=child_manifest.assembly_revision,
                    basis=child_manifest.basis,
                    posting_number=observed.source_id,
                    status="ready",
                    lease_token=uuid4(),
                    lease_until=datetime.now(UTC),
                    refresh_run_id=run_id,
                    instance_context_id=contexts[child.id],
                    verified_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                )
                tx.add(proof_session)
                tx.flush()
                for item in child_manifest.items:
                    tx.add(
                        AssemblyLine(
                            session_id=proof_session.id,
                            item_id=item.item_id,
                            name=item.name,
                            article=item.article,
                            source_line_id=item.source_line_id,
                            quantity=item.quantity,
                            references=list(item.references),
                            marking_hint=item.marking_hint,
                        )
                    )
                tx.flush()
                for index, entry in enumerate(units, 1):
                    uid = UUID(entry["unit_id"])
                    tx.add(
                        AssemblyUnit(
                            id=uuid4(),
                            session_id=proof_session.id,
                            account_id=row.account_id,
                            order_id=row.order_id,
                            shipment_id=child.id,
                            item_id=UUID(entry["item_id"]),
                            unit_id=uid,
                            allocation_id=UUID(entry["allocation_id"]),
                            ordinal=index,
                            scanned_at=original_checks[uid],
                        )
                    )
                result.append(
                    {
                        "shipment_id": str(child.id),
                        "posting_number": observed.source_id,
                        "session_id": str(proof_session.id),
                        "quantity": len(units),
                        "source_revision": child_manifest.assembly_revision,
                    }
                )
            row.status, row.finished_at, row.error, row.split_result = (
                "accepted",
                datetime.now(UTC),
                None,
                result,
            )
            if session.status != "cancelled":
                session.status, session.finished_at, session.message = "ready", datetime.now(UTC), None
                session.revision += 1
            Queue.finish_in(tx, job)
            tx.add(
                AuditEvent(
                    kind="assembly_split_confirmed",
                    subject_id=row.id,
                    details={"shipment_id": str(row.shipment_id), "children": result},
                )
            )

        self.r.write(job, accept)
