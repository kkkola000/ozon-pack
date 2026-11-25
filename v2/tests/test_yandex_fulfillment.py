"""FBS readiness, native cargo layout, original PDFs and physical-unit print plans."""

import io
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fpdf import FPDF
from pydantic import SecretStr
from pypdf import PdfReader
from sqlalchemy import select
from test_assembly import call
from test_orders import execute
from yandex_remote import BARCODE, GTD, ITEM, ORDER, YandexRemote, mark, offer

from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.jobs import Queue
from pack_v2.infrastructure.models import Job
from pack_v2.integrations.public import Connections
from pack_v2.integrations.yandex.documents import YandexLabelAdapter
from pack_v2.integrations.yandex.shipping import YandexShippingAdapter
from pack_v2.modules.assembly.public import Assembly
from pack_v2.modules.assembly.readiness import Readiness
from pack_v2.modules.catalog.public import Catalog
from pack_v2.modules.documents.archive import StickerArchive
from pack_v2.modules.documents.public import Documents
from pack_v2.modules.instance_data.public import InstanceData
from pack_v2.modules.marking.public import Marking
from pack_v2.modules.orders.public import Orders
from pack_v2.modules.printing.public import Printing


class FulfillmentRemote(YandexRemote):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.cis_status = "OK"
        self.ready_apply, self.ship_timeout, self.ship_denied = True, False, False
        self.bad_pdf, self.wrong_pages = False, False
        self.sequence = 1000
        self.raw["delivery"]["shipment"]["id"] = 555
        self.reverse_orders = False
        self.additional_orders = []

    def shipping(self, source, credentials):
        return YandexShippingAdapter(source, credentials, transport=self.transport)

    def labels(self, source, credentials):
        return YandexLabelAdapter(source, credentials, transport=self.transport)

    def unmarked(self):
        self.raw["items"][0]["requiredInstanceTypes"] = []
        self.card["offer"]["markingDeclared"] = False

    def second(self):
        self.unmarked()
        card = offer()
        card["offer"].update(offerId="OTHER", markingDeclared=False, barcodes=["MRKTOTHER"])
        card["mapping"]["marketSku"] = 9876
        self.additional_cards.append(card)
        item = deepcopy(self.raw["items"][0])
        item.update(id=int(ITEM) + 1, offerId="OTHER")
        self.raw["items"].append(item)
        self.raw["delivery"]["boxesLayout"][0]["items"].append({"id": item["id"], "fullCount": 1})

    def handle(self, request):
        assert self.db.engine.pool.checkedout() == 0, "No transaction may span marketplace HTTP"
        path = request.url.path
        if path == "/v1/businesses/123/orders" and self.additional_orders:
            data = json.loads(request.content)
            self.calls.append((request.method, path, data, request.content))
            orders = deepcopy([self.raw, *self.additional_orders])
            if data.get("orderIds"):
                orders = [r for r in orders if r["orderId"] in data["orderIds"]]
            return httpx.Response(200, json={"orders": orders})
        if path.endswith("/status") and request.method == "PUT":
            data = json.loads(request.content)
            self.calls.append((request.method, path, data, request.content))
            assert data == {"order": {"status": "PROCESSING", "substatus": "READY_TO_SHIP"}}
            if self.ship_denied:
                return httpx.Response(403, json={"error": "private details"})
            if self.ready_apply:
                self.raw["substatus"] = "READY_TO_SHIP"
            if self.ship_timeout:
                raise httpx.ReadTimeout("synthetic")
            return httpx.Response(
                200, json={"order": {"id": int(ORDER), "status": "PROCESSING", "substatus": "READY_TO_SHIP"}}
            )
        if "/delivery/shipments/" in path and path.endswith("/label"):
            self.calls.append((request.method, path, None, request.content))
            assert request.method == "GET" and request.url.params["format"] == "A7"
            if self.bad_pdf:
                return httpx.Response(200, content=b"not a PDF")
            document = FPDF(unit="mm", format=(75, 120))
            boxes = self.raw["delivery"]["boxesLayout"]
            box_id = int(path.split("/")[-2])
            index = next(i for i, b in enumerate(boxes, 1) if b["boxId"] == box_id)
            for _ in range(2 if self.wrong_pages else 1):
                document.add_page()
                document.set_font("Helvetica", size=10)
                document.text(5, 15, f"ORIGINAL YANDEX BOX {index}")
            return httpx.Response(
                200, content=bytes(document.output()), headers={"content-type": "application/pdf"}
            )
        response = super().handle(request)
        if path == "/v1/businesses/123/orders" and self.reverse_orders:
            data = response.json()
            for order in data["orders"]:
                order["delivery"]["boxesLayout"].reverse()
            response = httpx.Response(200, json=data)
        if path.endswith("/boxes") and request.method == "PUT":
            for box in self.raw["delivery"]["boxesLayout"]:
                self.sequence += 1
                box.update(boxId=self.sequence, barcode=f"seller-{self.sequence}")
        return response


@pytest.fixture
def flow(db, settings, owner):
    remote = FulfillmentRemote(db)
    box = SecretBox(settings.encryption_key.get_secret_value())
    connections = Connections(
        db,
        box,
        None,
        yandex_reader_factory=remote.reader,
        yandex_instance_factory=remote.instances,
        yandex_shipping_factory=remote.shipping,
        yandex_label_factory=remote.labels,
    )
    account = connections.create(
        owner, "Яндекс", "123", SecretStr("synthetic-yandex-key"), marketplace="yandex"
    )
    catalog = Catalog(db, connections)
    orders = Orders(db, connections, catalog)
    marks = Marking(box, settings.secret_key.get_secret_value())
    data = InstanceData(db, connections, orders, marks, box)
    docs = Documents(db, connections, orders, box)
    data.documents = docs
    docs.instances = data
    ready = Readiness(db, connections, orders, data, documents=docs)
    assembly = Assembly(
        db,
        connections,
        orders,
        box,
        settings.secret_key.get_secret_value(),
        instance_data=data,
        readiness=ready,
        documents=docs,
    )
    archive = StickerArchive(
        db, connections, orders, docs, marks, data, ready, settings.secret_key.get_secret_value()
    )
    printing = Printing(db, assembly, docs, archive, marks, orders, box)
    return assembly, data, remote, account, printing


def work(flow):
    assembly, data, _, _, _ = flow
    with assembly.db.transaction() as tx:
        for row in tx.scalars(select(Job).where(Job.status == "pending")):
            row.available_at = datetime.now(UTC) - timedelta(seconds=1)
    job = Queue(assembly.db).claim(120)
    assert job
    service = {
        "orders.sync": assembly.orders,
        "instances.discover": data,
        "instances.submit": data,
        "documents.label": assembly.documents,
        "assembly.ready": assembly.readiness,
    }[job.kind]
    service.perform(job)
    return job


def view(flow, owner, session):
    return flow[0].session(owner, UUID(session["id"]))


def until(flow, owner, session, predicate, limit=12):
    for _ in range(limit):
        current = view(flow, owner, session)
        if predicate(current):
            return current
        try:
            work(flow)
        except AssertionError:
            pytest.fail(repr(view(flow, owner, session)))
    pytest.fail(repr(view(flow, owner, session)))


def opened(flow, owner):
    assembly, _, remote, account, _ = flow
    execute(assembly.orders.catalog, owner, account, assembly.db)
    execute(assembly.orders, owner, account, assembly.db)
    initial = assembly.open(owner, uuid4(), code=ORDER)
    assert initial["ok"], initial
    work(flow)
    result = call(assembly, owner, initial["session"], "activate")
    assert result.get("pending"), result
    work(flow)
    result = call(assembly, owner, result["session"], "activate")
    assert result["ok"], result
    return result["session"]


def marked(flow, owner):
    session = opened(flow, owner)
    session = call(flow[0], owner, session, "scan", BARCODE)["session"]
    uid = next(u["id"] for i in session["data"]["items"].values() for u in i["units"])
    session = call(flow[0], owner, session, "gtd", UUID(uid), GTD)["session"]
    result = call(flow[0], owner, session, "scan", mark())
    assert result["ok"], result
    return until(flow, owner, result["session"], lambda s: s["data"]["can_verify"])


def test_ready_requires_actual_readback_and_keeps_cis_gtd(flow, owner):
    session = marked(flow, owner)
    result = call(flow[0], owner, session, "complete")
    assert result["ok"], result
    assert not any(c[0] == "PUT" and c[1].endswith("/status") for c in flow[2].calls)
    ready = until(flow, owner, result["session"], lambda s: s["status"] == "ready")
    assert ready["data"]["can_verify"]
    assert flow[2].raw["items"][0]["instances"] == [{"cis": mark(), "gtd": GTD}]
    assert len([c for c in flow[2].calls if c[0] == "PUT" and c[1].endswith("/status")]) == 1


@pytest.mark.parametrize("applied", [True, False])
@pytest.mark.parametrize("timeout", [True, False])
def test_unknown_status_write_is_never_replayed(flow, owner, applied, timeout):
    flow[2].unmarked()
    session = opened(flow, owner)
    session = call(flow[0], owner, session, "scan", BARCODE)["session"]
    flow[2].ready_apply, flow[2].ship_timeout = applied, timeout
    result = call(flow[0], owner, session, "complete")
    assert result["ok"], result
    for _ in range(7):
        work(flow)
        current = view(flow, owner, result["session"])
        if current["status"] == "ready":
            break
    assert (current["status"] == "ready") is applied
    assert len([c for c in flow[2].calls if c[0] == "PUT" and c[1].endswith("/status")]) == 1


def test_one_unit_in_two_boxes_scanned_once_and_originals_are_archived(flow, owner):
    flow[2].unmarked()
    session = opened(flow, owner)
    uid = session["layout"]["units"][0]["id"]
    saved = call(flow[0], owner, session, "layout", [[uid], [uid]])
    assert saved["ok"], saved
    session = until(flow, owner, saved["session"], lambda s: s["data"]["can_verify"])
    assert session["need"] == 1 and session["scanned"] == 0
    boxes = flow[2].raw["delivery"]["boxesLayout"]
    assert [b["items"][0]["partialCount"] for b in boxes] == [
        {"current": 1, "total": 2},
        {"current": 2, "total": 2},
    ]
    session = until(flow, owner, session, lambda s: any(label["status"] == "ready" for label in s["labels"]))
    label = session["labels"][0]
    data, name = flow[0].documents.file(owner, UUID(label["id"]), label["generation"])
    assert name.startswith("yandex-") and len(PdfReader(io.BytesIO(data)).pages) == 2
    assert flow[4].plan(owner, UUID(session["id"]))["jobs"] == []
    session = call(flow[0], owner, session, "scan", BARCODE)["session"]
    plans = flow[4].plan(owner, UUID(session["id"]))["jobs"]
    assert len(plans) == 2 and all(p["kind"] == "yandex:label" for p in plans)
    assert len({p["id"] for p in plans}) == 2
    assert flow[4].plan(owner, UUID(session["id"]))["jobs"] == plans
    for index, plan in enumerate(plans, 1):
        printed = PdfReader(io.BytesIO(flow[4].file(owner, UUID(plan["id"]))))
        assert len(printed.pages) == 1 and f"BOX {index}" in printed.pages[0].extract_text()


def test_mixed_boxes_print_only_after_their_own_products(flow, owner):
    flow[2].second()
    session = opened(flow, owner)
    units = [u["id"] for u in session["layout"]["units"]]
    result = call(flow[0], owner, session, "layout", [[units[0]], [units[1]]])
    assert result["ok"], result
    session = until(
        flow,
        owner,
        result["session"],
        lambda s: s["data"]["can_verify"] and any(label["status"] == "ready" for label in s["labels"]),
    )
    session = call(flow[0], owner, session, "scan", BARCODE)["session"]
    first = flow[4].plan(owner, UUID(session["id"]))["jobs"]
    assert len(first) == 1
    session = call(flow[0], owner, session, "scan", "MRKTOTHER")["session"]
    both = flow[4].plan(owner, UUID(session["id"]))["jobs"]
    assert len(both) == 2 and first[0] in both


def test_marked_single_item_parts_keep_exact_cis_and_gtd(flow, owner):
    session = opened(flow, owner)
    uid = session["layout"]["units"][0]["id"]
    saved = call(flow[0], owner, session, "layout", [[uid], [uid]])
    assert saved["ok"] and not any(c[0] == "PUT" for c in flow[2].calls)
    session = call(flow[0], owner, saved["session"], "scan", BARCODE)["session"]
    session = call(flow[0], owner, session, "gtd", UUID(uid), GTD)["session"]
    result = call(flow[0], owner, session, "scan", mark())
    assert result["ok"], result
    session = until(
        flow,
        owner,
        result["session"],
        lambda s: s["data"]["can_verify"] and any(label["status"] == "ready" for label in s["labels"]),
    )
    for box in flow[2].raw["delivery"]["boxesLayout"]:
        assert box["items"][0]["instances"] == [{"cis": mark(), "gtd": GTD}]
    settings = flow[4].settings(owner)
    flow[4].save(owner, settings["revision"], True, True, settings["profiles"])
    plans = flow[4].plan(owner, UUID(session["id"]))["jobs"]
    assert len([p for p in plans if p["kind"] == "marking:duplicate"]) == 1
    labels = [p for p in plans if p["kind"] == "yandex:label"]
    assert len(labels) == 2
    for plan in labels:
        document = PdfReader(io.BytesIO(flow[4].file(owner, UUID(plan["id"]))))
        assert len(document.pages) == 2
        assert BARCODE in document.pages[0].extract_text()
        assert "ORIGINAL YANDEX BOX" in document.pages[1].extract_text()


def test_cancel_reopen_adopts_external_removed_split_and_invalidates_print(flow, owner):
    flow[2].unmarked()
    session = opened(flow, owner)
    uid = session["layout"]["units"][0]["id"]
    result = call(flow[0], owner, session, "layout", [[uid], [uid]])
    session = until(
        flow,
        owner,
        result["session"],
        lambda s: s["data"]["can_verify"] and any(label["status"] == "ready" for label in s["labels"]),
    )
    session = call(flow[0], owner, session, "scan", BARCODE)["session"]
    old_jobs = flow[4].plan(owner, UUID(session["id"]))["jobs"]
    assert call(flow[0], owner, session, "cancel")["ok"]
    flow[2].raw["delivery"]["boxesLayout"] = [
        {"boxId": 9999, "barcode": "new-box", "items": [{"id": int(ITEM), "fullCount": 1}]}
    ]
    reopened = flow[0].open(owner, uuid4(), code=ORDER)
    work(flow)
    result = call(flow[0], owner, reopened["session"], "activate")
    assert result.get("pending"), result
    work(flow)
    result = call(flow[0], owner, result["session"], "activate")
    assert result["ok"], result
    session = until(
        flow, owner, result["session"], lambda s: any(label["status"] == "ready" for label in s["labels"])
    )
    assert len(session["layout"]["groups"]) == 1
    current = session["labels"][0]
    data, _ = flow[0].documents.file(owner, UUID(current["id"]), current["generation"])
    assert len(PdfReader(io.BytesIO(data)).pages) == 1
    from pack_v2.shared.errors import Conflict

    for job in old_jobs:
        with pytest.raises(Conflict):
            flow[4].file(owner, UUID(job["id"]))


def test_empty_layout_is_initialized_without_faking_readiness(flow, owner):
    flow[2].unmarked()
    flow[2].raw["delivery"]["boxesLayout"] = []
    session = opened(flow, owner)
    assert not session["data"]["can_verify"]
    session = until(
        flow,
        owner,
        session,
        lambda s: s["data"]["can_verify"] and any(label["status"] == "ready" for label in s["labels"]),
    )
    assert flow[2].raw["substatus"] == "STARTED"
    assert len(flow[2].raw["delivery"]["boxesLayout"]) == 1
    assert not any(c[1].endswith("/status") and c[0] == "PUT" for c in flow[2].calls)


@pytest.mark.parametrize("bad", ["bad_pdf", "wrong_pages"])
def test_invalid_originals_are_not_stored_or_printed(flow, owner, bad):
    flow[2].unmarked()
    setattr(flow[2], bad, True)
    session = opened(flow, owner)
    for _ in range(2):
        work(flow)
    session = view(flow, owner, session)
    assert session["labels"][0]["status"] == "failed"
    assert not session["labels"][0]["url"]
    session = call(flow[0], owner, session, "scan", BARCODE)["session"]
    assert flow[4].plan(owner, UUID(session["id"]))["jobs"] == []


def test_ready_order_is_adopted_without_new_status_write(flow, owner):
    flow[2].unmarked()
    flow[2].raw["substatus"] = "READY_TO_SHIP"
    session = opened(flow, owner)
    assert not session["layout"]["editable"]
    session = call(flow[0], owner, session, "scan", BARCODE)["session"]
    result = call(flow[0], owner, session, "complete")
    assert result["ok"], result
    until(flow, owner, result["session"], lambda s: s["status"] == "ready")
    assert not any(c[0] == "PUT" for c in flow[2].calls)


def test_cancel_after_unknown_status_write_reconciles_and_never_replays(flow, owner):
    flow[2].unmarked()
    flow[2].ship_timeout = True
    session = opened(flow, owner)
    session = call(flow[0], owner, session, "scan", BARCODE)["session"]
    result = call(flow[0], owner, session, "complete")
    assert result["ok"], result
    for _ in range(5):
        work(flow)
        if any(c[0] == "PUT" and c[1].endswith("/status") for c in flow[2].calls):
            break
    session = view(flow, owner, session)
    assert call(flow[0], owner, session, "cancel")["ok"]
    work(flow)
    assert view(flow, owner, session)["status"] == "cancelled"
    assert len([c for c in flow[2].calls if c[0] == "PUT" and c[1].endswith("/status")]) == 1


def test_final_native_data_change_blocks_ready_before_mutation(flow, owner):
    session = marked(flow, owner)
    original = flow[2].shipping

    class Changed:
        def __init__(self, source, credentials):
            self.client = original(source, credentials)

        def __enter__(self):
            self.client.__enter__()
            return self

        def __exit__(self, *args):
            return self.client.__exit__(*args)

        def ship(self, payload):
            flow[2].raw["items"][0]["instances"][0]["gtd"] = "DIFFERENT-GTD"
            for box in flow[2].raw["delivery"]["boxesLayout"]:
                box["items"][0]["instances"][0]["gtd"] = "DIFFERENT-GTD"
            return self.client.ship(payload)

    flow[0].connections.yandex_shipping_factory = Changed
    result = call(flow[0], owner, session, "complete")
    assert result["ok"], result
    until(flow, owner, result["session"], lambda s: s["readiness"] and s["readiness"]["status"] == "rejected")
    assert not any(c[0] == "PUT" and c[1].endswith("/status") for c in flow[2].calls)


def test_rotated_key_fences_status_intent(flow, owner):
    flow[2].unmarked()
    session = opened(flow, owner)
    session = call(flow[0], owner, session, "scan", BARCODE)["session"]
    original = flow[2].shipping

    class Rotated:
        def __init__(self, source, credentials):
            self.client = original(source, credentials)

        def __enter__(self):
            self.client.__enter__()
            account = flow[0].connections.list_accounts(owner)[0]
            flow[0].connections.update(
                owner, account.id, account.revision, account.title, SecretStr("synthetic-new-key")
            )
            return self.client

        def __exit__(self, *args):
            return self.client.__exit__(*args)

    flow[0].connections.yandex_shipping_factory = Rotated
    result = call(flow[0], owner, session, "complete")
    assert result["ok"], result
    for _ in range(5):
        work(flow)
        if flow[0].connections.list_accounts(owner)[0].revision > 0:
            break
    assert not any(c[0] == "PUT" and c[1].endswith("/status") for c in flow[2].calls)


def test_campaign_and_layout_digest_are_never_shipping_stickers(flow, owner):
    flow[2].unmarked()
    session = opened(flow, owner)
    session = call(flow[0], owner, session, "scan", BARCODE)["session"]
    result = call(flow[0], owner, session, "scan", "777")
    assert not result["ok"] and result["session"]["status"] == "scanning"
    assert not any(c[0] == "PUT" and c[1].endswith("/status") for c in flow[2].calls)


def test_repeated_layout_round_trip_creates_new_submission(flow, owner):
    flow[2].unmarked()
    session = opened(flow, owner)
    uid = session["layout"]["units"][0]["id"]
    for count in (2, 3, 2):
        result = call(flow[0], owner, session, "layout", [[uid] for _ in range(count)])
        assert result["ok"] and not result["session"]["data"]["can_verify"], result
        session = until(flow, owner, result["session"], lambda s: s["data"]["can_verify"])
        assert len(flow[2].raw["delivery"]["boxesLayout"]) == count
    assert len([c for c in flow[2].calls if c[0] == "PUT" and c[1].endswith("/boxes")]) == 3


def test_api_array_reordering_preserves_per_box_pdf_mapping(flow, owner):
    flow[2].second()
    flow[2].reverse_orders = True
    session = opened(flow, owner)
    units = [u["id"] for u in session["layout"]["units"]]
    result = call(flow[0], owner, session, "layout", [[units[0]], [units[1]]])
    assert result["ok"], result
    session = until(
        flow,
        owner,
        result["session"],
        lambda s: s["data"]["can_verify"] and any(label["status"] == "ready" for label in s["labels"]),
    )
    session = call(flow[0], owner, session, "scan", BARCODE)["session"]
    jobs = flow[4].plan(owner, UUID(session["id"]))["jobs"]
    assert len(jobs) == 1
    document = PdfReader(io.BytesIO(flow[4].file(owner, UUID(jobs[0]["id"]))))
    assert "BOX 1" in document.pages[0].extract_text()


def test_multiple_orders_share_campaign_metadata_without_identity_conflict(flow, owner):
    flow[2].unmarked()
    other = deepcopy(flow[2].raw)
    other["orderId"] += 1
    other["items"][0]["id"] += 20
    other["delivery"]["boxesLayout"] = [
        {
            "boxId": 988,
            "barcode": "other-order-box",
            "items": [{"id": other["items"][0]["id"], "fullCount": 1}],
        }
    ]
    flow[2].additional_orders.append(other)
    execute(flow[0].orders.catalog, owner, flow[3], flow[0].db)
    execute(flow[0].orders, owner, flow[3], flow[0].db)
    assert flow[0].orders.list_shipments(owner).total == 2


def test_upgrade_preserves_printers_and_corrects_old_campaign_identity(flow, owner, settings):
    from alembic import command

    from pack_v2.cli import migration_config
    from pack_v2.modules.orders.models import ShipmentRef
    from pack_v2.modules.printing.models import PrintSettings

    flow[2].unmarked()
    opened(flow, owner)
    configured = flow[4].settings(owner)
    configured["profiles"][0]["printer"] = "Existing printer"
    flow[4].save(owner, configured["revision"], True, True, configured["profiles"])
    command.downgrade(migration_config(settings), "0013_yandex")
    try:
        with flow[0].db.transaction() as tx:
            ref = tx.scalar(select(ShipmentRef).where(ShipmentRef.kind == "campaign_id"))
            ref.authoritative = True
        command.upgrade(migration_config(settings), "head")
        with flow[0].db.transaction() as tx:
            assert not tx.scalar(select(ShipmentRef).where(ShipmentRef.kind == "campaign_id")).authoritative
            value = tx.get(PrintSettings, 1)
            assert value.product_barcodes and value.marking_duplicates
            assert value.profiles[0]["printer"] == "Existing printer"
            assert sum(p["kind"] == "yandex:label" for p in value.profiles) == 1
    finally:
        command.upgrade(migration_config(settings), "head")


def test_document_task_for_changed_shipment_never_downloads_or_prints(flow, owner):
    from dataclasses import replace

    class ChangedTask(YandexLabelAdapter):
        def poll(self, task_id, posting_number):
            return replace(super().poll(task_id, posting_number), identity_digest="different-shipment")

    flow[0].connections.yandex_label_factory = lambda source, credentials: ChangedTask(
        source, credentials, transport=flow[2].transport
    )
    flow[2].unmarked()
    session = opened(flow, owner)
    work(flow)
    work(flow)
    session = view(flow, owner, session)
    assert session["labels"][0]["status"] == "stale"
    assert not any(c[0] == "GET" and c[1].endswith("/label") for c in flow[2].calls)
