"""Durable dispatch: persist intent before HTTP; after ambiguity reconcile only."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from pack_v2.infrastructure.jobs import LeaseLost, Queue
from pack_v2.infrastructure.models import AuditEvent
from pack_v2.integrations.public import AccountSource, OutcomeUnknown, ReadError, SourceChanged
from pack_v2.shared.errors import Conflict, DomainError

from . import policy
from .models import InstanceContext, Submission, UnitDetail


class InstanceJobs:
    def __init__(self, service):
        self.s = service

    def _locked(self, tx, job, *, adopt=False):
        if job.kind == "instances.discover":
            batch = None
            row = self.s._context(tx, UUID(job.payload["context_id"]))
        else:
            batch = tx.get(Submission, UUID(job.payload["batch_id"]))
            if batch is None:
                raise SourceChanged("Пакет передачи не найден")
            row = self.s._context(tx, batch.context_id)
        source = AccountSource(row.account_id, "ozon", row.credential_revision)
        self.s.connections.lock_source(tx, source)
        Queue.owned(tx, job)
        self.s.orders.packing_lock(tx, row.shipment_id)
        row = tx.scalar(
            select(InstanceContext)
            .where(InstanceContext.id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if batch:
            batch = tx.scalar(
                select(Submission)
                .where(Submission.id == batch.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if (batch.job_id if batch else row.job_id) != job.id:
            raise LeaseLost("Исполнитель проверки устарел")
        manifest = self.s.orders.packing_manifest(tx, row.shipment_id)
        if not manifest.eligible:
            raise SourceChanged("Отправление больше недоступно для сборки")
        if adopt and row.encrypted_snapshot is None and row.status in ("queued", "reading"):
            row.source_revision, row.basis = manifest.assembly_revision, manifest.basis
        if (
            row.source_revision != manifest.assembly_revision
            or row.basis != manifest.basis
            or row.status == "stale"
        ):
            raise SourceChanged("Состав или требования отправления изменились. Повторите сверку")
        if batch and self.s._fingerprint(tx, row)[0] != batch.fingerprint:
            raise SourceChanged("Значения изменились после подготовки пакета")
        return row, batch, source, manifest

    def write(self, job, callback, *, adopt=False):
        with self.s.db.transaction() as tx:
            return callback(tx, *self._locked(tx, job, adopt=adopt))

    def perform(self, job):
        try:
            if job.kind == "instances.discover":
                self.discover(job)
            elif job.kind == "instances.submit":
                self.submit(job)
            else:
                raise DomainError("Неизвестная операция экземпляров")
        except LeaseLost:
            return
        except SourceChanged as error:
            self.failed(job, str(error), stale=True)
        except OutcomeUnknown as error:
            self.failed(job, str(error), retry=True)
        except ReadError as error:
            self.failed(job, str(error), retry=error.retryable, delay=error.retry_after)
        except Conflict as error:
            self.failed(job, str(error), stale=True)
        except DomainError as error:
            self.failed(job, str(error))
        except SQLAlchemyError:
            self.failed(job, "База данных временно недоступна", retry=True)
        except Exception as error:
            logging.error("instance job stopped; job=%s; type=%s", job.id, type(error).__name__)
            self.failed(job, "Проверка данных прервана. Повторите сверку")

    def discover(self, job):
        def begin(tx, row, batch, source, manifest):
            if row.status not in ("queued", "reading"):
                raise SourceChanged("Проверка требований уже завершена")
            row.status = "reading"
            return source, manifest

        source, manifest = self.write(job, begin, adopt=True)
        with self.s.connections.instance_client(source) as client:
            snapshot = client.discover(manifest.posting_number)
            if snapshot.source != source or snapshot.posting_number != manifest.posting_number:
                raise ReadError("invalid_response")
            if snapshot.posting:
                self._posting_matches(snapshot.posting, manifest)
            req = policy.requirements(manifest, snapshot)
            payload = {
                "posting_number": snapshot.posting_number,
                "products": snapshot.products,
                "multi_box_qty": snapshot.multi_box_qty,
            }

        def save(tx, row, batch, source, manifest):
            if row.status != "reading":
                raise SourceChanged("Проверка требований устарела")
            # Match again in the transaction where the result is saved.
            policy.requirements(manifest, snapshot)
            previous = tx.scalars(
                select(InstanceContext)
                .where(
                    InstanceContext.shipment_id == row.shipment_id,
                    InstanceContext.id != row.id,
                    InstanceContext.encrypted_snapshot.is_not(None),
                )
                .order_by(InstanceContext.created_at.desc(), InstanceContext.id.desc())
            )
            native_ids = {
                str(p["product_id"]): {str(e["exemplar_id"]) for e in p["exemplars"]}
                for p in snapshot.products
            }
            for old in previous:
                baseline = self.s._open(old.encrypted_snapshot)
                old_ids = {
                    str(p["product_id"]): {str(e["exemplar_id"]) for e in p["exemplars"]}
                    for p in baseline["products"]
                }
                if old_ids != native_ids:
                    continue
                # Ready postings can lose positive flags; retain the panel policy for
                # the same physical instances, without changing native cis_market.
                for key, fields in req.items():
                    for field in ("cis", "gtd"):
                        if old.requirements.get(key, {}).get(field):
                            fields[field] = True
                            fields["unknown"] = [f for f in fields["unknown"] if f != field]
            row.requirements, row.signature = req, policy.signature(snapshot.products, req)
            row.encrypted_snapshot = self.s._seal(payload)
            row.status, row.checked_at, row.error = "ready", datetime.now(UTC), None
            Queue.finish_in(tx, job)
            tx.add(
                AuditEvent(
                    kind="instance_requirements_received",
                    subject_id=row.id,
                    details={"shipment_id": str(row.shipment_id), "policy_version": row.policy_version},
                )
            )

        self.write(job, save)

    @staticmethod
    def _posting_matches(raw, manifest):
        items = raw.get("products")
        if not isinstance(items, list) or not items:
            raise ReadError("incomplete")
        actual = {str(p.get("sku")): p.get("quantity") for p in items}
        expected = {policy.product_key(i): i.quantity for i in manifest.items}
        if (
            len(actual) != len(items)
            or actual != expected
            or raw.get("status") not in ("awaiting_packaging", "awaiting_deliver")
            or raw.get("substatus") == "ship_failed"
        ):
            raise SourceChanged("Ozon изменил состав или состояние отправления. Повторите сверку")

    def _intent(self, job, kind):
        def save(tx, row, batch, source, manifest):
            if getattr(batch, kind + "_started_at") is not None:
                raise Conflict("Повторная запись без сверки запрещена")
            setattr(batch, kind + "_started_at", datetime.now(UTC))
            batch.status, batch.error = "checking", None
            tx.add(
                AuditEvent(
                    kind="instance_dispatch_started",
                    subject_id=batch.id,
                    details={"shipment_id": str(row.shipment_id), "method": kind},
                )
            )

        self.write(job, save)

    def _send(self, job, client, kind, payload):
        self._intent(job, kind)
        try:
            if kind == "set":
                client.submit(payload)
            else:
                client.update(payload["posting_number"])
        except ReadError as error:
            if error.status is not None and 400 <= error.status < 500:
                # A definitive HTTP refusal did not accept the write. Ambiguity
                # (timeout, malformed 200, 5xx) NEVER clears this durable intent.
                self.write(
                    job, lambda tx, row, batch, source, manifest: setattr(batch, kind + "_started_at", None)
                )
            raise

    def submit(self, job):
        def read(tx, row, batch, source, manifest):
            if batch.status in ("accepted", "rejected", "stale"):
                Queue.finish_in(tx, job)
                return None
            return (
                source,
                manifest,
                self.s._open(row.encrypted_snapshot),
                row.requirements,
                row.signature,
                self.s._open(batch.encrypted_payload),
                bool(batch.set_started_at),
                bool(batch.update_started_at),
            )

        saved = self.write(job, read)
        if saved is None:
            return
        source, manifest, baseline, req, signature, packet, set_started, update_started = saved
        payload = packet["request"]
        unit_ids = {tuple(k.split(":")): v for k, v in packet["unit_ids"].items()}
        with self.s.connections.instance_client(source) as client:
            fresh = client.discover(manifest.posting_number)
            if fresh.source != source or not policy.compatible(
                fresh, baseline, req, policy.requirements(manifest, fresh)
            ):
                raise SourceChanged("Ozon изменил экземпляры или требования. Повторите сверку")
            if not policy.fresh_matches(fresh, baseline, payload) and not (
                packet.get("previous_rejected")
                and policy.fresh_matches(fresh, packet["previous_rejected"], payload)
            ):
                raise SourceChanged(
                    "Данные экземпляров изменились в кабинете Ozon. Передача остановлена для сверки"
                )
            posting = (
                fresh.posting
                if fresh.posting and fresh.posting.get("status") == "awaiting_packaging"
                else client.posting(manifest.posting_number)
            )
            self._posting_matches(posting, manifest)
            # create-or-get can initialize an empty unit with is_gtd_absent=true.
            # That default is not proof that the packer's explicit choice was sent.
            declaration_needed = any(
                req[str(p["product_id"])]["gtd"] and e.get("is_gtd_absent") is True
                for p in payload["products"]
                for e in p["exemplars"]
            )
            if (
                set_started
                or not declaration_needed
                and all(
                    policy.same_unit(got, want, check_id=True)
                    for p in fresh.products
                    for got, want in zip(
                        p["exemplars"],
                        next(
                            q["exemplars"]
                            for q in payload["products"]
                            if str(q["product_id"]) == str(p["product_id"])
                        ),
                        strict=True,
                    )
                )
            ):
                data = client.status(manifest.posting_number)
                passed, errors, results = policy.checked_status(data, payload, req, unit_ids)
                if errors:
                    self.reject(job, errors, policy.applied_field_proofs(posting, payload, unit_ids, results))
                    return
                if (
                    passed
                    and posting["status"] == "awaiting_packaging"
                    and data.get("status") == "ship_available"
                ):
                    self.accept(job)
                    return
                if passed and posting["status"] == "awaiting_deliver":
                    if policy.applied(posting, payload):
                        self.accept(job)
                        return
                    if data.get("status") == "update_available" and not update_started:
                        self._send(job, client, "update", payload)
                        self.write(job, lambda *args: None)
                        posting = client.posting(manifest.posting_number)
                        self._posting_matches(posting, manifest)
                        if policy.applied(posting, payload):
                            self.accept(job)
                            return
                    self.wait(job)
                    return
                if set_started:
                    self.wait(job)
                    return
            # validate checks markings, not the explicit absence declaration for GTD.
            # Include native UIN/IMEI marks too: the complete packet must be preserved.
            if any(e.get("marks") for p in payload["products"] for e in p["exemplars"]):
                validation = client.validate(policy.validation_request(payload))
                errors = policy.checked_validation(validation, payload, unit_ids)
                if errors:
                    self.reject(job, errors)
                    return
            # A final revision/lease/source fence and durable intent before the only set.
            self._send(job, client, "set", payload)
        self.wait(job)

    def accept(self, job):
        def save(tx, row, batch, source, manifest):
            batch.status, batch.finished_at, batch.error = "accepted", datetime.now(UTC), None
            packet = self.s._open(batch.encrypted_payload)
            batch.field_results = {
                uid: {
                    **({"cis": "accepted"} if uid in packet["assignments"] else {}),
                    **({"gtd": "accepted"} if row.requirements[key.split(":")[0]]["gtd"] else {}),
                }
                for key, uid in packet["unit_ids"].items()
            }
            self.s.marking.transmissions(
                tx, batch.id, [UUID(v) for v in packet["assignments"].values()], "accepted"
            )
            self.s.marking.finalize_replacements(
                tx, row.shipment_id, [UUID(v) for v in packet["assignments"].values()], packet["request"]
            )
            for detail in self.s._details(tx, row):
                stored = tx.get(UnitDetail, detail.unit_id)
                values = dict(detail.values)
                values["base_gtd"] = values.get("gtd")
                stored.encrypted_values = self.s._seal(values)
            Queue.finish_in(tx, job)
            tx.add(
                AuditEvent(
                    kind="instance_submission_accepted",
                    subject_id=batch.id,
                    details={"shipment_id": str(row.shipment_id)},
                )
            )

        self.write(job, save)

    def reject(self, job, errors, results=None):
        def save(tx, row, batch, source, manifest):
            batch.status, batch.finished_at, batch.error = (
                "rejected",
                datetime.now(UTC),
                "Ozon не принял данные экземпляра. Проверьте Честный знак и ГТД",
            )
            batch.field_errors = errors
            batch.field_results = results or {}
            packet = self.s._open(batch.encrypted_payload)
            self.s.marking.transmissions(
                tx,
                batch.id,
                [
                    UUID(v)
                    for uid, v in packet["assignments"].items()
                    if (results or {}).get(uid, {}).get("cis") == "accepted"
                ],
                "accepted",
            )
            self.s.marking.transmissions(
                tx,
                batch.id,
                [UUID(v) for uid, v in packet["assignments"].items() if "cis" in errors.get(uid, [])],
                "rejected",
            )
            Queue.finish_in(tx, job, failed=True)
            tx.add(
                AuditEvent(
                    kind="instance_submission_rejected",
                    subject_id=batch.id,
                    details={"shipment_id": str(row.shipment_id)},
                )
            )

        self.write(job, save)

    def wait(self, job):
        self.failed(job, "Ожидаем подтверждения данных экземпляров Ozon", retry=True)

    def failed(self, job, message, *, retry=False, stale=False, delay=None):
        try:
            with self.s.db.transaction() as tx:
                Queue.owned(tx, job)
                batch = (
                    tx.get(Submission, UUID(job.payload["batch_id"]))
                    if job.kind == "instances.submit"
                    else None
                )
                row = tx.get(InstanceContext, batch.context_id if batch else UUID(job.payload["context_id"]))
                if (batch.job_id if batch else row.job_id) != job.id:
                    raise LeaseLost("Проверка устарела")
                if batch and datetime.now(UTC) - batch.created_at > timedelta(minutes=10):
                    retry = False
                status = Queue.finish_in(
                    tx,
                    job,
                    retry=retry and not stale,
                    failed=True,
                    retry_at=datetime.now(UTC) + timedelta(seconds=max(10, delay or 0)),
                )
                if batch:
                    if batch.status == "accepted":
                        return
                    batch.error = message
                    if stale:
                        batch.status = "stale"
                    elif status == "pending":
                        batch.status = "checking"
                    else:
                        batch.status = (
                            "uncertain" if batch.set_started_at or batch.update_started_at else "rejected"
                        )
                    if batch.status == "uncertain":
                        packet = self.s._open(batch.encrypted_payload)
                        self.s.marking.transmissions(
                            tx, batch.id, [UUID(v) for v in packet["assignments"].values()], "unknown"
                        )
                else:
                    row.status, row.error = (
                        ("stale" if stale else "queued" if status == "pending" else "failed"),
                        message,
                    )
        except LeaseLost:
            return
