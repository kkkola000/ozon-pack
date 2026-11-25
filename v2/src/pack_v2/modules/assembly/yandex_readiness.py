"""Exact FBS readback before and after a durable, one-attempt READY_TO_SHIP request."""

from datetime import UTC, datetime

from pack_v2.infrastructure.jobs import Queue
from pack_v2.infrastructure.models import AuditEvent
from pack_v2.integrations.public import ReadError
from pack_v2.shared.errors import Conflict

WAITING = "Яндекс Маркет обрабатывает сборку. Ожидаем подтверждения готовности к отгрузке"


def run(service, job, source, manifest, evidence, started, cancelled):
    with service.connections.instance_client(source) as client:
        fresh, observed = service.instances.check_yandex_dispatch(client, manifest, evidence)
    native = fresh.posting["yandex"]
    if native["substatus"] == "READY_TO_SHIP":

        def accept(tx, row, session, source, manifest, evidence):
            service.instances.observe_yandex_dispatch(tx, row.context_id, manifest, fresh)
            service.orders.packing_observe(tx, source, observed)
            if service.documents:
                service.documents.request(tx, source, row.shipment_id, retry=True)
            row.status, row.finished_at, row.error = "accepted", datetime.now(UTC), None
            if session.status != "cancelled":
                session.status, session.finished_at, session.message = "ready", datetime.now(UTC), None
                session.revision += 1
            Queue.finish_in(tx, job)
            tx.add(
                AuditEvent(
                    kind="assembly_readiness_confirmed",
                    subject_id=row.id,
                    details={"shipment_id": str(row.shipment_id), "marketplace": "yandex"},
                )
            )

        service.write(job, accept)
        return
    if started:
        service.fail(job, WAITING, retry=True)
        return
    if cancelled:
        raise Conflict("Локальная сборка отменена до передачи запроса Яндекс Маркету")

    def intent(tx, row, session, *_):
        if session.status == "cancelled" or row.ship_started_at:
            raise Conflict("Повторная передача готовности без сверки запрещена")
        row.ship_started_at, row.send_count, row.retry_allowed = datetime.now(UTC), row.send_count + 1, False
        tx.add(
            AuditEvent(
                kind="assembly_ship_started",
                subject_id=row.id,
                details={"shipment_id": str(row.shipment_id), "marketplace": "yandex"},
            )
        )

    with service.connections.shipping_client(source) as client:
        service.write(job, intent)
        try:
            client.ship(
                {
                    "posting_number": manifest.posting_number,
                    "campaign_id": native["campaign_id"],
                    "layout_digest": native["layout_digest"],
                    "data_digest": native["data_digest"],
                }
            )
        except ReadError as error:
            if error.status is not None and 400 <= error.status < 500:

                def refused(tx, row, *_):
                    row.ship_started_at, row.retry_allowed = None, True

                service.write(job, refused)
            raise
    service.fail(job, WAITING, retry=True)
