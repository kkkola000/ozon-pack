"""Exact child exemplar matching and owner-controlled transfer after an observed Ozon split."""

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from pack_v2.infrastructure.jobs import enqueue
from pack_v2.infrastructure.models import Job
from pack_v2.modules.marking.public import UnitBinding
from pack_v2.shared.errors import Conflict

from . import policy
from .models import InstanceContext, Submission, UnitDetail


@dataclass(frozen=True)
class RequiredItem:
    references: tuple
    quantity: int
    required_fields: tuple
    optional_fields: tuple


@dataclass(frozen=True)
class RequiredManifest:
    items: tuple


def freeze(service, tx, context_id, manifest):
    fingerprint, evidence = service.dispatch_evidence(tx, context_id, manifest)
    evidence["unit_ids"] = [[*key, value] for key, value in evidence["unit_ids"].items()]
    return fingerprint, service._seal(evidence)


def thaw(service, sealed):
    evidence = service._open(sealed)
    evidence["unit_ids"] = {(key, exemplar): uid for key, exemplar, uid in evidence["unit_ids"]}
    return evidence


def expected_units(evidence):
    return {
        evidence["unit_ids"][str(p["product_id"]), str(e["exemplar_id"])]: {
            "product_id": str(p["product_id"]),
            "value": e,
        }
        for p in evidence["request"]["products"]
        for e in p["exemplars"]
    }


def match_group(group, expected, snapshot):
    remaining = set(group)
    mapping = []
    if Counter(expected[uid]["product_id"] for uid in group) != Counter(
        {str(p["product_id"]): p["quantity"] for p in snapshot.products}
    ):
        return None
    for product in snapshot.products:
        key = str(product["product_id"])
        for slot in product["exemplars"]:
            candidates = sorted(
                uid
                for uid in remaining
                if expected[uid]["product_id"] == key
                and policy.same_unit(slot, expected[uid]["value"], check_id=False)
            )
            if not candidates:
                return None
            # Several indistinguishable, unmarked units are interchangeable; codes are never guessed.
            if len(candidates) > 1 and any(m["mark_type"] == "mandatory_mark" for m in slot.get("marks", [])):
                raise Conflict("Неоднозначное соответствие Честного знака новым отправлениям")
            uid = candidates[0]
            remaining.remove(uid)
            mapping.append({"unit_id": uid, "product_id": key, "exemplar_id": str(slot["exemplar_id"])})
    return mapping if not remaining else None


def prove(service, client, evidence, groups, snapshots):
    expected = expected_units(evidence)
    left = list(groups)
    result = {}
    for observed in sorted(snapshots, key=lambda s: s.source_id):
        fresh = client.discover(observed.source_id)
        if fresh.source != observed.source or fresh.posting_number != observed.source_id:
            raise Conflict("Данные новых отправлений относятся к другому кабинету")
        if observed.state != "awaiting_dispatch" or observed.transition_failed:
            raise Conflict("Ozon ещё не подтвердил готовность всех новых отправлений")
        candidates = [(group, match_group(group, expected, fresh)) for group in left]
        candidates = [(group, mapping) for group, mapping in candidates if mapping is not None]
        if not candidates:
            raise Conflict(
                "Состав, Честный знак или ГТД новых отправлений не совпали с сохранённым распределением"
            )
        if len(candidates) > 1:
            signatures = {
                policy.digest(
                    sorted(
                        (
                            expected[u]["product_id"],
                            policy.digest(
                                {k: v for k, v in expected[u]["value"].items() if k != "exemplar_id"}
                            ),
                        )
                        for u in group
                    )
                )
                for group, _ in candidates
            }
            if len(signatures) != 1:
                raise Conflict("Неоднозначное распределение одинаковых товаров")
        group, mapping = candidates[0]
        left.remove(group)
        lines = RequiredManifest(
            tuple(
                RequiredItem(
                    tuple((r.kind, r.value) for r in line.product_references),
                    line.quantity,
                    line.required_fields,
                    line.optional_fields,
                )
                for line in observed.items
            )
        )
        req = policy.requirements(lines, fresh)
        for key, fields in req.items():
            previous = evidence["requirements"][key]
            fields["cis"] = fields["cis"] or previous["cis"]
            fields["gtd"] = fields["gtd"] or previous["gtd"]
        if any(r["unknown"] or r["unsupported_missing"] for r in req.values()):
            raise Conflict("Не подтверждены требования экземпляров нового отправления")
        if {str(p["product_id"]): p["quantity"] for p in fresh.products} != {
            policy.product_key(line): line.quantity for line in lines.items
        }:
            raise Conflict("Состав нового отправления изменился во время сверки")
        for entry in mapping:
            entry["source_line_id"] = next(
                line.source_line_id
                for line in observed.items
                if any(r.kind == "sku" and r.value == entry["product_id"] for r in line.product_references)
            )
        payload = {
            "posting_number": observed.source_id,
            "products": [
                {"product_id": int(p["product_id"]), "exemplars": deepcopy(p["exemplars"])}
                for p in fresh.products
            ],
        }
        # A new requirement may never be silently bypassed, even if Ozon accepted ship.
        for p in payload["products"]:
            flags = req[str(p["product_id"])]
            if any(
                flags["cis"]
                and not any(m["mark_type"] == "mandatory_mark" for m in e["marks"])
                or flags["gtd"]
                and not (
                    e.get("gtd")
                    and not e.get("is_gtd_absent")
                    or e.get("is_gtd_absent") is True
                    and not e.get("gtd")
                )
                for e in p["exemplars"]
            ):
                raise Conflict("В новом отправлении отсутствуют обязательные данные")
        unit_ids = {(r["product_id"], r["exemplar_id"]): r["unit_id"] for r in mapping}
        results = {}
        if any(r["cis"] or r["gtd"] for r in req.values()):
            passed, errors, results = policy.checked_status(
                client.status(observed.source_id), payload, req, unit_ids
            )
            if not passed or errors:
                raise Conflict("Ozon не подтвердил проверку Честного знака или ГТД в новых отправлениях")
        posting = client.posting(observed.source_id)
        if (
            posting["status"] != "awaiting_deliver"
            or posting.get("substatus") == "ship_failed"
            or not policy.applied(posting, payload)
        ):
            raise Conflict("Не подтверждено фактическое сохранение данных новых отправлений")
        result[observed.source_id] = {
            "mapping": mapping,
            "fresh": fresh,
            "requirements": req,
            "results": results,
        }
    if left:
        raise Conflict("Ozon не подтвердил все новые отправления")
    return result


def accept(service, tx, children, proofs, source):
    # Avoid transient unique conflicts if Ozon reused another unit's former exemplar ID.
    ids = [UUID(u["unit_id"]) for _, _, units, _ in children for u in units]
    for row in tx.scalars(select(UnitDetail).where(UnitDetail.unit_id.in_(ids))):
        row.exemplar_id = "relocating:" + str(row.unit_id)
    tx.flush()
    contexts = {}
    for child, observed, units, _ in children:
        manifest = service.orders.packing_manifest(tx, child.id)
        proof = proofs[observed.source_id]
        fresh, req = proof["fresh"], proof["requirements"]
        context_id = uuid4()
        job_id = enqueue(
            tx,
            "instances.discover",
            "instances.split.observed:" + str(context_id),
            {"context_id": str(context_id)},
        )
        tx.get(Job, job_id).status = "succeeded"
        payload = {
            "posting_number": observed.source_id,
            "products": fresh.products,
            "multi_box_qty": fresh.multi_box_qty,
        }
        context = InstanceContext(
            policy_version=policy.POLICY_VERSION,
            id=context_id,
            account_id=child.account_id,
            order_id=child.order_id,
            shipment_id=child.id,
            credential_revision=source.credential_revision,
            source_revision=manifest.assembly_revision,
            basis=manifest.basis,
            posting_number=observed.source_id,
            job_id=job_id,
            status="ready",
            requirements=req,
            encrypted_snapshot=service._seal(payload),
            signature=policy.signature(fresh.products, req),
            checked_at=datetime.now(UTC),
        )
        tx.add(context)
        for entry in units:
            uid = UUID(entry["unit_id"])
            detail = tx.get(UnitDetail, uid)
            if detail is None:
                raise Conflict("Не найдены исходные данные физической единицы")
            slot = next(
                e
                for p in fresh.products
                if str(p["product_id"]) == entry["product_id"]
                for e in p["exemplars"]
                if str(e["exemplar_id"]) == entry["exemplar_id"]
            )
            detail.shipment_id, detail.allocation_id, detail.item_id = (
                child.id,
                UUID(entry["allocation_id"]),
                UUID(entry["item_id"]),
            )
            detail.exemplar_id = entry["exemplar_id"]
            gtd = slot.get("gtd") if not slot.get("is_gtd_absent") else None
            detail.encrypted_values = service._seal(
                {
                    "base_gtd": gtd,
                    **({"gtd": gtd} if gtd or slot.get("is_gtd_absent") is True else {}),
                }
            )
            detail.revision += 1
            detail.input_errors = []
            service.marking.relocate(
                tx,
                UnitBinding(
                    child.account_id,
                    child.order_id,
                    child.id,
                    uid,
                    UUID(entry["allocation_id"]),
                    observed.source_id,
                ),
            )
        tx.flush()
        batch_id = service.prepare(tx, context)
        if batch_id:
            batch = tx.get(Submission, batch_id)
            batch.status, batch.finished_at, batch.field_results = (
                "accepted",
                datetime.now(UTC),
                proof["results"],
            )
            tx.get(Job, batch.job_id).status = "succeeded"
            marks = service.marking.values(tx, child.id, ids)
            service.marking.transmissions(tx, batch.id, [m.assignment_id for m in marks.values()], "accepted")
        contexts[child.id] = context_id
    tx.flush()
    return contexts
