"""Real PostgreSQL, contract HTTP, zero live marketplace access."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import login
from sqlalchemy import select
from test_assembly import call
from test_instance_data import (
    KONVERT,
    MUFT,
    finish_data,
    input_data,
    open_window,
    state,
)
from test_instance_data import (
    pipeline as pipeline,
)

from pack_v2.bootstrap.app import create_app
from pack_v2.infrastructure.jobs import Queue
from pack_v2.infrastructure.models import Job
from pack_v2.integrations.ozon.shipping import SHIP, OzonShippingAdapter
from pack_v2.modules.assembly.models import AssemblyDispatch, AssemblySession
from pack_v2.modules.assembly.readiness import Readiness
from pack_v2.modules.marking.models import MarkingAssignment
from pack_v2.modules.orders.models import Shipment


@pytest.fixture
def ready_pipeline(pipeline):
    assembly, data, remote, account = pipeline
    remote.ship_mode, remote.ship_calls, remote.before_ship = "ok", [], None

    def shipping(request):
        assert assembly.db.engine.pool.checkedout() == 0
        assert request.url.path == SHIP
        payload = json.loads(request.content)
        remote.ship_calls.append(payload)
        if remote.before_ship:
            callback, remote.before_ship = remote.before_ship, None
            callback(request)
        number = payload["posting_number"]
        if remote.ship_mode == "denied":
            return httpx.Response(403, json={"message": "private synthetic key and full marking"})
        if remote.ship_mode == "rate_limited":
            return httpx.Response(429, headers={"retry-after": "30"})
        if remote.ship_mode == "failed":
            remote.details_by_number[number]["substatus"] = "ship_failed"
        elif remote.ship_mode in ("ok", "timeout_applied", "malformed_applied"):
            remote.details_by_number[number]["status"] = "awaiting_deliver"
            remote.details_by_number[number]["substatus"] = "awaiting_deliver"
            remote.saved.setdefault(number, {})["updated"] = True
        if remote.ship_mode in ("timeout", "timeout_applied"):
            raise httpx.ReadTimeout("private synthetic key and full marking", request=request)
        if remote.ship_mode == "malformed_applied":
            return httpx.Response(200, json={"result": ["unexpected-number"]})
        return httpx.Response(200, json={"result": [number]})

    assembly.connections.shipping_factory = lambda source, credentials: OzonShippingAdapter(
        source, credentials, transport=httpx.MockTransport(shipping)
    )
    assembly.readiness = Readiness(assembly.db, assembly.connections, assembly.orders, data)
    return pipeline


def all_scans(pipeline, owner, view=None):
    view = finish_data(pipeline, owner, input_data(pipeline, owner, view=view))
    assembly = pipeline[0]
    for _ in range(2):
        result = call(assembly, owner, view, "scan", KONVERT)
        assert result["ok"], result
        view = result["session"]
    assert view["scanned"] == view["need"] and view["data"]["can_verify"]
    return view


def run_ready(pipeline):
    assembly = pipeline[0]
    with assembly.db.transaction() as tx:
        for row in tx.scalars(select(Job).where(Job.status == "pending")):
            row.available_at = datetime.now(UTC) - timedelta(seconds=1)
    job = Queue(assembly.db).claim(120)
    assert job and job.kind == "assembly.ready"
    assembly.readiness.perform(job)
    return job


def complete(pipeline, owner, view=None):
    view = view or all_scans(pipeline, owner)
    result = call(pipeline[0], owner, view, "complete")
    assert result["ok"] and result["session"]["status"] == "preparing", result
    return result["session"]


def test_ready_is_confirmed_by_get_and_preserves_one_package(ready_pipeline, owner, db):
    assembly, _, remote, _ = ready_pipeline
    view = complete(ready_pipeline, owner)
    assert not remote.ship_calls
    run_ready(ready_pipeline)
    current = state(ready_pipeline, owner, view)
    assert current["status"] == "preparing" and current["readiness"]["status"] == "checking"
    assert remote.ship_calls == [
        {
            "posting_number": view["posting_number"],
            "packages": [
                {
                    "products": [
                        {"product_id": 6225436164, "quantity": 2},
                        {"product_id": 634662543, "quantity": 1},
                    ]
                }
            ],
        }
    ]
    run_ready(ready_pipeline)
    current = state(ready_pipeline, owner, view)
    assert current["status"] == "ready" and current["readiness"]["status"] == "accepted"
    assert len(remote.ship_calls) == 1
    with db.transaction() as tx:
        shipment = tx.get(Shipment, UUID(view["shipment_id"]))
        assert shipment.state == "awaiting_dispatch" and shipment.source_status == "awaiting_deliver"
        assert shipment.assembly_revision == tx.scalar(select(AssemblyDispatch.source_revision))
        assert tx.scalar(select(MarkingAssignment)).released_at is None


@pytest.mark.parametrize("mode", ["timeout_applied", "malformed_applied"])
def test_lost_ack_reconciles_without_second_ship(ready_pipeline, owner, mode):
    view = complete(ready_pipeline, owner)
    ready_pipeline[2].ship_mode = mode
    run_ready(ready_pipeline)
    run_ready(ready_pipeline)
    assert state(ready_pipeline, owner, view)["status"] == "ready"
    assert len(ready_pipeline[2].ship_calls) == 1


@pytest.mark.parametrize("mode", ["pending", "timeout"])
def test_200_or_timeout_without_transition_remains_waiting_and_never_resends(ready_pipeline, owner, mode):
    view = complete(ready_pipeline, owner)
    ready_pipeline[2].ship_mode = mode
    for _ in range(3):
        run_ready(ready_pipeline)
    current = state(ready_pipeline, owner, view)
    assert current["status"] == "preparing" and current["readiness"]["status"] == "checking"
    assert len(ready_pipeline[2].ship_calls) == 1


def test_ship_failed_is_retried_only_three_times(ready_pipeline, owner):
    view = complete(ready_pipeline, owner)
    ready_pipeline[2].ship_mode = "failed"
    for _ in range(4):
        run_ready(ready_pipeline)
    current = state(ready_pipeline, owner, view)
    assert current["readiness"]["status"] == "uncertain"
    assert len(ready_pipeline[2].ship_calls) == 3
    assert "трёх" in current["readiness"]["message"]
    result = call(ready_pipeline[0], owner, current, "check_ready")
    assert result["ok"]
    run_ready(ready_pipeline)
    assert len(ready_pipeline[2].ship_calls) == 3


def test_already_ready_order_is_only_read_not_shipped_again(ready_pipeline, owner):
    assembly, _, remote, _ = ready_pipeline
    number = remote.postings[0]["posting_number"]
    remote.details_by_number[number]["status"] = "awaiting_deliver"
    view = complete(ready_pipeline, owner)
    run_ready(ready_pipeline)
    assert state(ready_pipeline, owner, view)["status"] == "ready"
    assert remote.ship_calls == []
    assert assembly.queue(owner)


def test_completion_command_is_idempotent(ready_pipeline, owner, db):
    view = all_scans(ready_pipeline, owner)
    command = uuid4()
    assembly = ready_pipeline[0]
    args = (owner, command, UUID(view["id"]), UUID(view["lease_token"]), view["revision"])
    assert assembly.complete(*args)["ok"]
    assert assembly.complete(*args)["ok"]
    with db.transaction() as tx:
        assert len(list(tx.scalars(select(AssemblyDispatch)))) == 1
        assert len(list(tx.scalars(select(Job).where(Job.kind == "assembly.ready")))) == 1


def test_cannot_complete_without_all_product_scans_and_data(ready_pipeline, owner):
    assembly = ready_pipeline[0]
    view = open_window(ready_pipeline, owner)
    assert call(assembly, owner, view, "complete")["code"] == "incomplete"
    view = call(assembly, owner, view, "scan", MUFT)["session"]
    assert call(assembly, owner, view, "complete")["code"] == "incomplete"
    assert ready_pipeline[2].ship_calls == []


@pytest.mark.parametrize("change", ["marks", "gtd", "quantity", "status", "requirement", "boxes"])
def test_remote_changes_block_shipping(ready_pipeline, owner, change):
    view = complete(ready_pipeline, owner)
    remote = ready_pipeline[2]
    number = view["posting_number"]
    product = next(p for p in remote.contexts[number]["products"] if p["product_id"] == 634662543)
    if change == "marks":
        product["exemplars"][0]["marks"] = []
    elif change == "gtd":
        product["exemplars"][0]["gtd"] = "changed"
    elif change == "quantity":
        remote.details_by_number[number]["products"][0]["quantity"] += 1
    elif change == "status":
        remote.details_by_number[number]["status"] = "cancelled"
    elif change == "requirement":
        product["is_rnpt_needed"] = True
    else:
        remote.contexts[number]["multi_box_qty"] = 2
    run_ready(ready_pipeline)
    assert state(ready_pipeline, owner, view)["readiness"]["status"] == "stale"
    assert remote.ship_calls == []


def test_local_cancel_before_ship_prevents_write(ready_pipeline, owner):
    view = complete(ready_pipeline, owner)
    assert call(ready_pipeline[0], owner, view, "cancel")["ok"]
    run_ready(ready_pipeline)
    current = state(ready_pipeline, owner, view)
    assert current["status"] == "cancelled"
    assert current["readiness"]["status"] == "rejected" and not ready_pipeline[2].ship_calls


def test_cancel_after_write_retains_mark_and_reconciles(ready_pipeline, owner, db):
    view = complete(ready_pipeline, owner)
    run_ready(ready_pipeline)
    current = state(ready_pipeline, owner, view)
    assert call(ready_pipeline[0], owner, current, "cancel")["ok"]
    run_ready(ready_pipeline)
    current = state(ready_pipeline, owner, view)
    assert current["status"] == "cancelled" and current["readiness"]["status"] == "accepted"
    with db.transaction() as tx:
        assert tx.scalar(select(MarkingAssignment)).released_at is None
    assert len(ready_pipeline[2].ship_calls) == 1


def test_key_rotation_before_dispatch_blocks_http(ready_pipeline, owner, db):
    view = complete(ready_pipeline, owner)
    assembly, _, remote, account = ready_pipeline
    from pydantic import SecretStr

    current = next(a for a in assembly.connections.list_accounts(owner) if a.id == account)
    assembly.connections.update(owner, account, current.revision, "ANEX", SecretStr("new-synthetic-key"))
    run_ready(ready_pipeline)
    with db.transaction() as tx:
        assert tx.scalar(select(AssemblyDispatch)).status == "stale"
    assert remote.ship_calls == []
    assert state(ready_pipeline, owner, view)["status"] == "stale"


def test_lost_worker_lease_cannot_send_or_save(ready_pipeline, owner, db):
    view = complete(ready_pipeline, owner)
    old = Queue(db).claim(120)
    with db.transaction() as tx:
        tx.get(Job, old.id).lease_until = datetime.now(UTC) - timedelta(seconds=1)
    new = Queue(db).claim(120)
    assert new and old.lease_token != new.lease_token
    ready_pipeline[0].readiness.perform(old)
    assert not ready_pipeline[2].ship_calls
    ready_pipeline[0].readiness.perform(new)
    assert len(ready_pipeline[2].ship_calls) == 1
    assert state(ready_pipeline, owner, view)["readiness"]["status"] == "checking"


def test_403_is_safe_and_does_not_resend_on_recheck(ready_pipeline, owner):
    view = complete(ready_pipeline, owner)
    ready_pipeline[2].ship_mode = "denied"
    run_ready(ready_pipeline)
    current = state(ready_pipeline, owner, view)
    assert current["readiness"]["can_recheck"] and "private" not in json.dumps(current)
    assert call(ready_pipeline[0], owner, current, "check_ready")["ok"]
    run_ready(ready_pipeline)
    assert len(ready_pipeline[2].ship_calls) == 1


def test_429_is_bounded_and_respects_future_poll(ready_pipeline, owner, db):
    complete(ready_pipeline, owner)
    ready_pipeline[2].ship_mode = "rate_limited"
    run_ready(ready_pipeline)
    with db.transaction() as tx:
        row = tx.scalar(select(AssemblyDispatch))
        job = tx.get(Job, row.job_id)
        assert job.available_at >= datetime.now(UTC) + timedelta(seconds=25)
        assert row.ship_started_at is None
    ready_pipeline[2].ship_mode = "ok"
    run_ready(ready_pipeline)
    run_ready(ready_pipeline)
    assert len(ready_pipeline[2].ship_calls) == 2


def test_new_completion_routes_require_csrf_and_owner(ready_pipeline, owner, settings):
    from fastapi.testclient import TestClient

    view = all_scans(ready_pipeline, owner)
    with TestClient(create_app(settings)) as client:
        app = client.app
        app.state.assembly = ready_pipeline[0]
        login(client)
        body = {"command_id": str(uuid4()), "lease_token": view["lease_token"], "revision": view["revision"]}
        for action in ("complete", "check_ready"):
            assert client.post(f"/api/v2/assembly/{view['id']}/{action}", json=body).status_code == 403


def test_preparing_session_survives_scanner_lease_expiry(ready_pipeline, owner, db):
    view = complete(ready_pipeline, owner)
    with db.transaction() as tx:
        tx.get(AssemblySession, UUID(view["id"])).lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assert state(ready_pipeline, owner, view)["status"] == "preparing"
    run_ready(ready_pipeline)
    run_ready(ready_pipeline)
    assert state(ready_pipeline, owner, view)["status"] == "ready"


def test_scan_posting_label_completes_only_after_all_units(ready_pipeline, owner):
    assembly = ready_pipeline[0]
    view = open_window(ready_pipeline, owner)
    assert call(assembly, owner, view, "scan", "ii50082908916")["code"] == "incomplete"
    view = all_scans(ready_pipeline, owner, view)
    result = call(assembly, owner, view, "scan", "ii50082908916")
    assert result["ok"] and result["session"]["status"] == "preparing"
    run_ready(ready_pipeline)
    run_ready(ready_pipeline)
    assert state(ready_pipeline, owner, result["session"])["status"] == "ready"


def test_unknown_exhaustion_then_manual_read_reconciles_without_write(ready_pipeline, owner, db):
    view = complete(ready_pipeline, owner)
    remote = ready_pipeline[2]
    remote.ship_mode = "timeout"
    run_ready(ready_pipeline)
    with db.transaction() as tx:
        row = tx.scalar(select(AssemblyDispatch))
        tx.get(Job, row.job_id).attempts = 29
    run_ready(ready_pipeline)
    current = state(ready_pipeline, owner, view)
    assert current["readiness"]["status"] == "uncertain"
    assert call(ready_pipeline[0], owner, current, "check_ready")["ok"]
    number = view["posting_number"]
    remote.details_by_number[number]["status"] = "awaiting_deliver"
    remote.saved[number]["updated"] = True
    run_ready(ready_pipeline)
    assert state(ready_pipeline, owner, view)["status"] == "ready"
    assert len(remote.ship_calls) == 1


def test_cancelled_unknown_reopen_restores_operation_without_new_ship(ready_pipeline, owner):
    view = complete(ready_pipeline, owner)
    ready_pipeline[2].ship_mode = "timeout"
    run_ready(ready_pipeline)
    current = state(ready_pipeline, owner, view)
    assert call(ready_pipeline[0], owner, current, "cancel")["ok"]
    restored = ready_pipeline[0].open(owner, uuid4(), code="ii50082908916")
    assert restored["code"] == "readiness_pending" and restored["session"]["id"] == view["id"]
    run_ready(ready_pipeline)
    assert len(ready_pipeline[2].ship_calls) == 1


def test_cancel_during_fresh_provider_read_prevents_ship(ready_pipeline, owner):
    view = complete(ready_pipeline, owner)
    assembly, _, remote, _ = ready_pipeline

    def cancel(request, payload):
        current = state(ready_pipeline, owner, view)
        assert call(assembly, owner, current, "cancel")["ok"]

    remote.before_instance = cancel
    run_ready(ready_pipeline)
    assert remote.ship_calls == []


def test_all_scans_without_provider_confirmation_cannot_ship(ready_pipeline, owner):
    from pack_v2.shared.errors import Conflict

    assembly = ready_pipeline[0]
    view = input_data(ready_pipeline, owner)
    for _ in range(2):
        view = call(assembly, owner, view, "scan", KONVERT)["session"]
    assert view["scanned"] == view["need"] and not view["data"]["can_verify"]
    with pytest.raises(Conflict):
        call(assembly, owner, view, "complete")
    assert not ready_pipeline[2].ship_calls


def test_non_marked_non_gtd_products_still_refresh_requirements_and_ship(ready_pipeline, owner):
    assembly, _, remote, _ = ready_pipeline
    number = remote.postings[0]["posting_number"]
    raw = remote.details_by_number[number]
    raw["requirements"], raw["optional"] = {}, {}
    remote.make_context(raw, cis=False, gtd=False)
    view = open_window(ready_pipeline, owner)
    for code in (MUFT, KONVERT, KONVERT):
        view = call(assembly, owner, view, "scan", code)["session"]
    assert view["data"]["can_verify"]
    view = complete(ready_pipeline, owner, view)
    run_ready(ready_pipeline)
    run_ready(ready_pipeline)
    assert state(ready_pipeline, owner, view)["status"] == "ready"
    assert len(remote.ship_calls) == 1


def test_background_sync_of_ship_failed_does_not_destroy_retry_proof(ready_pipeline, owner, db):
    assembly, _, remote, account = ready_pipeline
    view = complete(ready_pipeline, owner)
    remote.ship_mode = "failed"
    run_ready(ready_pipeline)
    number = view["posting_number"]
    remote.details_by_number[number]["status"] = "awaiting_deliver"
    with db.transaction() as tx:
        original = tx.get(Shipment, UUID(view["shipment_id"])).assembly_revision
        source = assembly.connections.source_for_sync(tx, account)
        assembly.orders.packing_refresh(tx, source, UUID(view["shipment_id"]), owner.id)
    request = Queue(db).claim(120)
    assert request.kind == "orders.sync"
    assembly.orders.perform(request)
    with db.transaction() as tx:
        shipment = tx.get(Shipment, UUID(view["shipment_id"]))
        assert shipment.transition_failed and shipment.assembly_revision == original
    assert state(ready_pipeline, owner, view)["status"] == "preparing"
    remote.ship_mode = "ok"
    run_ready(ready_pipeline)
    run_ready(ready_pipeline)
    assert state(ready_pipeline, owner, view)["status"] == "ready"
    with db.transaction() as tx:
        shipment = tx.get(Shipment, UUID(view["shipment_id"]))
        assert not shipment.transition_failed and shipment.state == "awaiting_dispatch"
    assert len(remote.ship_calls) == 2


def test_late_order_snapshot_cannot_roll_back_confirmed_readiness(ready_pipeline, owner, db):
    from types import SimpleNamespace

    from pack_v2.modules.orders import repository

    assembly, _, remote, account = ready_pipeline
    view = all_scans(ready_pipeline, owner)
    with db.transaction() as tx:
        source = assembly.connections.source_for_sync(tx, account)
    with assembly.connections.reader_for_sync(source) as reader:
        old = reader.get_shipment(view["posting_number"])
    view = complete(ready_pipeline, owner, view)
    run_ready(ready_pipeline)
    run_ready(ready_pipeline)
    with db.transaction() as tx:
        repository.apply_page(
            tx, SimpleNamespace(account_id=account, id=uuid4()), (old,), assembly.orders.catalog
        )
    assert state(ready_pipeline, owner, view)["status"] == "ready"
    assert assembly.orders.shipment(owner, UUID(view["shipment_id"])).shipment.state == "awaiting_dispatch"


def test_stale_unknown_operation_blocks_new_ship_after_local_revision_change(ready_pipeline, owner, db):
    assembly, data, remote, account = ready_pipeline
    view = complete(ready_pipeline, owner)
    remote.ship_mode = "timeout"
    run_ready(ready_pipeline)
    with db.transaction() as tx:
        shipment = tx.get(Shipment, UUID(view["shipment_id"]))
        shipment.assembly_revision += 1
    run_ready(ready_pipeline)
    with db.transaction() as tx:
        assert tx.scalar(select(AssemblyDispatch)).status == "stale"
    current = state(ready_pipeline, owner, view)
    assert call(assembly, owner, current, "cancel")["ok"]
    restored = assembly.open(owner, uuid4(), code="ii50082908916")
    assert restored["code"] == "readiness_pending" and restored["session"]["id"] == view["id"]
    assert len(remote.ship_calls) == 1


def test_preflight_failure_without_ship_can_be_reauthorized_after_new_checks(ready_pipeline, owner):
    assembly, _, remote, _ = ready_pipeline
    view = complete(ready_pipeline, owner)
    number = view["posting_number"]
    remote.details_by_number[number]["status"] = "cancelled"
    run_ready(ready_pipeline)
    assert not remote.ship_calls
    current = state(ready_pipeline, owner, view)
    assert current["readiness"]["status"] == "stale"
    assert call(assembly, owner, current, "cancel")["ok"]
    remote.details_by_number[number]["status"] = "awaiting_packaging"
    view = open_window(ready_pipeline, owner)
    for code in (MUFT, KONVERT, KONVERT):
        view = call(assembly, owner, view, "scan", code)["session"]
    view = finish_data(ready_pipeline, owner, view)
    view = complete(ready_pipeline, owner, view)
    run_ready(ready_pipeline)
    run_ready(ready_pipeline)
    assert state(ready_pipeline, owner, view)["status"] == "ready"
    assert len(remote.ship_calls) == 1


def test_old_acceptance_cannot_confirm_order_returned_to_awaiting_assembly(ready_pipeline, owner, db):
    assembly, _, remote, account = ready_pipeline
    view = complete(ready_pipeline, owner)
    run_ready(ready_pipeline)
    run_ready(ready_pipeline)
    assert state(ready_pipeline, owner, view)["status"] == "ready"
    remote.details_by_number[view["posting_number"]]["status"] = "awaiting_packaging"
    with db.transaction() as tx:
        source = assembly.connections.source_for_sync(tx, account)
        assembly.orders.packing_refresh(tx, source, UUID(view["shipment_id"]), owner.id)
    request = Queue(db).claim(120)
    assert request.kind == "orders.sync"
    assembly.orders.perform(request)
    assert state(ready_pipeline, owner, view)["status"] == "stale"
    view = open_window(ready_pipeline, owner)
    for code in (MUFT, KONVERT, KONVERT):
        view = call(assembly, owner, view, "scan", code)["session"]
    view = finish_data(ready_pipeline, owner, view)
    view = complete(ready_pipeline, owner, view)
    run_ready(ready_pipeline)
    assert state(ready_pipeline, owner, view)["status"] == "preparing"
    assert len(remote.ship_calls) == 1


def test_previous_operator_cannot_recheck_new_operators_dispatch(ready_pipeline, owner, identity):
    from conftest import PASSWORD

    from pack_v2.shared.errors import Conflict

    assembly, _, remote, _ = ready_pipeline
    view = complete(ready_pipeline, owner)
    cancelled = call(assembly, owner, view, "cancel")["session"]
    run_ready(ready_pipeline)
    other = identity.create_user(owner, "next-packer", PASSWORD, "packer", frozenset({"pack"}))
    new_view = open_window(ready_pipeline, other)
    for code in (MUFT, KONVERT, KONVERT):
        new_view = call(assembly, other, new_view, "scan", code)["session"]
    new_view = finish_data(ready_pipeline, other, new_view)
    complete(ready_pipeline, other, new_view)
    with pytest.raises(Conflict):
        call(assembly, owner, cancelled, "check_ready")
    assert remote.ship_calls == []
