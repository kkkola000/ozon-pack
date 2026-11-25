"""Requirements and immutable submissions; assembly owns scanner sessions only."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from pack_v2.infrastructure.jobs import enqueue
from pack_v2.infrastructure.models import AuditEvent, Job
from pack_v2.modules.marking.public import UnitBinding
from pack_v2.shared.errors import Conflict, DomainError

from . import policy, yandex_policy
from .models import InstanceContext, Submission, UnitDetail

IN_FLIGHT = ("prepared", "checking", "uncertain")


@dataclass(frozen=True)
class Detail:
    unit_id: object
    item_id: object
    product_id: str
    exemplar_id: str
    revision: int
    allocation_id: object
    values: dict = field(repr=False)
    input_errors: tuple[str, ...] = ()


class InstanceData:
    def __init__(self, db, connections, orders, marking, box):
        self.db, self.connections, self.orders, self.marking, self.box = db, connections, orders, marking, box

    def _seal(self, value):
        return self.box.seal(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())

    def _open(self, value):
        return json.loads(self.box.open(value))

    @staticmethod
    def _context(tx, context_id):
        row = tx.get(InstanceContext, context_id) if context_id else None
        if row is None:
            raise Conflict("Требования к экземплярам ещё не получены")
        return row

    @staticmethod
    def _effective_status(tx, row):
        job = tx.get(Job, row.job_id)
        if row.status in ("queued", "reading") and (
            job.status in ("failed", "succeeded")
            or job.status == "running"
            and job.lease_until <= datetime.now(UTC)
            and job.attempts >= job.max_attempts
        ):
            return "failed"
        return row.status

    def ensure_context(self, tx, session, manifest):
        if session.instance_context_id:
            row = self._context(tx, session.instance_context_id)
            return self._effective_status(tx, row), row.error
        outstanding = tx.scalar(
            select(Submission).where(
                Submission.shipment_id == manifest.shipment_id, Submission.status.in_(IN_FLIGHT)
            )
        )
        if outstanding:
            previous = self._context(tx, outstanding.context_id)
            if (
                previous.status == "ready"
                and previous.credential_revision == session.credential_revision
                and previous.source_revision == manifest.assembly_revision
                and previous.basis == manifest.basis
            ):
                session.instance_context_id = previous.id
                return "ready", None
            raise Conflict("Предыдущая передача данных не завершена. Сначала проверьте её результат")
        existing = tx.scalar(
            select(InstanceContext)
            .where(
                InstanceContext.shipment_id == manifest.shipment_id,
                InstanceContext.status.in_(("queued", "reading")),
            )
            .with_for_update()
        )
        if existing and (
            existing.credential_revision != session.credential_revision
            or existing.source_revision != manifest.assembly_revision
            or existing.basis != manifest.basis
        ):
            existing.status = "stale"
            tx.flush()
            existing = None
        if existing is None:
            context_id = uuid4()
            job_id = enqueue(
                tx,
                "instances.discover",
                "instances.discover:" + str(context_id),
                {"context_id": str(context_id)},
            )
            existing = InstanceContext(
                policy_version=policy.POLICY_VERSION,
                id=context_id,
                account_id=manifest.account_id,
                order_id=manifest.order_id,
                shipment_id=manifest.shipment_id,
                credential_revision=session.credential_revision,
                source_revision=manifest.assembly_revision,
                basis=manifest.basis,
                posting_number=manifest.posting_number,
                job_id=job_id,
            )
            tx.add(existing)
            tx.flush()
        session.instance_context_id = existing.id
        return self._effective_status(tx, existing), existing.error

    def _details(self, tx, row):
        return [
            Detail(
                d.unit_id,
                d.item_id,
                d.product_id,
                d.exemplar_id,
                d.revision,
                d.allocation_id,
                self._open(d.encrypted_values),
                tuple(d.input_errors),
            )
            for d in tx.scalars(
                select(UnitDetail)
                .where(UnitDetail.shipment_id == row.shipment_id)
                .order_by(UnitDetail.product_id, UnitDetail.exemplar_id)
            )
        ]

    @staticmethod
    def _guard_context(row, manifest):
        if (
            row.status != "ready"
            or row.source_revision != manifest.assembly_revision
            or row.basis != manifest.basis
            or not manifest.eligible
        ):
            raise Conflict("Требования или состав отправления изменились. Нужна повторная сверка")

    def bind(self, tx, actor, context_id, manifest, planned):
        row = self._context(tx, context_id)
        self._guard_context(row, manifest)
        if any(r["unknown"] for r in row.requirements.values()):
            raise Conflict("Площадка вернула неполные требования к Честному знаку или ГТД. Повторите сверку")
        if any(r["unsupported_missing"] for r in row.requirements.values()):
            raise Conflict(
                "Площадка требует дополнительные сведения (РНПТ, вес, IMEI или страну). Укажите их в кабинете и повторите сверку"
            )
        snapshot = self._open(row.encrypted_snapshot)
        by_item = {i.item_id: i for i in manifest.items}
        existing = {
            d.unit_id: d
            for d in tx.scalars(select(UnitDetail).where(UnitDetail.shipment_id == row.shipment_id))
        }
        current_marks = self.marking.values(tx, row.shipment_id, existing)
        if snapshot.get("marketplace") == "yandex":
            yandex_policy.align_snapshot(snapshot, planned, by_item, existing, current_marks)
            row.encrypted_snapshot = self._seal(snapshot)
        source = {str(p["product_id"]): p["exemplars"] for p in snapshot["products"]}
        for item_id, item in by_item.items():
            units = sorted((u for u in planned if u.item_id == item_id), key=lambda u: u.ordinal)
            key = policy.product_key(item)
            slots = source[key]
            if len(units) != len(slots):
                raise Conflict("Распределение экземпляров не совпадает с отправлением")
            known = {existing[u.unit_id].exemplar_id for u in units if u.unit_id in existing}
            free = iter(slot for slot in slots if slot["exemplar_id"] not in known)
            for unit in units:
                detail = existing.get(unit.unit_id)
                slot = (
                    next((s for s in slots if s["exemplar_id"] == detail.exemplar_id), None)
                    if detail
                    else next(free, None)
                )
                if slot is None:
                    raise Conflict(
                        "Идентификаторы экземпляров изменились. Требуется сверка привязки Честного знака"
                    )
                if detail and (
                    detail.exemplar_id != slot["exemplar_id"]
                    or detail.product_id != key
                    or detail.allocation_id != unit.allocation_id
                ):
                    raise Conflict(
                        "Идентификаторы экземпляров изменились. Требуется сверка привязки Честного знака"
                    )
                remote_gtd = slot.get("gtd") if not slot.get("is_gtd_absent") else None
                if detail is None:
                    values = {"base_gtd": remote_gtd}
                    if remote_gtd:
                        values["gtd"] = remote_gtd
                    detail = UnitDetail(
                        unit_id=unit.unit_id,
                        account_id=manifest.account_id,
                        order_id=manifest.order_id,
                        shipment_id=manifest.shipment_id,
                        allocation_id=unit.allocation_id,
                        item_id=item_id,
                        product_id=key,
                        exemplar_id=slot["exemplar_id"],
                        encrypted_values=self._seal(values),
                    )
                    tx.add(detail)
                else:
                    values = self._open(detail.encrypted_values)
                    if remote_gtd != values.get("base_gtd"):
                        if values.get("gtd") not in (values.get("base_gtd"), remote_gtd):
                            raise Conflict("ГТД изменена и в панели, и на площадке. Сверьте данные")
                        values = {"base_gtd": remote_gtd, **({"gtd": remote_gtd} if remote_gtd else {})}
                        detail.encrypted_values, detail.revision = self._seal(values), detail.revision + 1
                remote_mark = next(
                    (m["mark"] for m in slot.get("marks", []) if m["mark_type"] == "mandatory_mark"), None
                )
                local_mark = current_marks.get(unit.unit_id)
                # Yandex can return only the short CIS. Its identity still proves that
                # a previously accepted assignment has not been removed remotely.
                if (
                    local_mark
                    and snapshot.get("marketplace") == "yandex"
                    and yandex_policy.matches(slot.get("native_short_cis"), local_mark.raw)
                ):
                    remote_mark = local_mark.raw
                if local_mark and (
                    not remote_mark or not self.marking.equivalent(remote_mark, local_mark.raw)
                ):
                    # A fresh complete read after cancellation may confirm external deletion.
                    # Never resurrect an accepted code automatically or release its reservation
                    # before a replacement has been confirmed by the marketplace.
                    elsewhere = any(
                        self.marking.equivalent(m["mark"], local_mark.raw)
                        for p in snapshot["products"]
                        for e in p["exemplars"]
                        for m in e.get("marks", [])
                        if m["mark_type"] == "mandatory_mark"
                    )
                    if not elsewhere and self.marking.accepted(tx, local_mark.assignment_id):
                        self.marking.retire(tx, local_mark.assignment_id, actor.id, "native_removed")
                        detail.revision += 1
                        detail.input_errors = [v for v in detail.input_errors if v != "cis"]
                        local_mark = None
                if remote_mark and local_mark and not self.marking.equivalent(remote_mark, local_mark.raw):
                    raise Conflict(
                        "Честный знак на площадке отличается от привязанного к единице товара. Нужна сверка"
                    )
                if remote_mark and not local_mark:
                    self.marking.assert_native_import(tx, manifest.shipment_id, unit.unit_id, remote_mark)
                    self.marking.capture(
                        tx,
                        None,
                        UnitBinding(
                            manifest.account_id,
                            manifest.order_id,
                            manifest.shipment_id,
                            unit.unit_id,
                            unit.allocation_id,
                            manifest.posting_number,
                        ),
                        remote_mark,
                        expected_gtins=self.marking.gtins(item.gtins),
                    )
        tx.flush()
        self.prepare(tx, row)

    def _fingerprint(self, tx, row):
        details = self._details(tx, row)
        marks = self.marking.values(tx, row.shipment_id, [d.unit_id for d in details])
        value = [
            row.signature,
            [
                (
                    str(d.unit_id),
                    d.product_id,
                    d.exemplar_id,
                    d.revision,
                    str(marks[d.unit_id].assignment_id) if d.unit_id in marks else None,
                    marks[d.unit_id].fingerprint if d.unit_id in marks else None,
                )
                for d in details
            ],
        ]
        return policy.digest(value), details, marks

    @staticmethod
    def _has_fields(row):
        return any(r["cis"] or r["gtd"] for r in row.requirements.values())

    def _complete(self, row, details, marks):
        return len(details) == sum(r["quantity"] for r in row.requirements.values()) and all(
            (not row.requirements[d.product_id]["cis"] or d.unit_id in marks)
            and (not row.requirements[d.product_id]["gtd"] or "gtd" in d.values)
            for d in details
        )

    def _editable(self, tx, row):
        ongoing = tx.scalar(
            select(Submission).where(
                Submission.shipment_id == row.shipment_id, Submission.status.in_(IN_FLIGHT)
            )
        )
        if ongoing:
            raise Conflict("Данные уже передаются или результат не подтверждён. Дождитесь проверки площадки")

    def _request(self, row, details, marks):
        snapshot = self._open(row.encrypted_snapshot)
        builder = yandex_policy.to_request if snapshot.get("marketplace") == "yandex" else policy.to_request
        return builder(snapshot, details, marks)

    def prepare(self, tx, row):
        fingerprint, details, marks = self._fingerprint(tx, row)
        if not self._has_fields(row) or not self._complete(row, details, marks):
            return None
        old = tx.scalar(
            select(Submission).where(Submission.context_id == row.id, Submission.fingerprint == fingerprint)
        )
        if old:
            return old.id
        self._editable(tx, row)
        request = self._request(row, details, marks)
        unit_ids = {d.product_id + ":" + d.exemplar_id: str(d.unit_id) for d in details}
        packet = {
            "request": request,
            "unit_ids": unit_ids,
            "assignments": {str(uid): str(value.assignment_id) for uid, value in marks.items()},
        }
        rejected = tx.scalar(
            select(Submission)
            .where(Submission.context_id == row.id, Submission.status == "rejected")
            .order_by(Submission.created_at.desc(), Submission.id.desc())
            .limit(1)
        )
        if rejected:
            packet["previous_rejected"] = self._open(rejected.encrypted_payload)["request"]
        batch_id = uuid4()
        job_id = enqueue(
            tx, "instances.submit", "instances.submit:" + str(batch_id), {"batch_id": str(batch_id)}
        )
        tx.get(Job, job_id).max_attempts = 30
        batch = Submission(
            id=batch_id,
            context_id=row.id,
            account_id=row.account_id,
            shipment_id=row.shipment_id,
            fingerprint=fingerprint,
            encrypted_payload=self._seal(packet),
            job_id=job_id,
        )
        tx.add(batch)
        tx.flush()
        self.marking.transmissions(tx, batch.id, [m.assignment_id for m in marks.values()], "pending")
        return batch_id

    def capture(self, tx, actor, context_id, manifest, unit_id, code):
        row = self._context(tx, context_id)
        self._guard_context(row, manifest)
        self._editable(tx, row)
        detail = tx.get(UnitDetail, unit_id)
        if (
            detail is None
            or detail.shipment_id != row.shipment_id
            or not row.requirements[detail.product_id]["cis"]
        ):
            raise Conflict("Для этой единицы не ожидается Честный знак")
        item = next(i for i in manifest.items if i.item_id == detail.item_id)
        fingerprint, _, _ = self._fingerprint(tx, row)
        previous = tx.scalar(
            select(Submission).where(Submission.context_id == row.id, Submission.fingerprint == fingerprint)
        )
        may_replace = bool(
            previous
            and previous.status == "rejected"
            and "cis" in previous.field_errors.get(str(unit_id), [])
        )
        assignment_id, changed = self.marking.capture(
            tx,
            actor.id,
            UnitBinding(
                row.account_id,
                row.order_id,
                row.shipment_id,
                unit_id,
                detail.allocation_id,
                row.posting_number,
            ),
            code,
            expected_gtins=self.marking.gtins(item.gtins),
            may_replace=may_replace,
        )
        detail.input_errors = [v for v in detail.input_errors if v != "cis"]
        if changed or may_replace:
            detail.revision += 1
            tx.flush()
            self.prepare(tx, row)
        return {"assignment_id": str(assignment_id), "changed": changed}

    def save_gtd(self, tx, actor, context_id, manifest, unit_id, value, *, absent=False):
        row = self._context(tx, context_id)
        self._guard_context(row, manifest)
        self._editable(tx, row)
        if absent and self._open(row.encrypted_snapshot).get("marketplace") == "yandex":
            raise DomainError("Яндекс Маркет требует номер ГТД. Укажите его в поле под товаром")
        value = value.strip()
        if type(absent) is not bool or absent and value:
            raise DomainError("Выберите «Не указывать» либо укажите номер ГТД")
        if self.marking.looks_like(value):
            raise DomainError("Сканируйте Честный знак в поле сканирования, а не в поле ГТД")
        if not absent and (not 1 <= len(value) <= 100 or not value.isprintable()):
            raise DomainError("Укажите номер ГТД без управляющих символов, до 100 знаков")
        detail = tx.get(UnitDetail, unit_id)
        if (
            detail is None
            or detail.shipment_id != row.shipment_id
            or not row.requirements[detail.product_id]["gtd"]
        ):
            raise Conflict("Для этой единицы не требуется ГТД")
        data = self._open(detail.encrypted_values)
        detail.input_errors = [v for v in detail.input_errors if v != "gtd"]
        fingerprint, _, _ = self._fingerprint(tx, row)
        previous = tx.scalar(
            select(Submission).where(Submission.context_id == row.id, Submission.fingerprint == fingerprint)
        )
        retry = bool(
            previous
            and previous.status == "rejected"
            and any(field in previous.field_errors.get(str(unit_id), []) for field in ("gtd", "data"))
        )
        value = None if absent else value
        if "gtd" not in data or data["gtd"] != value or retry:
            data["gtd"] = value
            detail.encrypted_values, detail.revision = self._seal(data), detail.revision + 1
            tx.add(
                AuditEvent(
                    actor_id=actor.id,
                    kind="instance_gtd_saved",
                    subject_id=unit_id,
                    details={"shipment_id": str(row.shipment_id), "revision": detail.revision},
                )
            )
            tx.flush()
            self.prepare(tx, row)

    def view(self, tx, context_id, manifest, scanned=(), last_unit_id=None):
        if context_id is None:
            return {
                "status": "pending",
                "message": "Получаем требования к Честному знаку и ГТД",
                "can_verify": False,
                "items": {},
                "pending_unit_id": None,
            }
        row = self._context(tx, context_id)
        if self._effective_status(tx, row) != "ready":
            return {
                "status": "pending"
                if self._effective_status(tx, row) in ("queued", "reading")
                else "blocked",
                "message": row.error or "Получаем требования к Честному знаку и ГТД",
                "can_verify": False,
                "items": {},
                "pending_unit_id": None,
            }
        fingerprint, details, marks = self._fingerprint(tx, row)
        batch = tx.scalar(
            select(Submission).where(Submission.context_id == row.id, Submission.fingerprint == fingerprint)
        )
        status = batch.status if batch else "needs_data" if self._has_fields(row) else "accepted"
        if status in ("prepared", "checking"):
            job = tx.get(Job, batch.job_id)
            if job.status in ("failed", "succeeded"):
                status = "uncertain"
        current = (
            row.source_revision == manifest.assembly_revision
            and row.basis == manifest.basis
            and manifest.eligible
        )
        if not current:
            status = "stale"
        proof_batches = list(tx.scalars(select(Submission.id).where(Submission.context_id == row.id)))
        accepted_marks = self.marking.proven(tx, proof_batches, [m.assignment_id for m in marks.values()])
        items = {}
        for item in manifest.items:
            req = row.requirements.get(policy.product_key(item), {})
            units = []
            for d in (d for d in details if d.item_id == item.item_id):
                failures = (batch.field_errors.get(str(d.unit_id), []) if batch else []) + list(
                    d.input_errors
                )
                proof = batch.field_results.get(str(d.unit_id), {}) if batch else {}
                units.append(
                    {
                        "id": str(d.unit_id),
                        "scanned": d.unit_id in scanned,
                        "gtd_required": bool(req.get("gtd")),
                        "gtd_omission_allowed": self._open(row.encrypted_snapshot).get("marketplace")
                        != "yandex",
                        "market_cis_status": proof.get("cis_status"),
                        "market_cis_substatus": proof.get("cis_substatus"),
                        "market_cis_message": proof.get("cis_message"),
                        "gtd": d.values.get("gtd") or "",
                        "gtd_omitted": "gtd" in d.values and d.values["gtd"] is None,
                        "gtd_status": "rejected"
                        if "gtd" in failures or "data" in failures
                        else "accepted"
                        if current and (status == "accepted" or proof.get("gtd") == "accepted")
                        else "saved"
                        if "gtd" in d.values
                        else "empty",
                        "cis_status": "rejected"
                        if "cis" in failures
                        else "accepted"
                        if current
                        and (
                            status == "accepted"
                            or proof.get("cis") == "accepted"
                            or d.unit_id in marks
                            and marks[d.unit_id].assignment_id in accepted_marks
                        )
                        and d.unit_id in marks
                        else "saved"
                        if d.unit_id in marks
                        else "empty",
                    }
                )
            marked = bool(req.get("cis"))
            color = (
                "rejected"
                if any(u["cis_status"] == "rejected" for u in units)
                else "accepted"
                if units and all(u["cis_status"] == "accepted" for u in units)
                else "pending"
            )
            items[str(item.item_id)] = {"cis": marked, "mark_status": color, "units": units}
        missing = [
            d.unit_id
            for d in details
            if d.unit_id in scanned and row.requirements[d.product_id]["cis"] and d.unit_id not in marks
        ]
        rejected = [
            d.unit_id
            for d in details
            if d.unit_id in scanned and batch and "cis" in batch.field_errors.get(str(d.unit_id), [])
        ]
        waiting = missing or rejected
        pending = last_unit_id if last_unit_id in waiting else waiting[0] if len(waiting) == 1 else None
        current = (
            row.source_revision == manifest.assembly_revision
            and row.basis == manifest.basis
            and manifest.eligible
        )
        return {
            "status": status,
            "message": batch.error
            if batch and batch.error
            else "Проверяем данные экземпляров на площадке"
            if status in ("prepared", "checking")
            else "",
            "can_verify": status == "accepted" and current and self._complete(row, details, marks),
            "items": items,
            "pending_unit_id": str(pending) if pending else None,
            "batch_id": str(batch.id) if batch else None,
        }

    def guard_marking_release(self, tx, shipment_id):
        manifest = self.orders.packing_manifest(tx, shipment_id)
        ongoing = tx.scalar(
            select(Submission.id)
            .join(InstanceContext, InstanceContext.id == Submission.context_id)
            .where(InstanceContext.order_id == manifest.order_id, Submission.status.in_(IN_FLIGHT))
            .limit(1)
        )
        if ongoing:
            raise Conflict(
                "Данные заказа передаются или результат не подтверждён. Дождитесь сверки площадки перед отвязкой"
            )

    @staticmethod
    def marking_released(tx, unit_id):
        row = tx.get(UnitDetail, unit_id)
        if row:
            row.revision += 1
            row.input_errors = [e for e in row.input_errors if e != "cis"]

    def dispatch_evidence(self, tx, context_id, manifest):
        """Trusted backend port: current, confirmed full data; never an API projection."""
        row = self._context(tx, context_id)
        self._guard_context(row, manifest)
        if not self.view(tx, context_id, manifest)["can_verify"]:
            raise Conflict("Сначала дождитесь подтверждения Честного знака и ГТД")
        fingerprint, details, marks = self._fingerprint(tx, row)
        return fingerprint, {
            "baseline": self._open(row.encrypted_snapshot),
            "requirements": row.requirements,
            "request": self._request(row, details, marks),
            "unit_ids": {(d.product_id, d.exemplar_id): str(d.unit_id) for d in details},
            "has_fields": self._has_fields(row),
        }

    def freeze_dispatch(self, tx, context_id, manifest):
        from .splitting import freeze

        return freeze(self, tx, context_id, manifest)

    def open_dispatch(self, sealed):
        from .splitting import thaw

        return thaw(self, sealed)

    def prove_split(self, client, evidence, groups, snapshots):
        from .splitting import prove

        return prove(self, client, evidence, groups, snapshots)

    def accept_split(self, tx, children, proofs, source):
        from .splitting import accept

        return accept(self, tx, children, proofs, source)

    @staticmethod
    def split_products(evidence, groups):
        from collections import Counter

        from .splitting import expected_units

        expected = expected_units(evidence)
        return [
            {
                "products": [
                    {"product_id": int(key), "quantity": count}
                    for key, count in sorted(Counter(expected[uid]["product_id"] for uid in group).items())
                ]
            }
            for group in groups
        ]

    def completed_split_view(self, tx, context_id, lines):
        req = self._context(tx, context_id).requirements
        return {
            "status": "accepted",
            "can_verify": True,
            "message": "",
            "pending_unit_id": None,
            "items": {
                str(line.item_id): {
                    "cis": req.get(next((v for k, v in line.references if k == "sku"), ""), {}).get(
                        "cis", False
                    ),
                    "mark_status": "accepted",
                    "units": [],
                }
                for line in lines
            },
        }

    @staticmethod
    def check_dispatch(client, manifest, evidence):
        """Fresh requirements and exact values are required immediately before ship."""
        fresh = client.discover(manifest.posting_number)
        baseline, req, payload = evidence["baseline"], evidence["requirements"], evidence["request"]
        if (
            fresh.posting_number != manifest.posting_number
            or fresh.source.account_id != manifest.account_id
            or fresh.source.marketplace != "ozon"
        ):
            raise Conflict("Получен ответ другого отправления или кабинета")
        if not policy.compatible(fresh, baseline, req, policy.requirements(manifest, fresh)):
            raise Conflict("Ozon изменил требования или экземпляры. Повторите проверку данных")
        if fresh.multi_box_qty != baseline.get("multi_box_qty") or not all(
            policy.same_unit(got, want, check_id=True)
            for product in fresh.products
            for got, want in zip(
                product["exemplars"],
                next(
                    p["exemplars"]
                    for p in payload["products"]
                    if str(p["product_id"]) == str(product["product_id"])
                ),
                strict=True,
            )
        ):
            raise Conflict("Данные экземпляров изменились в Ozon. Повторите проверку данных")
        posting = client.posting(manifest.posting_number)
        if evidence["has_fields"]:
            passed, errors, _ = policy.checked_status(
                client.status(manifest.posting_number), payload, req, evidence["unit_ids"]
            )
            if (
                not passed
                or errors
                or posting.get("status") == "awaiting_deliver"
                and posting.get("substatus") != "ship_failed"
                and not policy.applied(posting, payload)
            ):
                raise Conflict("Ozon не подтвердил текущие данные Честного знака и ГТД")
        return posting

    def recheck(self, tx, context_id):
        row = self._context(tx, context_id)
        fingerprint, _, _ = self._fingerprint(tx, row)
        batch = tx.scalar(
            select(Submission)
            .where(Submission.context_id == row.id, Submission.fingerprint == fingerprint)
            .with_for_update()
        )
        if batch is None or batch.status not in ("checking", "uncertain", "rejected"):
            raise Conflict("Нет незавершённой передачи для проверки")
        job = tx.get(Job, batch.job_id)
        if job.status in ("pending", "running") and not (
            job.status == "running"
            and job.lease_until <= datetime.now(UTC)
            and job.attempts >= job.max_attempts
        ):
            return
        batch.job_id = enqueue(
            tx, "instances.submit", "instances.recheck:" + str(uuid4()), {"batch_id": str(batch.id)}
        )
        tx.get(Job, batch.job_id).max_attempts = 30
        batch.status, batch.error = "checking", None

    def input_error(self, tx, context_id, unit_id, field):
        row = self._context(tx, context_id)
        detail = tx.get(UnitDetail, unit_id)
        if detail is not None and detail.shipment_id == row.shipment_id:
            detail.input_errors = sorted(set(detail.input_errors) | {field})

    def perform(self, job):
        from .jobs import InstanceJobs

        InstanceJobs(self).perform(job)


__all__ = ["InstanceData"]
