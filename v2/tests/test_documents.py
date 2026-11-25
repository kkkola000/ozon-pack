"""Current Ozon labels through durable jobs, actual PostgreSQL and mocked HTTP."""

import io
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import PASSWORD, login
from pydantic import SecretStr
from pypdf import PdfWriter
from sqlalchemy import func, select
from test_assembly import call
from test_instance_data import MUFT, open_window, state
from test_instance_data import pipeline as pipeline
from test_orders import execute
from test_readiness import all_scans, complete, run_ready
from test_readiness import ready_pipeline as ready_pipeline
from test_splitting import split_pipeline as split_pipeline

from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.jobs import Queue
from pack_v2.infrastructure.models import AuditEvent, Job
from pack_v2.integrations.documents import MAX_LABEL_BYTES
from pack_v2.integrations.ozon.documents import CREATE, GET, OzonLabelAdapter, label_url
from pack_v2.integrations.public import ReadError
from pack_v2.modules.documents.models import ShipmentLabel
from pack_v2.modules.documents.public import Documents, validate_pdf
from pack_v2.modules.orders.models import ShipmentItem


def pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=164.4, height=113.4)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class LabelRemote:
    def __init__(self, db):
        self.db = db
        self.calls, self.tasks = [], {}
        self.mode = "ok"
        self.file = pdf_bytes()
        self.before = None

    def handle(self, request):
        assert self.db.engine.pool.checkedout() == 0, "No DB transaction may span HTTP"
        self.calls.append((request.method, request.url.path, request.headers, bytes(request.content)))
        if self.before:
            callback, self.before = self.before, None
            callback(request)
        if request.url.path == CREATE:
            if self.mode == "denied":
                return httpx.Response(403, json={"message": "private credentials and payload"})
            if self.mode == "rate":
                return httpx.Response(429, headers={"retry-after": "10"})
            number = json.loads(request.content)["posting_numbers"][0]
            task_id = 100 + len(self.tasks)
            self.tasks[str(task_id)] = number
            if self.mode == "create_timeout":
                raise httpx.ReadTimeout("private URL and key", request=request)
            return httpx.Response(
                200, json={"result": {"tasks": [{"task_type": "small_label", "task_id": task_id}]}}
            )
        if request.url.path == GET:
            task_id = str(json.loads(request.content)["task_id"])
            assert task_id in self.tasks
            if self.mode == "pending":
                return httpx.Response(200, json={"result": {"status": {"code": "processing"}}})
            status = {"code": "success", "unprinted_postings": []}
            if self.mode == "unprinted":
                status["unprinted_postings"] = [
                    {"posting_number": self.tasks[task_id], "message": "private marking code"}
                ]
            return httpx.Response(
                200,
                json={
                    "result": {
                        "status": status,
                        "file_url": f"https://cdn.ozone.ru/labels/{task_id}.pdf?token=private",
                    }
                },
            )
        if self.mode == "bad_pdf":
            return httpx.Response(200, content=b"%PDF-1.7\ninvalid")
        return httpx.Response(200, content=self.file, headers={"content-type": "application/pdf"})

    def factory(self, source, credentials):
        transport = httpx.MockTransport(self.handle)
        return OzonLabelAdapter(source, credentials, transport=transport, file_transport=transport)


def attach(pipeline, settings):
    assembly = pipeline[0]
    remote = LabelRemote(assembly.db)
    assembly.connections.label_factory = remote.factory
    docs = Documents(
        assembly.db,
        assembly.connections,
        assembly.orders,
        SecretBox(settings.encryption_key.get_secret_value()),
    )
    assembly.documents = docs
    assembly.readiness.documents = docs
    return remote


@pytest.fixture
def labels(ready_pipeline, settings):
    return attach(ready_pipeline, settings)


def label_job(pipeline):
    assembly = pipeline[0]
    with assembly.db.transaction() as tx:
        for row in tx.scalars(select(Job).where(Job.status == "pending")):
            row.available_at = datetime.now(UTC) - timedelta(seconds=1)
    job = Queue(assembly.db).claim(120)
    assert job
    services = {
        "documents.label": assembly.documents,
        "instances.submit": pipeline[1],
        "orders.sync": assembly.orders,
        "assembly.ready": assembly.readiness,
    }
    services[job.kind].perform(job)
    return job


def ready_open(pipeline, owner, db):
    assembly, _, remote, account = pipeline
    remote.postings[0].update(status="awaiting_deliver", substatus="posting_formed")
    remote.make_context(remote.postings[0])
    execute(assembly.orders, owner, account, db)
    return open_window(pipeline, owner, code=MUFT)


def finish_labels(pipeline, owner, view):
    for _ in range(8):
        current = state(pipeline, owner, view)
        if not any(label["status"] in ("queued", "processing") for label in current["labels"]):
            return current
        label_job(pipeline)
    pytest.fail("Label jobs did not settle")


def test_ready_open_queues_once_persists_task_and_serves_original(
    pipeline, ready_pipeline, labels, owner, db
):
    assembly = ready_pipeline[0]
    view = ready_open(ready_pipeline, owner, db)
    assert view["labels"][0]["status"] == "queued" and not labels.calls
    assert view["status"] == "scanning"
    label_job(ready_pipeline)
    with db.transaction() as tx:
        row = tx.scalar(select(ShipmentLabel))
        assert row.task_id and row.encrypted_pdf is None
        task_id = row.task_id
    accepted = finish_labels(ready_pipeline, owner, view)
    item = accepted["labels"][0]
    assert item["status"] == "ready" and item["url"]
    content, filename = assembly.documents.file(owner, UUID(item["id"]), int(item["key"].split(":")[-1]))
    assert content == labels.file and view["posting_number"] in filename
    with db.transaction() as tx:
        row = tx.scalar(select(ShipmentLabel))
        assert row.encrypted_pdf != content and row.task_id == task_id and row.received_at
        assert tx.scalar(select(func.count()).select_from(ShipmentLabel)) == 1
        assert "private" not in repr([a.details for a in tx.scalars(select(AuditEvent))])
    assert sum(path == CREATE for _, path, _, _ in labels.calls) == 1
    assert len([c for c in labels.calls if c[0] == "GET"]) == 1
    cdn_headers = next(headers for method, _, headers, _ in labels.calls if method == "GET")
    assert "api-key" not in cdn_headers and "client-id" not in cdn_headers
    assert not ready_pipeline[2].ship_calls


def test_waiting_for_ready_does_not_request_or_block_chz(labels, ready_pipeline, owner, db):
    view = open_window(ready_pipeline, owner, code=MUFT)
    assert view["labels"][0]["status"] == "waiting_ready"
    assert not labels.calls
    with db.transaction() as tx:
        assert tx.scalar(select(ShipmentLabel.job_id)) is None
    view = all_scans(ready_pipeline, owner, view)
    view = complete(ready_pipeline, owner, view)
    run_ready(ready_pipeline)
    assert not labels.calls
    run_ready(ready_pipeline)
    view = finish_labels(ready_pipeline, owner, view)
    assert view["status"] == "ready" and view["labels"][0]["status"] == "ready"
    assert len(ready_pipeline[2].ship_calls) == 1


def test_pending_task_survives_worker_restart_without_duplicate_create(labels, ready_pipeline, owner, db):
    view = ready_open(ready_pipeline, owner, db)
    label_job(ready_pipeline)
    labels.mode = "pending"
    label_job(ready_pipeline)
    assert state(ready_pipeline, owner, view)["labels"][0]["status"] == "processing"
    previous = ready_pipeline[0].documents
    ready_pipeline[0].documents = Documents(previous.db, previous.connections, previous.orders, previous.box)
    labels.mode = "ok"
    assert finish_labels(ready_pipeline, owner, view)["labels"][0]["status"] == "ready"
    assert sum(path == CREATE for _, path, _, _ in labels.calls) == 1


@pytest.mark.parametrize("mode", ["denied", "unprinted", "bad_pdf"])
def test_label_errors_are_separate_and_retry_does_not_ship_or_resubmit_data(
    labels, ready_pipeline, owner, db, mode
):
    assembly = ready_pipeline[0]
    labels.mode = mode
    view = ready_open(ready_pipeline, owner, db)
    current = finish_labels(ready_pipeline, owner, view)
    assert current["status"] == "scanning" and current["labels"][0]["status"] == "failed"
    assert "private" not in json.dumps(current)
    assert not ready_pipeline[2].ship_calls
    labels.mode = "ok"
    retry = call(assembly, owner, current, "retry_label")
    assert retry["ok"]
    assert finish_labels(ready_pipeline, owner, retry["session"])["labels"][0]["status"] == "ready"


def test_changed_local_composition_discards_late_pdf(labels, ready_pipeline, owner, db):
    view = ready_open(ready_pipeline, owner, db)
    label_job(ready_pipeline)

    def change(request):
        if request.method == "GET":
            with db.transaction() as tx:
                item = tx.scalar(
                    select(ShipmentItem).where(ShipmentItem.shipment_id == UUID(view["shipment_id"]))
                )
                item.quantity += 1
        else:
            labels.before = change

    labels.before = change
    current = finish_labels(ready_pipeline, owner, view)
    assert current["status"] == "stale" and not current["labels"]
    with db.transaction() as tx:
        assert tx.scalar(select(ShipmentLabel.status)) == "stale"
        assert tx.scalar(select(ShipmentLabel.encrypted_pdf)) is None


def test_changed_native_composition_after_download_is_not_served(labels, ready_pipeline, owner, db):
    view = ready_open(ready_pipeline, owner, db)
    label_job(ready_pipeline)

    def change(request):
        if request.method == "GET":
            ready_pipeline[2].details_by_number[view["posting_number"]]["products"][0]["quantity"] += 1
        else:
            labels.before = change

    labels.before = change
    current = finish_labels(ready_pipeline, owner, view)
    assert current["labels"][0]["status"] == "stale" and current["labels"][0]["url"] is None


def test_saved_label_reused_after_cancel_and_reopen(labels, ready_pipeline, owner, db):
    assembly = ready_pipeline[0]
    view = finish_labels(ready_pipeline, owner, ready_open(ready_pipeline, owner, db))
    key = view["labels"][0]["key"]
    call(assembly, owner, view, "cancel")
    reopened = open_window(ready_pipeline, owner, code=MUFT)
    assert reopened["labels"][0]["key"] == key and reopened["labels"][0]["status"] == "ready"
    assert sum(path == CREATE for _, path, _, _ in labels.calls) == 1


def test_lost_lease_cannot_save_task_or_file(labels, ready_pipeline, owner, db):
    view = ready_open(ready_pipeline, owner, db)

    def lose(_):
        with db.transaction() as tx:
            tx.scalar(select(Job).where(Job.kind == "documents.label")).lease_token = uuid4()

    labels.before = lose
    label_job(ready_pipeline)
    with db.transaction() as tx:
        row = tx.scalar(select(ShipmentLabel))
        assert row.task_id is None and row.encrypted_pdf is None
    assert state(ready_pipeline, owner, view)["labels"][0]["status"] != "ready"


def test_rotation_of_credentials_during_http_fences_result(labels, ready_pipeline, owner, db):
    assembly = ready_pipeline[0]
    ready_open(ready_pipeline, owner, db)

    def rotate(_):
        account = assembly.connections.list_accounts(owner)[0]
        assembly.connections.update(
            owner, account.id, account.revision, account.title, SecretStr("rotated-synthetic-label-key")
        )

    labels.before = rotate
    label_job(ready_pipeline)
    with db.transaction() as tx:
        assert tx.scalar(select(ShipmentLabel.status)) == "stale"
        assert tx.scalar(select(ShipmentLabel.encrypted_pdf)) is None


def test_pdf_access_requires_pack_and_current_generation(labels, ready_pipeline, owner, db, client, identity):
    assembly = ready_pipeline[0]
    client.app.state.documents = assembly.documents
    view = finish_labels(ready_pipeline, owner, ready_open(ready_pipeline, owner, db))
    url = view["labels"][0]["url"]
    assert client.get(url).status_code == 401
    login(client)
    response = client.get(url)
    assert response.status_code == 200 and response.content == labels.file
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith("inline;")
    assert client.get(url + "?download=true").headers["content-disposition"].startswith("attachment;")
    assert client.get(url.replace("/1/pdf", "/999/pdf")).status_code == 409
    identity.create_user(owner, "reader", PASSWORD, "packer", frozenset({"orders"}))
    csrf = client.get("/api/v2/me").json()["csrf"]
    client.post("/logout", data={"csrf": csrf})
    login(client, "reader")
    assert client.get(url).status_code == 403


@pytest.mark.parametrize("keep_root", [False, True])
def test_each_confirmed_split_child_has_own_label_and_parent_is_not_used(
    split_pipeline, settings, owner, db, keep_root
):
    labels = attach(split_pipeline, settings)
    split_pipeline[2].keep_root = keep_root
    view = complete(split_pipeline, owner)
    assert not state(split_pipeline, owner, view)["labels"]
    run_ready(split_pipeline)
    run_ready(split_pipeline)
    current = finish_labels(split_pipeline, owner, view)
    assert current["status"] == "ready"
    assert len(current["labels"]) == 3 and all(label["status"] == "ready" for label in current["labels"])
    assert {
        json.loads(data)["posting_numbers"][0] for _, path, _, data in labels.calls if path == CREATE
    } == set(split_pipeline[2].split_children)
    assert (view["posting_number"] in labels.tasks.values()) is keep_root
    assert len({label["id"] for label in current["labels"]}) == 3
    with db.transaction() as tx:
        assert tx.scalar(
            select(ShipmentLabel.status).where(ShipmentLabel.shipment_id == UUID(view["shipment_id"]))
        ) == ("ready" if keep_root else "stale")


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.ozone.ru/f.pdf",
        "https://127.0.0.1/f.pdf",
        "https://evil.test/a",
        "//evil.test/a",
        "https://cdn.ozone.ru.evil.test/f.pdf",
        "https://user:pass@cdn.ozone.ru/f.pdf",
        "https://cdn.ozone.ru:8443/f.pdf",
        "https://cdn.ozone.ru/a\\b",
        "https://cdn.ozone.ru/a\nb",
    ],
)
def test_untrusted_label_urls_rejected(url):
    with pytest.raises(ReadError):
        label_url(url)


def test_pdf_validation_rejects_html_truncation_and_oversized_bodies():
    assert validate_pdf(pdf_bytes()) is None
    for data in (b"<html>wrong file</html>", b"%PDF-1.7\ntruncated", b"%PDF-" + b"a" * MAX_LABEL_BYTES):
        with pytest.raises(ReadError):
            validate_pdf(data)


def test_retry_endpoint_requires_csrf_owner_and_revision(labels, ready_pipeline, owner, db, client, identity):
    assembly = ready_pipeline[0]
    client.app.state.assembly = assembly
    labels.mode = "denied"
    view = finish_labels(ready_pipeline, owner, ready_open(ready_pipeline, owner, db))
    login(client)
    csrf = client.get("/api/v2/me").json()["csrf"]
    payload = {"command_id": str(uuid4()), "lease_token": view["lease_token"], "revision": view["revision"]}
    url = f"/api/v2/assembly/{view['id']}/retry_label"
    assert client.post(url, json=payload).status_code == 403
    labels.mode = "ok"
    first = client.post(url, json=payload, headers={"x-csrf-token": csrf})
    assert first.status_code == 200 and first.json()["ok"]
    assert client.post(url, json=payload, headers={"x-csrf-token": csrf}).status_code == 200
    stale = dict(payload, command_id=str(uuid4()))
    assert client.post(url, json=stale, headers={"x-csrf-token": csrf}).status_code == 409
    identity.create_user(owner, "other", PASSWORD, "packer", frozenset({"pack"}))
    client.post("/logout", data={"csrf": csrf})
    login(client, "other")
    csrf = client.get("/api/v2/me").json()["csrf"]
    assert client.post(url, json=stale, headers={"x-csrf-token": csrf}).status_code == 404


def test_retry_after_and_pending_limits_are_bounded(labels, ready_pipeline, owner, db):
    view = ready_open(ready_pipeline, owner, db)
    labels.mode = "rate"
    before = datetime.now(UTC)
    label_job(ready_pipeline)
    with db.transaction() as tx:
        job = tx.scalar(select(Job).where(Job.kind == "documents.label"))
        assert job.status == "pending" and job.available_at >= before + timedelta(seconds=9)
        job.max_attempts = 2
    labels.mode = "pending"
    label_job(ready_pipeline)
    current = state(ready_pipeline, owner, view)
    assert current["labels"][0]["status"] == "failed" and current["status"] == "scanning"
    assert len(labels.calls) == 2


def test_changed_barcode_invalidates_download_until_a_fresh_generation(labels, ready_pipeline, owner, db):
    from pack_v2.modules.orders.models import ShipmentRef
    from pack_v2.shared.errors import Conflict

    assembly = ready_pipeline[0]
    view = finish_labels(ready_pipeline, owner, ready_open(ready_pipeline, owner, db))
    label = view["labels"][0]
    with db.transaction() as tx:
        ref = tx.scalar(
            select(ShipmentRef).where(
                ShipmentRef.shipment_id == UUID(view["shipment_id"]), ShipmentRef.kind == "label_barcode"
            )
        )
        ref.value = "ii-new-label-code"
    with pytest.raises(Conflict):
        assembly.documents.file(owner, UUID(label["id"]), int(label["key"].split(":")[-1]))


@pytest.mark.parametrize("target", ["https://evil.test/private.pdf", "http://127.0.0.1/private.pdf"])
def test_download_redirect_never_visits_untrusted_host(target):
    from pack_v2.integrations.contracts import OzonCredentials
    from pack_v2.integrations.public import AccountSource

    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": target})

    transport = httpx.MockTransport(handle)
    with OzonLabelAdapter(
        AccountSource(uuid4(), "ozon", 1),
        OzonCredentials("123", SecretStr("private-api-key")),
        transport=transport,
        file_transport=transport,
    ) as client:
        with pytest.raises(ReadError):
            client.download("https://cdn.ozone.ru/label.pdf")
    assert len(calls) == 1 and "api-key" not in calls[0].headers


def test_origin_credentials_are_removed_on_cdn_redirect():
    from pack_v2.integrations.contracts import OzonCredentials
    from pack_v2.integrations.public import AccountSource

    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "https://cdn.ozone.ru/label.pdf"})
        return httpx.Response(200, content=pdf_bytes())

    transport = httpx.MockTransport(handle)
    with OzonLabelAdapter(
        AccountSource(uuid4(), "ozon", 1),
        OzonCredentials("123", SecretStr("private-api-key")),
        transport=transport,
        file_transport=transport,
    ) as client:
        assert client.download("/generated-label.pdf").startswith(b"%PDF-")
    assert calls[0].headers["api-key"] == "private-api-key"
    assert "api-key" not in calls[1].headers and "client-id" not in calls[1].headers


def test_same_scanit_and_barcode_are_one_label_identity(labels, ready_pipeline, owner, db):
    raw = ready_pipeline[2].postings[0]
    raw["scanit"] = raw["barcodes"]["upper_barcode"]
    view = ready_open(ready_pipeline, owner, db)
    assert finish_labels(ready_pipeline, owner, view)["labels"][0]["status"] == "ready"


@pytest.mark.parametrize("size,expected", [("small", "12"), ("big", "11")])
def test_exact_requested_label_format_is_chosen(size, expected):
    from pack_v2.integrations.contracts import OzonCredentials
    from pack_v2.integrations.public import AccountSource

    def handle(request):
        assert request.url.path == CREATE
        assert json.loads(request.content) == {"posting_numbers": ["synthetic-posting"]}
        return httpx.Response(
            200,
            json={
                "result": {
                    "tasks": [
                        {"task_type": "big_label", "task_id": 11},
                        {"task_type": "small_label", "task_id": 12},
                    ]
                }
            },
        )

    transport = httpx.MockTransport(handle)
    with OzonLabelAdapter(
        AccountSource(uuid4(), "ozon", 1),
        OzonCredentials("123", SecretStr("synthetic-key")),
        transport=transport,
        file_transport=transport,
    ) as client:
        assert client.create("synthetic-posting", size) == expected


@pytest.mark.parametrize("code", ["processing", "unknown_status"])
def test_file_url_does_not_override_nonfinal_status(code):
    from pack_v2.integrations.contracts import OzonCredentials
    from pack_v2.integrations.public import AccountSource

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "result": {"status": {"code": code}, "file_url": "https://cdn.ozone.ru/file.pdf"},
            },
        )
    )
    with OzonLabelAdapter(
        AccountSource(uuid4(), "ozon", 1),
        OzonCredentials("123", SecretStr("synthetic-key")),
        transport=transport,
        file_transport=transport,
    ) as client:
        if code == "processing":
            assert client.poll("123", "synthetic-posting").state == "pending"
        else:
            with pytest.raises(ReadError):
                client.poll("123", "synthetic-posting")
