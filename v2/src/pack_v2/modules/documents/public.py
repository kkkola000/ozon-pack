"""Automatic label acquisition. Reads and file delivery never change assembly readiness."""

import hashlib
import io
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import InvalidToken
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer

from pack_v2.infrastructure.jobs import LeaseLost, Queue, enqueue
from pack_v2.infrastructure.models import AuditEvent, Job
from pack_v2.integrations.documents import MAX_LABEL_BYTES
from pack_v2.integrations.public import ReadError, SourceChanged
from pack_v2.shared.errors import Conflict, DomainError, NotFound

from .models import ShipmentLabel


def validate_pdf(data):
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_LABEL_BYTES or not data.startswith(b"%PDF-"):
        raise ReadError("invalid_response")
    try:
        document = PdfReader(io.BytesIO(data), strict=True)
        if document.is_encrypted or not 1 <= len(document.pages) <= 100:
            raise ValueError("Invalid PDF")
        for page in document.pages:
            if not (0 < float(page.mediabox.width) <= 5000 and 0 < float(page.mediabox.height) <= 5000):
                raise ValueError("Invalid page")
    except Exception:
        raise ReadError("invalid_response") from None


class Documents:
    def __init__(self, db, connections, orders, box, *, label_size="small"):
        if label_size not in ("small", "big"):
            raise ValueError("Invalid label size")
        self.db, self.connections, self.orders, self.box = db, connections, orders, box
        self.label_size = label_size
        self.instances = None

    def request(self, tx, source, shipment_id, *, retry=False, defer=False):
        """Called under the account/order locks, after an authorized assembly refresh."""
        target = self.orders.label_target(tx, shipment_id)
        if target.account_id != source.account_id:
            raise Conflict("Кабинет стикера не совпадает с отправлением")
        if not target.eligible:
            return None
        row = tx.scalar(
            select(ShipmentLabel)
            .where(ShipmentLabel.shipment_id == shipment_id, ShipmentLabel.size == self.label_size)
            .with_for_update()
        )
        same = row and row.basis == target.basis and row.credential_revision == source.credential_revision
        if same and row.status == "waiting_ready" and defer:
            return row.id
        if (
            same
            and row.status == "waiting_ready"
            and target.state == "awaiting_assembly"
            and source.marketplace == "ozon"
        ):
            return row.id
        if (
            same
            and row.status == "ready"
            and (target.state == "awaiting_dispatch" or source.marketplace == "yandex")
        ):
            return row.id
        if same and row.status in ("queued", "processing"):
            pending = tx.get(Job, row.job_id)
            if pending.status in ("pending", "running") and not (
                pending.status == "running"
                and pending.attempts >= pending.max_attempts
                and pending.lease_until <= datetime.now(UTC)
            ):
                return row.id
        if same and row.status == "failed" and not retry:
            return row.id
        if row is None:
            row = ShipmentLabel(
                id=uuid4(),
                account_id=source.account_id,
                shipment_id=shipment_id,
                size=self.label_size,
                basis=target.basis,
                credential_revision=source.credential_revision,
                status="waiting_ready",
                generation=1,
            )
            tx.add(row)
        else:
            row.generation += 1
        row.basis, row.credential_revision = target.basis, source.credential_revision
        for field in ("task_id", "encrypted_pdf", "checksum", "byte_count", "received_at", "error", "job_id"):
            setattr(row, field, None)
        row.status = "waiting_ready"
        if not defer and (target.state == "awaiting_dispatch" or source.marketplace == "yandex"):
            row.status = "queued"
            row.job_id = enqueue(
                tx, "documents.label", f"documents.label:{row.id}:{row.generation}", {"label_id": str(row.id)}
            )
            tx.get(Job, row.job_id).max_attempts = 30
        tx.flush()
        return row.id

    @staticmethod
    def invalidate(tx, shipment_id):
        for row in tx.scalars(
            select(ShipmentLabel).where(ShipmentLabel.shipment_id == shipment_id).with_for_update()
        ):
            row.status, row.encrypted_pdf, row.checksum, row.byte_count, row.received_at = (
                "stale",
                None,
                None,
                None,
                None,
            )

    def view(self, tx, shipment_ids):
        result = []
        for shipment_id in dict.fromkeys(shipment_ids):
            target = self.orders.label_target(tx, shipment_id)
            row = tx.scalar(
                select(ShipmentLabel)
                .options(defer(ShipmentLabel.encrypted_pdf))
                .where(ShipmentLabel.shipment_id == shipment_id, ShipmentLabel.size == self.label_size)
            )
            if row is None:
                continue
            source = self.connections.account_label(tx, row.account_id)
            status = row.status
            if (
                not target.eligible
                or target.basis != row.basis
                or not source.active
                or source.credential_revision != row.credential_revision
            ):
                status = "stale"
            elif status == "ready" and target.state != "awaiting_dispatch" and source.marketplace == "ozon":
                status = "waiting_ready"
            elif status in ("queued", "processing"):
                pending = tx.get(Job, row.job_id)
                if pending.status in ("failed", "succeeded") or (
                    pending.status == "running"
                    and pending.attempts >= pending.max_attempts
                    and pending.lease_until <= datetime.now(UTC)
                ):
                    status = "failed"
            messages = {
                "waiting_ready": "Стикер будет получен после подтверждения готовности Ozon",
                "queued": "Получаем стикер Ozon…",
                "processing": "Ozon готовит стикер…",
                "ready": "Стикер получен",
                "failed": row.error or "Не удалось получить стикер. Повторите загрузку",
                "stale": "Данные стикера изменились. Откройте отправление заново",
                "expired": "Срок хранения стикера истёк",
            }
            if source.marketplace == "yandex":
                messages = {k: v.replace("Ozon", "Яндекс Маркета") for k, v in messages.items()}
            result.append(
                {
                    "id": str(row.id),
                    "shipment_id": str(shipment_id),
                    "posting_number": target.posting_number,
                    "status": status,
                    "message": messages[status],
                    "key": f"{row.id}:{row.generation}",
                    "generation": row.generation,
                    "box_units": row.archive_snapshot.get("box_units", []),
                    "url": f"/api/v2/documents/labels/{row.id}/{row.generation}/pdf"
                    if status == "ready"
                    else None,
                }
            )
        return result

    def file(self, actor, label_id, generation):
        actor.require("pack")
        with self.db.transaction() as tx:
            row = tx.get(ShipmentLabel, label_id)
            if row is None:
                raise NotFound("Стикер не найден")
            source = self.connections.source_at_revision(tx, row.account_id, row.credential_revision)
            self.connections.lock_source(tx, source)
            self.orders.packing_lock(tx, row.shipment_id)
            tx.refresh(row)
            target = self.orders.label_target(tx, row.shipment_id)
            if (
                row.generation != generation
                or row.status != "ready"
                or not target.eligible
                or target.state
                not in (
                    ("awaiting_assembly", "awaiting_dispatch")
                    if source.marketplace == "yandex"
                    else ("awaiting_dispatch",)
                )
                or row.basis != target.basis
            ):
                raise Conflict("Стикер ещё не готов или состав отправления изменился. Обновите окно сборки")
            pdf = self.content(row)
            number = "".join(c for c in target.posting_number if c.isascii() and (c.isalnum() or c in "-_"))
            return pdf, f"{source.marketplace}-{number or row.id}.pdf"

    def content(self, row):
        try:
            pdf = self.box.open(row.encrypted_pdf)
        except (InvalidToken, TypeError):
            raise DomainError("Не удалось прочитать стикер. Проверьте ключ шифрования панели") from None
        if len(pdf) != row.byte_count or hashlib.sha256(pdf).hexdigest() != row.checksum:
            raise DomainError("Не удалось проверить сохранённый стикер. Повторите загрузку")
        return pdf

    def print_source(self, tx, label_id, generation, *, archived=False, content=True):
        """Trusted print port; archived reads retain history after delivery/disabled account."""
        from .archive import cutoff

        row = tx.scalar(
            select(ShipmentLabel)
            .options(defer(ShipmentLabel.encrypted_pdf))
            .where(ShipmentLabel.id == label_id)
        )
        if row is None or (row.received_at or row.created_at) < cutoff(datetime.now(UTC)):
            raise NotFound("Стикер не найден или срок хранения истёк")
        if row.generation != generation or row.status != "ready":
            raise Conflict("Стикер обновлён или ещё не получен. Обновите окно")
        target = self.orders.label_target(tx, row.shipment_id)
        if row.basis != target.basis:
            raise Conflict("Состав стикера изменился. Откройте отправление заново")
        if not archived:
            source = self.connections.account_label(tx, row.account_id)
            if (
                not source.active
                or source.credential_revision != row.credential_revision
                or not target.eligible
                or target.state
                not in (
                    ("awaiting_assembly", "awaiting_dispatch")
                    if source.marketplace == "yandex"
                    else ("awaiting_dispatch",)
                )
            ):
                raise Conflict("Данные отправления изменились. Обновите окно сборки")
        return {
            "box_units": row.archive_snapshot.get("box_units", []),
            "marketplace": self.connections.account_label(tx, row.account_id).marketplace,
            "shipment_id": row.shipment_id,
            "posting_number": target.posting_number,
            "data": self.content(row) if content else None,
        }

    def _write(self, job, callback):
        with self.db.transaction() as tx:
            row = tx.get(ShipmentLabel, UUID(job.payload["label_id"]))
            if row is None:
                raise LeaseLost("Стикер не найден")
            source = self.connections.source_at_revision(tx, row.account_id, row.credential_revision)
            self.connections.lock_source(tx, source)
            Queue.owned(tx, job)
            self.orders.packing_lock(tx, row.shipment_id)
            tx.refresh(row, with_for_update=True)
            if row.job_id != job.id:
                raise LeaseLost("Загрузка стикера устарела")
            target = self.orders.label_target(tx, row.shipment_id)
            if row.status == "stale" or not target.eligible or row.basis != target.basis:
                raise SourceChanged("Состав отправления изменился. Стикер нужно получить заново")
            return callback(tx, row, source, target)

    def perform(self, job):
        try:
            self._perform(job)
        except LeaseLost:
            return
        except SourceChanged as error:
            self.fail(job, str(error), stale=True)
        except (ReadError, httpx.HTTPError) as error:
            self.fail(
                job,
                str(error) if isinstance(error, ReadError) else "Ozon временно недоступен",
                retry=not isinstance(error, ReadError) or error.retryable,
                delay=error.retry_after if isinstance(error, ReadError) else None,
            )
        except SQLAlchemyError:
            self.fail(job, "База данных временно недоступна", retry=True)
        except DomainError as error:
            self.fail(job, str(error))
        except Exception as error:
            logging.error("label job stopped; job=%s; type=%s", job.id, type(error).__name__)
            self.fail(job, "Не удалось получить стикер. Повторите загрузку")

    def _perform(self, job):
        def begin(tx, row, source, target):
            if row.status == "ready":
                Queue.finish_in(tx, job)
                return None
            pending = tx.get(Job, job.id)
            if datetime.now(UTC) - pending.created_at > timedelta(minutes=3):
                raise DomainError(
                    ("Яндекс Маркет" if source.marketplace == "yandex" else "Ozon")
                    + " не подготовил стикер за отведённое время. Повторите загрузку"
                )
            row.status = "processing"
            return source, target, row.task_id, row.size

        saved = self._write(job, begin)
        if saved is None:
            return
        source, target, task_id, size = saved
        market = "Яндекс Маркет" if source.marketplace == "yandex" else "Ozon"
        with self.connections.reader_for_sync(source) as reader:
            before = reader.get_shipment(target.posting_number)
        if before is None or before.source != source or not target.matches(before):
            raise SourceChanged(f"{market} изменил отправление. Стикер нужно получить после сверки заказа")
        if before.state == "awaiting_assembly" and source.marketplace == "ozon":
            self._write(
                job, lambda tx, row, *_: (setattr(row, "status", "waiting_ready"), Queue.finish_in(tx, job))
            )
            return
        if (
            before.state
            not in (
                ("awaiting_assembly", "awaiting_dispatch")
                if source.marketplace == "yandex"
                else ("awaiting_dispatch",)
            )
            or before.source_substatus == "ship_failed"
        ):
            raise SourceChanged(f"{market} не подтвердил доступность стикера этого отправления")
        with self.connections.label_client(source) as client:
            if task_id is None:
                # Repeating PDF generation after a lost response is safe: it does
                # not ship an order or change its codes. The local file is unique.
                task_id = client.create(target.posting_number, size)
                self._write(job, lambda tx, row, *_: setattr(row, "task_id", task_id))
                self.defer(job)
                return
            remote = client.poll(task_id, target.posting_number)
            if remote.state == "pending":
                self.defer(job)
                return
            if remote.state != "ready" or not remote.url:
                raise ReadError("invalid_response")
            if source.marketplace == "yandex" and remote.identity_digest != next(
                (value for kind, value in target.references if kind == "layout_digest"), None
            ):
                raise SourceChanged(
                    "Яндекс Маркет изменил отгрузку или грузоместа. Повторите сверку перед печатью"
                )
            pdf = client.download(remote.url)
        validate_pdf(pdf)
        if (
            source.marketplace == "yandex"
            and len(PdfReader(io.BytesIO(pdf)).pages) != before.package_count_hint
        ):
            raise ReadError("invalid_response", marketplace="yandex")
        with self.connections.reader_for_sync(source) as reader:
            after = reader.get_shipment(target.posting_number)
        if (
            after is None
            or after.source != source
            or not target.matches(after)
            or after.state
            not in (
                ("awaiting_assembly", "awaiting_dispatch")
                if source.marketplace == "yandex"
                else ("awaiting_dispatch",)
            )
            or after.source_substatus == "ship_failed"
        ):
            raise SourceChanged(f"{market} изменил отправление во время получения стикера. Требуется сверка")

        def complete(tx, row, *_):
            row.encrypted_pdf, row.checksum, row.byte_count = (
                self.box.seal(pdf),
                hashlib.sha256(pdf).hexdigest(),
                len(pdf),
            )
            snapshot = self.orders.archive_info(tx, [row.shipment_id])[row.shipment_id]
            if source.marketplace == "yandex" and self.instances:
                digest = next(r.value for r in after.references if r.kind == "layout_digest")
                snapshot["box_units"] = self.instances.label_units(tx, row.shipment_id, digest)
            row.archive_snapshot = snapshot
            row.received_at, row.status, row.error = datetime.now(UTC), "ready", None
            Queue.finish_in(tx, job)
            tx.add(
                AuditEvent(
                    kind="shipment_label_received",
                    subject_id=row.id,
                    details={"shipment_id": str(row.shipment_id), "bytes": len(pdf)},
                )
            )

        self._write(job, complete)

    def defer(self, job):
        def save(tx, row, *_):
            state = Queue.finish_in(tx, job, retry=True, retry_at=datetime.now(UTC) + timedelta(seconds=2))
            if state == "failed":
                row.status, row.error = "failed", "Ozon не подготовил стикер. Повторите загрузку"

        self._write(job, save)

    def fail(self, job, message, *, retry=False, stale=False, delay=None):
        try:
            with self.db.transaction() as tx:
                Queue.owned(tx, job)
                row = tx.scalar(
                    select(ShipmentLabel)
                    .where(ShipmentLabel.id == UUID(job.payload["label_id"]))
                    .with_for_update()
                )
                if row is None or row.job_id != job.id:
                    raise LeaseLost("Загрузка стикера устарела")
                state = Queue.finish_in(
                    tx,
                    job,
                    retry=retry,
                    failed=not retry,
                    retry_at=datetime.now(UTC) + timedelta(seconds=min(300, max(2, delay or 2))),
                )
                row.status = "stale" if stale else "processing" if state == "pending" else "failed"
                row.error = "Не удалось получить стикер: " + message
        except LeaseLost:
            return
