"""The server grants one dispatch; the client owns the local printer connection."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from pack_v2.infrastructure.models import AuditEvent
from pack_v2.shared.errors import Conflict, Forbidden, NotFound

from . import pdf, profiles, signing
from .models import PrintJob, PrintSettings


class Printing:
    def __init__(self, db, assembly, documents, archive, marking, orders, box):
        self.db, self.assembly, self.documents, self.archive = db, assembly, documents, archive
        self.marking, self.orders, self.box = marking, orders, box

    @staticmethod
    def _settings(tx, *, lock=False):
        tx.execute(
            insert(PrintSettings).values(id=1, profiles=profiles.default_profiles()).on_conflict_do_nothing()
        )
        return tx.get(PrintSettings, 1, with_for_update=lock, populate_existing=True)

    @staticmethod
    def _setup(row):
        return {
            "revision": row.revision,
            "product_barcodes": row.product_barcodes,
            "marking_duplicates": row.marking_duplicates,
            "profiles": row.profiles,
        }

    def settings(self, actor):
        actor.require("printing")
        with self.db.transaction() as tx:
            return self._setup(self._settings(tx))

    def save(self, actor, revision, product_barcodes, marking_duplicates, rows):
        actor.require("printing")
        rows = profiles.validate(rows)
        with self.db.transaction() as tx:
            row = self._settings(tx, lock=True)
            if revision != row.revision:
                raise Conflict("Настройки изменены другим сотрудником. Обновите страницу")
            row.profiles, row.product_barcodes, row.marking_duplicates = (
                rows,
                product_barcodes,
                marking_duplicates,
            )
            row.revision += 1
            tx.add(
                AuditEvent(
                    actor_id=actor.id, kind="printing_settings_saved", details={"revision": row.revision}
                )
            )
            return self._setup(row)

    @staticmethod
    def _view(row):
        width, height = pdf.paper_size(row.profile)
        return {
            "id": str(row.id),
            "kind": row.kind,
            "state": row.state,
            "dispatch_token": str(row.dispatch_token),
            "profile": {**row.profile, "width": width, "height": height},
            "url": f"/api/v2/printing/jobs/{row.id}/pdf",
        }

    @staticmethod
    def _existing(tx, actor, request_id, fingerprint):
        row = tx.scalar(
            select(PrintJob).where(PrintJob.user_id == actor.id, PrintJob.request_id == request_id)
        )
        if row and row.fingerprint != fingerprint:
            raise Conflict("Команда печати уже использована для другого документа")
        return row

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

    def _create(
        self, tx, actor, request_id, fingerprint, kind, section, row, *, automatic_key=None, **values
    ):
        job_id = uuid4()
        tx.execute(
            insert(PrintJob)
            .values(
                id=job_id,
                user_id=actor.id,
                request_id=request_id,
                fingerprint=fingerprint,
                kind=kind,
                section=section,
                profile=row,
                automatic_key=automatic_key,
                dispatch_token=uuid4(),
                expires_at=datetime.now(UTC) + timedelta(hours=24),
                **values,
            )
            .on_conflict_do_nothing()
        )
        saved = tx.get(PrintJob, job_id)
        if saved is None:
            saved = self._existing(tx, actor, request_id, fingerprint)
            if saved is None and automatic_key:
                saved = tx.scalar(select(PrintJob).where(PrintJob.automatic_key == automatic_key))
            if saved is None or saved.user_id != actor.id:
                return None
        if saved.id == job_id:
            tx.add(
                AuditEvent(
                    actor_id=actor.id,
                    kind="print_prepared",
                    subject_id=job_id,
                    details={"kind": kind, "automatic": automatic_key is not None},
                )
            )
        return self._view(saved)

    def _label(
        self, tx, actor, request_id, label_id, generation, section, session_id=None, automatic_key=None
    ):
        fingerprint = self._fingerprint(["label", label_id, generation, section, session_id])
        old = self._existing(tx, actor, request_id, fingerprint)
        if old:
            return self._view(old)
        setup = self._settings(tx)
        source = self.documents.print_source(tx, label_id, generation, archived=section == "stickers")
        codes = self.orders.print_codes(tx, source["shipment_id"]) if setup.product_barcodes else []
        row = profiles.pick(setup.profiles, "ozon:label", pdf.measure(source["data"]))
        return self._create(
            tx,
            actor,
            request_id,
            fingerprint,
            "ozon:label",
            section,
            row,
            label_id=label_id,
            generation=generation,
            product_codes=codes,
            session_id=session_id,
            automatic_key=automatic_key,
        )

    def _marks(self, tx, actor, request_id, assignments, section, session_id=None, automatic_key=None):
        fingerprint = self._fingerprint(["marking", assignments, section, session_id])
        old = self._existing(tx, actor, request_id, fingerprint)
        if old:
            return self._view(old)
        for item in assignments:
            self.marking.print_source(tx, UUID(item["id"]), item["revision"])
        if section == "stickers":
            raise Forbidden("Для печати из архива укажите стикер")
        row = profiles.pick(self._settings(tx).profiles, "marking:duplicate", (58, 40))
        return self._create(
            tx,
            actor,
            request_id,
            fingerprint,
            "marking:duplicate",
            section,
            row,
            assignments=assignments,
            session_id=session_id,
            automatic_key=automatic_key,
        )

    def plan(self, actor, session_id):
        actor.require("pack")
        with self.db.transaction() as tx:
            context = self.assembly.printing_context(tx, actor, session_id)
            setup = self._settings(tx)
            jobs = []
            candidates = [
                ("label:" + item["key"], item)
                for item in context["labels"]
                if item["status"] == "ready" and context["unit_ids"]
            ]
            for key, item in candidates:
                old = tx.scalar(select(PrintJob).where(PrintJob.automatic_key == key))
                if old:
                    if old.user_id == actor.id:
                        jobs.append(self._view(old))
                    continue
                job = self._label(
                    tx, actor, uuid4(), UUID(item["id"]), item["generation"], "pack", session_id, key
                )
                if job:
                    jobs.append(job)
            if setup.marking_duplicates:
                for item in self.marking.print_candidates(tx, actor.id, context["unit_ids"]):
                    old = tx.scalar(select(PrintJob).where(PrintJob.automatic_key == item["key"]))
                    if old:
                        if old.user_id == actor.id:
                            jobs.append(self._view(old))
                        continue
                    job = self._marks(
                        tx,
                        actor,
                        uuid4(),
                        [{"id": str(item["id"]), "revision": item["revision"]}],
                        "pack",
                        session_id,
                        item["key"],
                    )
                    if job:
                        jobs.append(job)
            return {"jobs": jobs}

    def label(self, actor, request_id, label_id, generation, *, session_id=None):
        section = "pack" if session_id else "stickers"
        actor.require(section)
        with self.db.transaction() as tx:
            old = self._existing(
                tx, actor, request_id, self._fingerprint(["label", label_id, generation, section, session_id])
            )
            if old:
                return self._view(old)
            if session_id:
                context = self.assembly.printing_context(tx, actor, session_id)
                if not any(
                    item["id"] == str(label_id)
                    and item["generation"] == generation
                    and item["status"] == "ready"
                    for item in context["labels"]
                ):
                    raise Forbidden("Стикер не относится к открытому окну сборки")
            return self._label(tx, actor, request_id, label_id, generation, section, session_id)

    def marks(self, actor, request_id, label_id, assignments):
        actor.require("stickers")
        if len({item["id"] for item in assignments}) != len(assignments):
            raise Conflict("Выберите каждый код один раз")
        with self.db.transaction() as tx:
            context = self.archive.printing_context(tx, actor, label_id)
            fingerprint = self._fingerprint(["marking_archive", label_id, assignments])
            old = self._existing(tx, actor, request_id, fingerprint)
            if old:
                return self._view(old)
            for item in assignments:
                source = self.marking.print_source(tx, UUID(item["id"]), item["revision"])
                if source["shipment_id"] != context["shipment_id"]:
                    raise Conflict("Честный знак не относится к выбранному стикеру")
            row = profiles.pick(self._settings(tx).profiles, "marking:duplicate", (58, 40))
            return self._create(
                tx,
                actor,
                request_id,
                fingerprint,
                "marking:duplicate",
                "stickers",
                row,
                assignments=assignments,
                label_id=label_id,
                generation=context["generation"],
            )

    def test(self, actor, request_id, row):
        actor.require("printing")
        row = profiles.profile(**row)
        fingerprint = self._fingerprint(["test", row])
        with self.db.transaction() as tx:
            old = self._existing(tx, actor, request_id, fingerprint)
            return (
                self._view(old)
                if old
                else self._create(tx, actor, request_id, fingerprint, row["kind"], "test", row)
            )

    @staticmethod
    def _job(tx, actor, job_id):
        row = tx.get(PrintJob, job_id)
        if row is None or row.user_id != actor.id:
            raise NotFound("Задание печати не найдено")
        actor.require("printing" if row.section == "test" else row.section)
        return row

    def _guard(self, tx, actor, row, *, content=False):
        if row.expires_at <= datetime.now(UTC):
            raise Conflict("Задание печати устарело. Создайте ручную перепечатку")
        if row.session_id:
            self.assembly.printing_context(tx, actor, row.session_id)
        source = None
        if row.label_id and row.kind == "ozon:label":
            source = self.documents.print_source(
                tx, row.label_id, row.generation, archived=row.section == "stickers", content=content
            )
        codes = []
        context = (
            self.archive.printing_context(tx, actor, row.label_id)
            if row.assignments and row.section == "stickers"
            else None
        )
        if context and context["generation"] != row.generation:
            raise Conflict("Стикер изменён. Откройте список Честного знака заново")
        for item in row.assignments:
            value = self.marking.print_source(tx, UUID(item["id"]), item["revision"])
            if context and value["shipment_id"] != context["shipment_id"]:
                raise Conflict("Привязка Честного знака изменилась")
            codes.append(value["raw"])
        return source, codes

    def file(self, actor, job_id):
        with self.db.transaction() as tx:
            row = self._job(tx, actor, job_id)
            source, codes = self._guard(tx, actor, row, content=True)
            profile, kind, section, products = row.profile, row.kind, row.section, row.product_codes
        data = (
            pdf.sample(profile)
            if section == "test"
            else pdf.prepend(source["data"], products)
            if kind == "ozon:label"
            else pdf.labels(codes, profile["width"], profile["height"], marking=True)
        )
        return pdf.fit(data, profile)

    def start(self, actor, job_id, token):
        with self.db.transaction() as tx:
            row = self._job(tx, actor, job_id)
            self._guard(tx, actor, row)
            tx.refresh(row, with_for_update=True)
            if row.dispatch_token != token:
                raise Forbidden("Разрешение печати устарело")
            if row.state != "prepared":
                return {"dispatch": False, "state": row.state}
            row.state, row.started_at = "dispatching", datetime.now(UTC)
            row.delivery_token = uuid4()
            tx.add(
                AuditEvent(
                    actor_id=actor.id,
                    kind="print_dispatch_started",
                    subject_id=row.id,
                    details={"kind": row.kind},
                )
            )
            return {"dispatch": True, "state": row.state, "delivery_token": str(row.delivery_token)}

    def status(self, actor, job_id):
        with self.db.transaction() as tx:
            return {"state": self._job(tx, actor, job_id).state}

    def finish(self, actor, job_id, token, state):
        with self.db.transaction() as tx:
            row = self._job(tx, actor, job_id)
            tx.refresh(row, with_for_update=True)
            expected = row.delivery_token or row.dispatch_token
            if expected != token:
                raise Forbidden("Разрешение печати устарело")
            if row.state == state:
                return {"state": row.state}
            if row.state != "dispatching" and not (row.state == "prepared" and state == "failed"):
                raise Conflict("Результат этого задания уже сохранён")
            row.state, row.finished_at = state, datetime.now(UTC)
            tx.add(
                AuditEvent(
                    actor_id=actor.id,
                    kind="print_delivery_result",
                    subject_id=row.id,
                    details={"state": state, "kind": row.kind},
                )
            )
            return {"state": state}

    def certificate(self, actor):
        actor.require("printing")
        with self.db.transaction() as tx:
            row = self._settings(tx, lock=True)
            if not row.certificate:
                key, row.certificate = signing.create()
                row.encrypted_key = self.box.seal(key)
                tx.add(AuditEvent(actor_id=actor.id, kind="printing_certificate_created"))
            return row.certificate

    def sign(self, actor, value):
        self.certificate(actor)
        with self.db.transaction() as tx:
            row = self._settings(tx)
            key = self.box.open(row.encrypted_key)
        return signing.sign(key, value)
