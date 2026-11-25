"""Archive retention, private search, stable pagination and guarded manual releases."""

import hashlib
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import PASSWORD, login
from instance_remote import GTIN, mark
from sqlalchemy import event, func, select
from test_assembly import call
from test_documents import finish_labels, ready_open
from test_documents import labels as labels
from test_instance_data import KONVERT, finish_data, input_data, state
from test_instance_data import pipeline as pipeline
from test_orders import execute
from test_readiness import complete
from test_readiness import ready_pipeline as ready_pipeline

from pack_v2.infrastructure.jobs import Queue
from pack_v2.infrastructure.models import AuditEvent, Job
from pack_v2.modules.documents.archive import StickerArchive, cutoff, filters
from pack_v2.modules.documents.models import ShipmentLabel, StickerSearch
from pack_v2.modules.marking.models import MarkingAssignment
from pack_v2.modules.orders.models import Shipment
from pack_v2.shared.errors import Conflict, DomainError, Forbidden, NotFound
from pack_v2.web.deps import COOKIE


@pytest.fixture
def archive(ready_pipeline, labels, settings, owner, client):
    assembly = ready_pipeline[0]
    service = StickerArchive(
        assembly.db,
        assembly.connections,
        assembly.orders,
        assembly.documents,
        ready_pipeline[1].marking,
        ready_pipeline[1],
        assembly.readiness,
        settings.secret_key.get_secret_value(),
    )
    client.app.state.sticker_archive = service
    login(client)
    return service


def saved(client):
    return client.app.state.identity.current(client.cookies.get(COOKIE))


def saved_label(ready_pipeline, owner, db):
    return finish_labels(ready_pipeline, owner, ready_open(ready_pipeline, owner, db))


def saved_mark(ready_pipeline, owner, db):
    view = saved_label(ready_pipeline, owner, db)
    view = finish_data(ready_pipeline, owner, input_data(ready_pipeline, owner, view=view))
    assert view["data"]["can_verify"]
    return view


def make_rows(archive, ready_pipeline, owner, db, count=65):
    assembly, _, remote, account = ready_pipeline
    base = deepcopy(remote.postings[0])
    remote.postings = []
    for i in range(count):
        raw = deepcopy(base)
        raw.update(
            posting_number=f"archive-{i:04}",
            order_id=1000000 + i,
            order_number=f"order-{i:04}",
            status="awaiting_deliver",
            substatus="posting_formed",
            barcodes={"upper_barcode": f"ii-archive-{i:04}", "lower_barcode": f"archive-{i:04}"},
        )
        remote.postings.append(raw)
        remote.details_by_number[raw["posting_number"]] = raw
    execute(assembly.orders, owner, account, db)
    now = datetime.now(UTC)
    with db.transaction() as tx:
        ids = list(
            tx.scalars(
                select(Shipment.id).where(
                    Shipment.account_id == account, Shipment.source_status == "awaiting_deliver"
                )
            )
        )
        info = assembly.orders.archive_info(tx, ids)
        result = []
        for i, sid in enumerate(sorted(ids, key=lambda sid: info[sid]["posting_number"])):
            data = ready_pipeline[0].documents.box.seal(b"%PDF-test-archive")
            row = ShipmentLabel(
                id=uuid4(),
                account_id=account,
                shipment_id=sid,
                size="small",
                basis=assembly.orders.label_target(tx, sid).basis,
                credential_revision=1,
                generation=1,
                status="ready",
                encrypted_pdf=data,
                checksum=hashlib.sha256(b"%PDF-test-archive").hexdigest(),
                byte_count=len(b"%PDF-test-archive"),
                received_at=now - timedelta(seconds=i),
                archive_snapshot=info[sid],
            )
            tx.add(row)
            result.append(row.id)
        return result


def test_page_has_only_requested_filters_empty_dates_and_thirty_rows(
    archive, ready_pipeline, owner, db, client
):
    make_rows(archive, ready_pipeline, owner, db)
    page = client.get("/reports/stickers")
    assert page.status_code == 200
    assert len(re.findall("data-sticker-row=", page.text)) == 30
    assert "data-stickers-more" in page.text
    assert 'name="since" value=""' in page.text and 'name="until" value=""' in page.text
    assert "Площадка" not in page.text and 'name="state"' not in page.text
    assert "Рабочие операции будут перенесены" not in page.text
    assert "Данные в кабинете площадки остаются" in page.text


def test_keyset_pagination_is_stable_and_does_not_load_pdf(archive, ready_pipeline, owner, db, client):
    make_rows(archive, ready_pipeline, owner, db)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        first = archive.listing(saved(client))
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    assert len(first["items"]) == 30 and first["next_cursor"]
    selected = [s for s in statements if "FROM pack_v2.shipment_labels" in s]
    assert all("shipment_labels.encrypted_pdf," not in s.split("FROM")[0] for s in selected)
    second = archive.listing(saved(client), cursor=first["next_cursor"])
    last = archive.listing(saved(client), cursor=second["next_cursor"])
    assert len(second["items"]) == 30 and len(last["items"]) == 5 and not last["next_cursor"]
    ids = [r["id"] for page in (first, second, last) for r in page["items"]]
    assert len(set(ids)) == 65
    with pytest.raises(DomainError):
        archive.listing(saved(client), cursor=first["next_cursor"] + "tamper")


@pytest.mark.parametrize("query", ["ii50082908916", GTIN, mark(), mark().replace("\x1d", "<gs>")])
def test_search_finds_posting_gtin_and_exact_full_mark(archive, ready_pipeline, owner, db, client, query):
    view = saved_mark(ready_pipeline, owner, db)
    search = archive.search(saved(client), query=query)
    result = archive.listing(saved(client), search_id=search)
    assert [r["shipment_id"] for r in result["items"]] == [view["shipment_id"]]
    with db.transaction() as tx:
        stored = tx.get(StickerSearch, search).encrypted_filters
        assert query.encode() not in stored and b"<gs>" not in stored
    miss = archive.search(saved(client), query=mark(serial="OTHER00000001"))
    assert not archive.listing(saved(client), search_id=miss)["items"]


def test_search_tokens_are_private_expiring_and_do_not_put_codes_in_url(
    archive, ready_pipeline, owner, db, client, identity
):
    saved_mark(ready_pipeline, owner, db)
    csrf = saved(client).csrf_token
    response = client.post("/reports/stickers/search", data={"csrf": csrf, "q": mark()})
    assert response.status_code == 303 and "q=" not in response.headers["location"]
    assert GTIN not in response.headers["location"] and "91ABCD" not in response.headers["location"]
    search = UUID(response.headers["location"].split("search=")[1])
    identity.create_user(owner, "reader", PASSWORD, "packer", frozenset({"stickers"}))
    client.post("/logout", data={"csrf": csrf})
    login(client, "reader")
    assert client.get(response.headers["location"]).status_code == 400
    login_response = client.get("/api/v2/me")
    client.post("/logout", data={"csrf": login_response.json()["csrf"]})
    login(client)
    assert client.get(response.headers["location"]).status_code == 400
    with db.transaction() as tx:
        assert tx.get(StickerSearch, search) is not None


def test_archive_pdf_and_history_survive_delivery_and_disabled_account(
    archive, ready_pipeline, labels, owner, db, client
):
    view = saved_mark(ready_pipeline, owner, db)
    row = archive.listing(saved(client))["items"][0]
    with db.transaction() as tx:
        tx.get(Shipment, UUID(view["shipment_id"])).state = "delivered"
    account = ready_pipeline[0].connections.list_accounts(owner)[0]
    ready_pipeline[0].connections.set_active(owner, account.id, account.revision, False)
    assert archive.file(owner, UUID(row["id"]))[0] == labels.file
    response = client.get(f"/api/v2/stickers/{row['id']}/pdf?download=true")
    assert response.status_code == 200 and response.content.startswith(b"%PDF-")
    assert response.headers["content-disposition"].startswith("attachment")
    marks = archive.marking(owner, UUID(row["id"]))
    assert marks["marks"][0]["binding"] == "bound" and marks["marks"][0]["check"] == "accepted"


def test_archive_pdf_rejects_link_to_replaced_generation(archive, ready_pipeline, owner, db, client):
    saved_label(ready_pipeline, owner, db)
    entry = archive.listing(saved(client))["items"][0]
    url = f"/api/v2/stickers/{entry['id']}/pdf?generation={entry['generation']}"
    assert client.get(url).status_code == 200
    with db.transaction() as tx:
        tx.get(ShipmentLabel, UUID(entry["id"])).generation += 1
    response = client.get(url)
    assert response.status_code == 409 and "Стикер обновлён" in response.json()["detail"]
    refreshed = client.get("/reports/stickers")
    assert f"pdf?generation={entry['generation'] + 1}" in refreshed.text


def test_manual_unbind_preserves_history_never_calls_market_and_never_resurrects(
    archive, ready_pipeline, owner, db, client
):
    assembly = ready_pipeline[0]
    view = saved_mark(ready_pipeline, owner, db)
    row = archive.listing(saved(client))["items"][0]
    label_id = UUID(row["id"])
    code = archive.marking(owner, label_id)["marks"][0]
    before = len(ready_pipeline[2].instance_calls)
    result = archive.release(owner, label_id, UUID(code["id"]), code["revision"])
    assert result["marks"][0]["binding"] == "unbound"
    assert result["marks"][0]["check"] == "accepted"
    assert not result["marks"][0]["can_release"]
    assert len(ready_pipeline[2].instance_calls) == before and not ready_pipeline[2].ship_calls
    assert not state(ready_pipeline, owner, view)["data"]["can_verify"]
    assert (
        archive.release(owner, label_id, UUID(code["id"]), code["revision"])["marks"][0]["binding"]
        == "unbound"
    )
    with db.transaction() as tx:
        assignment = tx.get(MarkingAssignment, UUID(code["id"]))
        assert assignment.released_at and assignment.release_reason == "manual_admin"
        audits = list(tx.scalars(select(AuditEvent).where(AuditEvent.kind == "marking_released_admin")))
        assert len(audits) == 1 and audits[0].actor_id == owner.id
        assert mark() not in repr(audits[0].details)
    call(assembly, owner, state(ready_pipeline, owner, view), "cancel")
    opened = assembly.open(owner, uuid4(), code="ii50082908916")["session"]
    from test_instance_data import job

    job(ready_pipeline)
    opened = call(assembly, owner, opened, "activate")["session"]
    job(ready_pipeline)
    blocked = call(assembly, owner, opened, "activate")
    assert not blocked["ok"] and "отвязан администратором" in blocked["message"]


def test_admin_only_csrf_stale_revision_and_foreign_binding(
    archive, ready_pipeline, owner, db, client, identity
):
    saved_mark(ready_pipeline, owner, db)
    entry = archive.listing(saved(client))["items"][0]
    code = archive.marking(owner, UUID(entry["id"]))["marks"][0]
    url = f"/api/v2/stickers/{entry['id']}/marking/{code['id']}/unbind"
    assert client.post(url, json={"revision": code["revision"]}).status_code == 403
    csrf = saved(client).csrf_token
    assert client.post(url, json={"revision": True}, headers={"x-csrf-token": csrf}).status_code == 422
    assert client.post(url, json={"revision": 999}, headers={"x-csrf-token": csrf}).status_code == 409
    wrong = url.replace(code["id"], str(uuid4()))
    assert client.post(wrong, json={"revision": 0}, headers={"x-csrf-token": csrf}).status_code == 404
    reader = identity.create_user(owner, "archive-reader", PASSWORD, "packer", frozenset({"stickers"}))
    with pytest.raises(Forbidden):
        archive.release(reader, UUID(entry["id"]), UUID(code["id"]), code["revision"])
    client.post("/logout", data={"csrf": csrf})
    login(client, "archive-reader")
    history = client.get(f"/api/v2/stickers/{entry['id']}/marking").json()
    assert history["marks"][0]["can_release"] is False
    assert (
        client.post(url, json={"revision": 0}, headers={"x-csrf-token": saved(client).csrf_token}).status_code
        == 403
    )


def test_replayed_unbind_cannot_release_new_assignment(archive, ready_pipeline, owner, db, client):
    from pack_v2.modules.marking.public import UnitBinding

    saved_mark(ready_pipeline, owner, db)
    entry = archive.listing(saved(client))["items"][0]
    label_id = UUID(entry["id"])
    code = archive.marking(owner, label_id)["marks"][0]
    with db.transaction() as tx:
        old = tx.get(MarkingAssignment, UUID(code["id"]))
        binding = UnitBinding(
            old.account_id, old.order_id, old.shipment_id, old.unit_id, old.allocation_id, old.posting_number
        )
    archive.release(owner, label_id, UUID(code["id"]), code["revision"])
    # An explicit fresh scan is allowed; a replay of the old admin request
    # must not release the newly created reservation or inherit its proof.
    with db.transaction() as tx:
        new_id, changed = archive.marks.capture(tx, owner.id, binding, mark())
        assert changed and str(new_id) != code["id"]
    result = archive.release(owner, label_id, UUID(code["id"]), code["revision"])
    assert [m["binding"] for m in result["marks"]] == ["unbound", "bound"]
    assert result["marks"][1]["check"] == "pending"
    with db.transaction() as tx:
        assert tx.get(MarkingAssignment, new_id).released_at is None
        assert tx.scalar(select(func.count()).select_from(MarkingAssignment).where(
            MarkingAssignment.released_at.is_(None)
        )) == 1


@pytest.mark.parametrize("phase", ["prepared", "checking", "uncertain", "ship"])
def test_inflight_submission_or_shipping_blocks_unbind(archive, ready_pipeline, owner, db, client, phase):
    from pack_v2.modules.instance_data.models import Submission

    view = saved_mark(ready_pipeline, owner, db)
    entry = archive.listing(saved(client))["items"][0]
    code = archive.marking(owner, UUID(entry["id"]))["marks"][0]
    if phase == "ship":
        view = call(ready_pipeline[0], owner, view, "scan", KONVERT)["session"]
        view = call(ready_pipeline[0], owner, view, "scan", KONVERT)["session"]
        complete(ready_pipeline, owner, view)
    else:
        with db.transaction() as tx:
            tx.scalar(select(Submission)).status = phase
    with pytest.raises(Conflict):
        archive.release(owner, UUID(entry["id"]), UUID(code["id"]), code["revision"])
    assert archive.marking(owner, UUID(entry["id"]))["marks"][0]["binding"] == "bound"


def test_month_retention_expires_only_files_never_reservations(archive, ready_pipeline, owner, db, client):
    saved_mark(ready_pipeline, owner, db)
    with db.transaction() as tx:
        row = tx.scalar(select(ShipmentLabel))
        label_id = row.id
        row.received_at = cutoff(datetime.now(UTC)) - timedelta(seconds=2)
    assert not archive.listing(saved(client))["items"]
    with pytest.raises(NotFound):
        archive.file(owner, label_id)
    archive.schedule()
    job = Queue(db).claim(120)
    assert job.kind == "documents.cleanup"
    selects = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "shipment_labels" in statement:
            selects.append(statement.split("FROM", 1)[0])

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        archive.perform(job)
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    assert selects and not any("shipment_labels.encrypted_pdf" in projection for projection in selects)
    with db.transaction() as tx:
        row = tx.get(ShipmentLabel, label_id)
        assert row.status == "expired" and row.encrypted_pdf is None
        assert row.checksum is None and row.byte_count is None
        assert tx.scalar(select(MarkingAssignment.released_at)) is None


def test_calendar_month_boundary_and_moscow_date_filter(archive, ready_pipeline, owner, db, client):
    assert cutoff(datetime(2026, 8, 31, tzinfo=UTC)) == datetime(2026, 2, 28, tzinfo=UTC)
    view = saved_label(ready_pipeline, owner, db)
    at = datetime.now(UTC).replace(hour=21, minute=30, second=0, microsecond=0)
    with db.transaction() as tx:
        tx.scalar(select(ShipmentLabel)).received_at = at
    date = (at + timedelta(hours=3)).date().isoformat()
    query = archive.search(saved(client), since=date, until=date)
    assert archive.listing(saved(client), search_id=query)["items"][0]["shipment_id"] == view["shipment_id"]
    other = archive.search(saved(client), since=at.date().isoformat(), until=at.date().isoformat())
    assert not archive.listing(saved(client), search_id=other)["items"]
    with pytest.raises(DomainError):
        filters(since="2026-12-01", until="2026-01-01")


def test_full_code_and_serial_are_html_safe_and_gs_is_visible(archive, ready_pipeline, owner, db, client):
    saved_mark(ready_pipeline, owner, db)
    entry = archive.listing(saved(client))["items"][0]
    code = client.get(f"/api/v2/stickers/{entry['id']}/marking").json()["marks"][0]["code"]
    assert "<GS>" in code and "\x1d" not in code
    query = "<img src=x onerror=alert(1)>"
    search = archive.search(saved(client), query=query)
    page = client.get(f"/reports/stickers?search={search}")
    assert page.status_code == 200 and "&lt;img" in page.text
    assert query not in page.text


def test_retired_ghost_reservation_is_visible_and_can_be_released_without_invalidating_current_code(
    archive, ready_pipeline, owner, db, client
):
    from instance_remote import GTD
    from test_instance_data import job

    assembly, _, remote, _ = ready_pipeline
    view = saved_label(ready_pipeline, owner, db)
    view = input_data(ready_pipeline, owner, view=view, gtd=GTD)
    remote.instance_mode = "bad_mark"
    job(ready_pipeline)
    view = state(ready_pipeline, owner, view)
    remote.instance_mode = "ok"
    view = call(assembly, owner, view, "scan", mark(serial="REPLACEMENT01"))["session"]
    # The old reservation has not been released before marketplace confirmation.
    entry = archive.listing(saved(client))["items"][0]
    history = archive.marking(owner, UUID(entry["id"]))["marks"]
    assert len(history) == 2 and history[0]["binding"] == "retired"
    with pytest.raises(Conflict):
        archive.release(owner, UUID(entry["id"]), UUID(history[0]["id"]), history[0]["revision"])
    view = finish_data(ready_pipeline, owner, view)
    # Restore a pre-upgrade hidden reservation to exercise manual historical cleanup.
    with db.transaction() as tx:
        old = tx.get(MarkingAssignment, UUID(history[0]["id"]))
        old.released_at = old.release_reason = None
    history = archive.marking(owner, UUID(entry["id"]))["marks"]
    archive.release(owner, UUID(entry["id"]), UUID(history[0]["id"]), history[0]["revision"])
    assert state(ready_pipeline, owner, view)["data"]["can_verify"]
    assert archive.marking(owner, UUID(entry["id"]))["marks"][1]["binding"] == "bound"


def test_full_mark_is_not_sent_to_sql_as_plaintext(archive, ready_pipeline, owner, db, client):
    saved_mark(ready_pipeline, owner, db)
    search = archive.search(saved(client), query=mark())
    params = []

    def record(conn, cursor, statement, parameters, context, executemany):
        params.append(repr(parameters))

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        assert archive.listing(saved(client), search_id=search)["items"]
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    assert "abcdefghijklmnop" not in " ".join(params)


def test_sticker_permission_and_search_csrf_are_enforced(
    archive, ready_pipeline, owner, db, client, identity
):
    saved_mark(ready_pipeline, owner, db)
    assert client.post("/reports/stickers/search", data={"q": mark(), "csrf": "wrong"}).status_code == 403
    entry = archive.listing(saved(client))["items"][0]
    csrf = saved(client).csrf_token
    identity.create_user(owner, "pack-only", PASSWORD, "packer", frozenset({"pack"}))
    client.post("/logout", data={"csrf": csrf})
    login(client, "pack-only")
    assert client.get("/reports/stickers").status_code == 403
    assert client.get(f"/api/v2/stickers/{entry['id']}/pdf").status_code == 403
    assert client.get(f"/api/v2/stickers/{entry['id']}/marking").status_code == 403


def test_search_expiry_and_cursor_filter_binding(archive, ready_pipeline, owner, db, client):
    make_rows(archive, ready_pipeline, owner, db)
    first = archive.listing(saved(client))
    token = archive.search(saved(client), query="archive")
    with pytest.raises(DomainError):
        archive.listing(saved(client), search_id=token, cursor=first["next_cursor"])
    with db.transaction() as tx:
        tx.get(StickerSearch, token).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(DomainError):
        archive.listing(saved(client), search_id=token)


def test_no_pdf_does_not_hide_codes_from_admin(archive, ready_pipeline, owner, db, client):
    saved_mark(ready_pipeline, owner, db)
    with db.transaction() as tx:
        row = tx.scalar(select(ShipmentLabel))
        row.status, row.encrypted_pdf, row.checksum, row.byte_count = "failed", None, None, None
        row.received_at = None
    entry = archive.listing(saved(client))["items"][0]
    assert not entry["has_pdf"] and entry["mark_count"] == 1
    assert archive.marking(owner, UUID(entry["id"]))["marks"][0]["can_release"]


def test_upgrade_makes_old_reservations_reachable_without_inventing_pdfs(
    archive, ready_pipeline, owner, db, client, settings
):
    from alembic import command

    from pack_v2.cli import migration_config

    saved_mark(ready_pipeline, owner, db)
    with db.transaction() as tx:
        row = tx.scalar(select(ShipmentLabel))
        sid = row.shipment_id
        tx.delete(row)
    command.downgrade(migration_config(settings), "0010_labels")
    command.upgrade(migration_config(settings), "head")
    entries = archive.listing(saved(client))["items"]
    assert len(entries) == 1 and entries[0]["shipment_id"] == str(sid)
    assert entries[0]["mark_count"] == 1 and not entries[0]["has_pdf"]
    assert archive.marking(owner, UUID(entries[0]["id"]))["marks"][0]["binding"] == "bound"


def test_archive_worker_removes_expired_searches_and_is_scheduled_once(
    archive, ready_pipeline, owner, db, client
):
    archive.search(saved(client), query="first")
    with db.transaction() as tx:
        tx.scalar(select(StickerSearch)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    archive.schedule()
    archive.schedule()
    job = Queue(db).claim(120)
    archive.perform(job)
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(StickerSearch)) == 0
        assert tx.scalar(select(func.count()).select_from(Job).where(Job.kind == "documents.cleanup")) == 1
