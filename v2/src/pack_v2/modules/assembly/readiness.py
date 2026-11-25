"""Durable one-package readiness. Unknown writes are reconciled, never replayed."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from pack_v2.infrastructure.jobs import LeaseLost, Queue, enqueue
from pack_v2.infrastructure.models import AuditEvent, Job
from pack_v2.integrations.public import OutcomeUnknown, ReadError, SourceChanged
from pack_v2.shared.errors import Conflict, DomainError

from .models import AssemblyDispatch, AssemblySession, AssemblyUnit

PENDING = ("queued", "checking", "uncertain")
WAITING = "Ozon обрабатывает сборку. Ожидаем подтверждения готовности к отгрузке"


class Readiness:
    def __init__(self, db, connections, orders, instance_data, *, auto_split=False, documents=None):
        self.db, self.connections, self.orders, self.instances = db, connections, orders, instance_data
        self.auto_split = auto_split
        self.documents = documents
        self.instances.edit_guard = self.guard_marking_release

    def guard_marking_release(self, tx, shipment_id):
        manifest = self.orders.packing_manifest(tx, shipment_id)
        pending = tx.scalar(
            select(AssemblyDispatch.id)
            .where(
                AssemblyDispatch.order_id == manifest.order_id,
                AssemblyDispatch.status.in_(PENDING),
            )
            .limit(1)
        )
        if pending:
            raise Conflict(
                (
                    "Яндекс Маркет"
                    if self.connections.account_label(tx, manifest.account_id).marketplace == "yandex"
                    else "Ozon"
                )
                + " ещё обрабатывает сборку. Дождитесь подтверждения перед изменением данных"
            )

    @staticmethod
    def find(tx, session):
        return tx.scalar(
            select(AssemblyDispatch).where(
                AssemblyDispatch.shipment_id == session.shipment_id,
                AssemblyDispatch.source_revision == session.source_revision,
            )
        )

    @staticmethod
    def blocks_other_shipment(tx, manifest):
        return (
            tx.scalar(
                select(AssemblyDispatch.id)
                .where(
                    AssemblyDispatch.order_id == manifest.order_id,
                    AssemblyDispatch.shipment_id != manifest.shipment_id,
                    func.jsonb_array_length(AssemblyDispatch.layout) > 1,
                    AssemblyDispatch.status != "accepted",
                    or_(AssemblyDispatch.status.in_(PENDING), AssemblyDispatch.ship_started_at.is_not(None)),
                )
                .limit(1)
            )
            is not None
        )

    def view(self, tx, session):
        row = self.find(tx, session)
        if row is None:
            return None
        status = row.status
        if (
            status == "accepted"
            and row.split_result
            and not self.orders.packing_split_current(tx, row.split_result)
        ):
            status = "stale"
        job = tx.get(Job, row.job_id)
        if status in ("queued", "checking") and (
            job.status in ("failed", "succeeded")
            or job.status == "running"
            and job.attempts >= job.max_attempts
            and job.lease_until <= datetime.now(UTC)
        ):
            status = "uncertain" if row.ship_started_at else "rejected"
        return {
            "status": status,
            "message": "Распределение отправлений изменилось. Обновите заказы и выполните сверку"
            if status == "stale" and row.split_result
            else row.error
            or (
                (
                    "Яндекс Маркет обрабатывает сборку. Ожидаем подтверждения готовности к отгрузке"
                    if self.connections.account_label(tx, row.account_id).marketplace == "yandex"
                    else WAITING
                )
                if status in PENDING
                else ""
            ),
            "can_recheck": status in ("uncertain", "rejected"),
            "children": row.split_result if status == "accepted" else [],
            "package_count": (self.orders.label_target(tx, row.shipment_id).package_count or 1)
            if self.connections.account_label(tx, row.account_id).marketplace == "yandex"
            else len(row.layout) or 1,
        }

    def request(self, tx, session, manifest):
        fingerprint, _ = self.instances.dispatch_evidence(tx, session.instance_context_id, manifest)
        units = list(
            tx.scalars(
                select(AssemblyUnit)
                .where(AssemblyUnit.session_id == session.id)
                .order_by(AssemblyUnit.item_id, AssemblyUnit.ordinal)
            )
        )
        market = self.connections.account_label(tx, session.account_id).marketplace
        groups = (
            self.orders.packing_layout(tx, manifest, units, automatic=self.auto_split)
            if manifest.state == "awaiting_assembly" and market == "ozon"
            else []
        )
        frozen = None
        if len(groups) > 1:
            fingerprint, frozen = self.instances.freeze_dispatch(tx, session.instance_context_id, manifest)
        old = self.find(tx, session)
        if old:
            if (
                old.status == "accepted"
                or old.status in ("rejected", "stale")
                and old.ship_started_at is None
            ):
                # A new, explicit completion may replace a preflight failure, never an unknown write.
                # Previously accepted readiness is observed again; preserve any old ship intent.
                old.proof_session_id, old.context_id = session.id, session.instance_context_id
                old.credential_revision, old.basis = session.credential_revision, manifest.basis
                old.data_fingerprint = fingerprint
                if old.ship_started_at is None:
                    old.layout, old.encrypted_evidence = groups, frozen
                old.job_id = enqueue(
                    tx,
                    "assembly.ready",
                    "assembly.ready.resume:" + str(uuid4()),
                    {"dispatch_id": str(old.id)},
                )
                tx.get(Job, old.job_id).max_attempts = 30
                old.status, old.error, old.finished_at = "queued", None, None
                tx.add(
                    AuditEvent(
                        kind="assembly_readiness_reauthorized",
                        actor_id=session.user_id,
                        subject_id=old.id,
                        details={"session_id": str(session.id)},
                    )
                )
            elif (
                old.basis != manifest.basis
                or old.credential_revision != session.credential_revision
                or old.data_fingerprint != fingerprint
            ):
                raise Conflict("Предыдущая сборка требует сверки. Повторная отправка запрещена")
            return old
        # An uncertain dispatch survives source revision changes and local cancellation.
        unsettled = tx.scalar(
            select(AssemblyDispatch.id)
            .where(
                AssemblyDispatch.shipment_id == session.shipment_id,
                or_(
                    AssemblyDispatch.status.in_(PENDING),
                    AssemblyDispatch.ship_started_at.is_not(None) & (AssemblyDispatch.status != "accepted"),
                ),
            )
            .limit(1)
        )
        if unsettled:
            raise Conflict("Предыдущая сборка ещё не подтверждена. Сначала проверьте её результат")
        row = AssemblyDispatch(
            id=uuid4(),
            account_id=session.account_id,
            order_id=session.order_id,
            shipment_id=session.shipment_id,
            proof_session_id=session.id,
            context_id=session.instance_context_id,
            credential_revision=session.credential_revision,
            source_revision=session.source_revision,
            basis=session.basis,
            data_fingerprint=fingerprint,
            layout=groups,
            encrypted_evidence=frozen,
        )
        row.job_id = enqueue(
            tx, "assembly.ready", "assembly.ready:" + str(row.id), {"dispatch_id": str(row.id)}
        )
        tx.get(Job, row.job_id).max_attempts = 30
        tx.add(row)
        tx.flush()
        tx.add(
            AuditEvent(
                kind="assembly_readiness_requested",
                actor_id=session.user_id,
                subject_id=row.id,
                details={"shipment_id": str(row.shipment_id)},
            )
        )
        return row

    def recheck(self, tx, session):
        row = self.find(tx, session)
        if row is None or row.proof_session_id != session.id or row.status in ("accepted", "stale"):
            raise Conflict("Нет незавершённой сборки для проверки")
        job = tx.get(Job, row.job_id)
        if job.status in ("pending", "running") and not (
            job.status == "running"
            and job.lease_until <= datetime.now(UTC)
            and job.attempts >= job.max_attempts
        ):
            return
        row.job_id = enqueue(
            tx, "assembly.ready", "assembly.ready.recheck:" + str(uuid4()), {"dispatch_id": str(row.id)}
        )
        tx.get(Job, row.job_id).max_attempts = 30
        row.status, row.error = "checking", None
        # Durable ship intent and retry count are deliberately retained.

    def _locked(self, tx, job):
        row = tx.get(AssemblyDispatch, UUID(job.payload["dispatch_id"]))
        if row is None:
            raise LeaseLost("Операция сборки не найдена")
        source = self.connections.source_at_revision(tx, row.account_id, row.credential_revision)
        Queue.owned(tx, job)
        self.orders.packing_lock(tx, row.shipment_id)
        row = tx.scalar(
            select(AssemblyDispatch)
            .where(AssemblyDispatch.id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row.job_id != job.id:
            raise LeaseLost("Исполнитель сборки устарел")
        session = tx.scalar(
            select(AssemblySession)
            .where(AssemblySession.id == row.proof_session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row.status == "accepted":
            return row, session, source, None, None
        manifest = self.orders.packing_ready_manifest(tx, row.shipment_id)
        if len(row.layout) > 1 and row.ship_started_at:
            # After an irreversible split, the original composition may legitimately disappear.
            # Only reads are allowed; keep the immutable original proof for child reconciliation.
            units = list(tx.scalars(select(AssemblyUnit).where(AssemblyUnit.session_id == session.id)))
            if (
                not session.verified_at
                or any(u.scanned_at is None for u in units)
                or {str(u.unit_id) for u in units} != {uid for group in row.layout for uid in group}
            ):
                raise SourceChanged("Исходные сканы разделённого отправления не подтверждены")
            return row, session, source, manifest, self.instances.open_dispatch(row.encrypted_evidence)
        if (
            not manifest.eligible
            or manifest.assembly_revision != row.source_revision
            or manifest.basis != row.basis
        ):
            raise SourceChanged("Состав или состояние отправления изменились. Нужна повторная сверка")
        units = list(tx.scalars(select(AssemblyUnit).where(AssemblyUnit.session_id == session.id)))
        if (
            not session.verified_at
            or not units
            or any(u.scanned_at is None for u in units)
            or len(units) != sum(i.quantity for i in manifest.items)
        ):
            raise SourceChanged("Не подтверждены сканы всех физических единиц")
        fingerprint, evidence = self.instances.dispatch_evidence(tx, row.context_id, manifest)
        if fingerprint != row.data_fingerprint:
            raise SourceChanged("Данные экземпляров изменились после завершения проверки")
        return row, session, source, manifest, evidence

    def write(self, job, callback):
        with self.db.transaction() as tx:
            return callback(tx, *self._locked(tx, job))

    @staticmethod
    def posting_matches(posting, manifest):
        products = posting.get("products")
        actual = {}
        if not isinstance(products, list) or not products:
            raise ReadError("incomplete")
        for product in products:
            if (
                not isinstance(product, dict)
                or type(product.get("quantity")) is not int
                or product["quantity"] <= 0
            ):
                raise ReadError("invalid_response")
            key = str(product.get("sku"))
            if key in actual:
                raise ReadError("invalid_response")
            actual[key] = product["quantity"]
        expected = {}
        for item in manifest.items:
            keys = [v for k, v in item.references if k == "sku"]
            if (
                len(keys) != 1
                or not keys[0].isascii()
                or not keys[0].isdigit()
                or not 0 < int(keys[0]) < 2**63
                or keys[0] in expected
            ):
                raise SourceChanged("Не подтверждён SKU товара")
            expected[keys[0]] = item.quantity
        if (
            posting.get("posting_number") != manifest.posting_number
            or actual != expected
            or posting.get("status") not in ("awaiting_packaging", "awaiting_deliver")
        ):
            raise SourceChanged("Ozon изменил состав или состояние отправления. Повторите сверку")
        substatus = posting.get("substatus")
        if substatus is not None and (
            not isinstance(substatus, str)
            or len(substatus) > 128
            or substatus
            and not substatus.isprintable()
        ):
            raise ReadError("invalid_response")
        return [{"product_id": int(key), "quantity": value} for key, value in sorted(expected.items())]

    def perform(self, job):
        try:
            self.run(job)
        except LeaseLost:
            return
        except (SourceChanged, Conflict) as error:
            self.fail(job, str(error), stale=True)
        except OutcomeUnknown:
            self.fail(job, self.waiting(job), retry=True)
        except ReadError as error:
            self.fail(job, str(error), retry=error.retryable, delay=error.retry_after)
        except DomainError as error:
            self.fail(job, str(error))
        except SQLAlchemyError:
            self.fail(job, "База данных временно недоступна", retry=True)
        except Exception as error:
            logging.error("readiness job stopped; job=%s; type=%s", job.id, type(error).__name__)
            self.fail(job, "Проверка готовности прервана. Проверьте результат")

    def waiting(self, job):
        with self.db.transaction() as tx:
            row = tx.get(AssemblyDispatch, UUID(job.payload["dispatch_id"]))
            if row and self.connections.account_label(tx, row.account_id).marketplace == "yandex":
                return "Яндекс Маркет обрабатывает сборку. Ожидаем подтверждения готовности к отгрузке"
        return WAITING

    def run(self, job):
        def begin(tx, row, session, source, manifest, evidence):
            if row.status in ("accepted", "stale"):
                Queue.finish_in(tx, job)
                return None
            row.status, row.error = "checking", None
            return (
                source,
                manifest,
                evidence,
                row.ship_started_at,
                row.send_count,
                session.status == "cancelled",
                row.layout,
                row.split_targets,
            )

        saved = self.write(job, begin)
        if saved is None:
            return
        source, manifest, evidence, started, count, cancelled, layout, targets = saved
        if source.marketplace == "yandex":
            from .yandex_readiness import run

            return run(self, job, source, manifest, evidence, started, cancelled)
        if len(layout) > 1:
            from .splitting import SplitExecution

            SplitExecution(self).run(job, source, manifest, evidence, started, cancelled, layout, targets)
            return
        if cancelled and not started:
            self.fail(job, "Локальная сборка отменена до передачи запроса Ozon")
            return
        with self.connections.instance_client(source) as client:
            posting = self.instances.check_dispatch(client, manifest, evidence)
            observed_at = datetime.now(UTC)
            products = self.posting_matches(posting, manifest)
        if posting["status"] == "awaiting_deliver" and posting.get("substatus") != "ship_failed":

            def accept(tx, row, session, source, manifest, evidence):
                self.orders.packing_confirm_ready(tx, manifest, observed_at, posting.get("substatus"))
                if self.documents:
                    self.documents.request(tx, source, row.shipment_id)
                row.status, row.finished_at, row.error = "accepted", datetime.now(UTC), None
                if session.status != "cancelled":
                    session.status, session.finished_at, session.message = "ready", datetime.now(UTC), None
                    session.revision += 1
                Queue.finish_in(tx, job)
                tx.add(
                    AuditEvent(
                        kind="assembly_readiness_confirmed",
                        subject_id=row.id,
                        details={"shipment_id": str(row.shipment_id)},
                    )
                )

            self.write(job, accept)
            return
        failed = posting.get("substatus") == "ship_failed"
        if started and failed:
            if cancelled:
                self.fail(job, "Ozon не собрал отправление. Локальная сборка отменена; повтор не выполняется")
                return
            if count >= 3:
                self.fail(
                    job, "Ozon не смог собрать отправление после трёх попыток. Проверьте заказ в кабинете"
                )
                return

            def allow_retry(tx, row, *args):
                row.retry_allowed = True
                tx.add(
                    AuditEvent(
                        kind="assembly_ship_failure_observed",
                        subject_id=row.id,
                        details={"shipment_id": str(row.shipment_id), "attempt": row.send_count},
                    )
                )

            self.write(job, allow_retry)
        if started and not failed:
            self.fail(job, WAITING, retry=True)
            return
        payload = {"posting_number": manifest.posting_number, "packages": [{"products": products}]}
        with self.connections.shipping_client(source) as client:

            def intent(tx, row, session, *args):
                if session.status == "cancelled":
                    raise Conflict("Локальная сборка отменена до передачи запроса Ozon")
                if row.ship_started_at and not row.retry_allowed or row.send_count >= 3:
                    raise Conflict("Повторная сборка без подтверждённого отказа запрещена")
                row.ship_started_at, row.retry_allowed = datetime.now(UTC), False
                row.send_count += 1
                tx.add(
                    AuditEvent(
                        kind="assembly_ship_started",
                        subject_id=row.id,
                        details={"shipment_id": str(row.shipment_id), "attempt": row.send_count},
                    )
                )

            self.write(job, intent)
            try:
                client.ship(payload)
            except ReadError as error:
                if error.status is not None and 400 <= error.status < 500:
                    rate_limited = error.status == 429

                    def refused(tx, row, *args):
                        row.retry_allowed = False
                        if rate_limited:
                            row.ship_started_at = None

                    self.write(job, refused)
                raise
        self.fail(job, WAITING, retry=True)

    def fail(self, job, message, *, retry=False, stale=False, delay=None):
        try:
            with self.db.transaction() as tx:
                Queue.owned(tx, job)
                row = tx.scalar(
                    select(AssemblyDispatch)
                    .where(AssemblyDispatch.id == UUID(job.payload["dispatch_id"]))
                    .with_for_update()
                )
                if row is None or row.job_id != job.id:
                    raise LeaseLost("Операция проверки устарела")
                if row.status == "accepted":
                    Queue.finish_in(tx, job)
                    return
                if stale and len(row.layout) > 1 and row.ship_started_at:
                    stale = False  # The split happened; an unresolved child observation remains recheckable.
                # Bounded background polling; explicit recheck only resumes observations.
                job_row = tx.get(Job, job.id)
                recent = datetime.now(UTC) - job_row.created_at < timedelta(minutes=10)
                status = Queue.finish_in(
                    tx,
                    job,
                    retry=retry and not stale and recent,
                    failed=True,
                    retry_at=datetime.now(UTC) + timedelta(seconds=max(10, delay or 0)),
                )
                row.error = message
                row.status = (
                    "stale"
                    if stale
                    else "checking"
                    if status == "pending"
                    else "uncertain"
                    if row.ship_started_at and not row.retry_allowed
                    else "rejected"
                )
                if status != "pending":
                    row.finished_at = datetime.now(UTC)
        except LeaseLost:
            return
