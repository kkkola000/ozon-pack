"""Yandex FBS strategy: one durable boxes write followed by exact readback and CIS checks."""

from uuid import UUID

from pack_v2.infrastructure.models import AuditEvent
from pack_v2.integrations.public import SourceChanged

from . import policy
from . import yandex_policy as yp


def submit(jobs, job, saved):
    source, manifest, baseline, req, signature, packet, started, _ = saved
    payload = packet["request"]
    unit_ids = {tuple(k.split(":")): v for k, v in packet["unit_ids"].items()}
    with jobs.s.connections.instance_client(source) as client:
        fresh = client.discover(manifest.posting_number)
        if fresh.source != source or not policy.compatible(
            fresh, baseline, req, policy.requirements(manifest, fresh)
        ):
            raise SourceChanged("Яндекс Маркет изменил состав или требования. Повторите сверку")
        if fresh.posting["yandex"]["campaign_id"] != payload["campaign_id"]:
            raise SourceChanged("Кампания заказа изменилась. Повторите сверку")
        consistent = yp.fresh_matches(fresh, baseline, payload, after_write=started) or (
            packet.get("previous_rejected")
            and yp.fresh_matches(fresh, packet["previous_rejected"], payload, after_write=started)
        )
        if (
            yp.shape(fresh.posting["yandex"])
            not in (yp.shape(baseline["native"]), yp.shape({"boxes": payload["boxes"]}))
            or not started
            and not consistent
        ):
            raise SourceChanged("Данные или грузоместа изменились в Яндекс Маркете. Повторите сверку")
        if fresh.posting["yandex"]["status"] != "PROCESSING":
            raise SourceChanged("Заказ Яндекс Маркета больше недоступен для передачи данных экземпляров")
        jobs._posting_matches(fresh.posting, manifest)
        has_cis = any(yp.mark_of(e) for p in payload["products"] for e in p["exemplars"])
        status = client.status(manifest.posting_number) if has_cis else {"items": []}
        passed, errors, results = yp.checked(fresh, payload, req, unit_ids, status)
        # A complete status.value can prove the full cryptographic value even when
        # business orders returns only the short CIS. GTD always needs its own readback.
        if passed:
            if not consistent:
                raise SourceChanged("Данные экземпляров изменились в Яндекс Маркете. Повторите сверку")
            observed = client.get_shipment(manifest.posting_number)
            if (
                observed is None
                or observed.source != source
                or next((r.value for r in observed.references if r.kind == "layout_digest"), None)
                != fresh.posting["yandex"]["layout_digest"]
            ):
                raise SourceChanged("Раскладка Яндекс Маркета изменилась во время проверки")
            jobs.accept(job, results, fresh=fresh, observed=observed)
            return
        if started and errors:
            jobs.reject(job, errors, results, yp.message(results))
            return
        if started:
            _pending(jobs, job, results)
            return
        if fresh.posting["yandex"]["substatus"] != "STARTED":
            message = (
                "Честный знак нужно передавать в статусе заказа «Ожидает сборки». "
                "Заказ уже ожидает отгрузки; данные сохранены только в панели"
                if has_cis
                else "ГТД нужно передавать в статусе заказа «Ожидает сборки». Данные сохранены только в панели"
            )

            def record(tx, row, batch, *_):
                tx.add(
                    AuditEvent(
                        kind="yandex_instance_data_not_written",
                        subject_id=batch.id,
                        details={
                            "shipment_id": str(row.shipment_id),
                            "message": "Не записан ЧЗ на маркете"
                            if has_cis
                            else "Не записана ГТД на маркете",
                        },
                    )
                )

            jobs.write(job, record)
            jobs.reject(
                job, {uid: ["cis"] if has_cis else ["gtd"] for uid in unit_ids.values()}, results, message
            )
            return
        # A rescan after FAILED/INVALID creates a new immutable batch. Polling an
        # existing uncertain batch never repeats this mutation, even after restart.
        jobs._send(job, client, "set", payload)
    _pending(jobs, job, {})


def _pending(jobs, job, results):
    def save(tx, row, batch, *_):
        batch.field_results = results
        packet = jobs.s._open(batch.encrypted_payload)
        accepted = [
            UUID(a)
            for uid, a in packet["assignments"].items()
            if results.get(uid, {}).get("cis") == "accepted"
        ]
        if accepted:
            jobs.s.marking.transmissions(tx, batch.id, accepted, "accepted")

    jobs.write(job, save)
    jobs.failed(job, yp.message(results), retry=True)
