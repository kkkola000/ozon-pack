"""Split contracts and stable unit/CIS ownership, on a separate real PostgreSQL."""

import json
from collections import defaultdict
from copy import deepcopy
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import login
from sqlalchemy import func, select
from test_assembly import call
from test_instance_data import open_window, state
from test_instance_data import pipeline as pipeline
from test_readiness import all_scans, complete, run_ready
from test_readiness import ready_pipeline as ready_pipeline

from pack_v2.bootstrap.app import create_app
from pack_v2.infrastructure.models import AuditEvent
from pack_v2.integrations.ozon.shipping import OzonShippingAdapter
from pack_v2.modules.assembly.models import AssemblyDispatch, AssemblySession
from pack_v2.modules.instance_data.models import UnitDetail
from pack_v2.modules.marking.models import MarkingAssignment, MarkingUnit
from pack_v2.modules.orders.models import OrderUnit, Shipment, ShipmentUnit


@pytest.fixture
def split_pipeline(ready_pipeline):
    assembly, _, remote, _ = ready_pipeline
    assembly.readiness.auto_split = True
    remote.split_mode, remote.split_calls, remote.split_children, remote.keep_root = "ok", [], [], False
    remote.before_split, remote.reverse_slots = None, False

    def shipping(request):
        assert assembly.db.engine.pool.checkedout() == 0
        payload = json.loads(request.content)
        remote.split_calls.append(payload)
        if remote.before_split:
            callback, remote.before_split = remote.before_split, None
            callback()
        if remote.split_mode == "denied":
            return httpx.Response(403, json={"message": "private synthetic payload"})
        if remote.split_mode == "rate":
            return httpx.Response(429, headers={"retry-after": "20"})
        if len(payload["packages"]) == 1:
            number = payload["posting_number"]
            remote.details_by_number[number].update(status="awaiting_deliver", substatus="awaiting_deliver")
            remote.saved.setdefault(number, {})["updated"] = True
            return httpx.Response(200, json={"result": [number]})
        if remote.split_mode in ("pending", "timeout"):
            if remote.split_mode == "timeout":
                raise httpx.ReadTimeout("private synthetic code", request=request)
            return httpx.Response(
                200, json={"result": [f"child-{n}" for n in range(len(payload["packages"]))]}
            )
        number = payload["posting_number"]
        original = deepcopy(remote.details_by_number[number])
        context = deepcopy(remote.contexts[number])
        numbers = [
            number if remote.keep_root and index == 0 else number + "-s" + str(index + 1)
            for index in range(len(payload["packages"]))
        ]
        positions = defaultdict(int)
        for index, (package, target) in enumerate(zip(payload["packages"], numbers, strict=True)):
            child = deepcopy(original)
            child.update(
                posting_number=target,
                status="awaiting_deliver",
                substatus="awaiting_deliver",
                parent_posting_number=number if target != number else None,
                related_postings={"related_posting_numbers": [n for n in numbers if n != target]},
                barcodes={"upper_barcode": "ii-child-" + str(index), "lower_barcode": "child-" + str(index)},
            )
            counts = {p["product_id"]: p["quantity"] for p in package["products"]}
            child["products"] = [
                dict(p, quantity=counts[p["sku"]]) for p in original["products"] if p["sku"] in counts
            ]
            products = []
            for product in context["products"]:
                key = product["product_id"]
                if key not in counts:
                    continue
                start = positions[key]
                units = deepcopy(product["exemplars"][start : start + counts[key]])
                positions[key] += counts[key]
                if remote.reverse_slots:
                    units.reverse()
                for offset, unit in enumerate(units):
                    unit["exemplar_id"] = 10000 + index * 100 + offset
                products.append(dict(product, quantity=counts[key], exemplars=units))
            remote.contexts[target] = {"posting_number": target, "multi_box_qty": 1, "products": products}
            remote.details_by_number[target] = child
            remote.saved[target] = {"updated": True}
        if not remote.keep_root:
            remote.details_by_number[number]["related_postings"] = {"related_posting_numbers": numbers}
        remote.split_children = numbers
        if remote.split_mode == "timeout_applied":
            raise httpx.ReadTimeout("private synthetic code", request=request)
        if remote.split_mode == "malformed_applied":
            return httpx.Response(200, json={"result": ["unexpected"]})
        # The API result order never determines unit/CIS identity.
        return httpx.Response(200, json={"result": list(reversed(numbers))})

    assembly.connections.shipping_factory = lambda source, credentials: OzonShippingAdapter(
        source, credentials, transport=httpx.MockTransport(shipping)
    )
    return ready_pipeline


@pytest.mark.parametrize("keep_root", [False, True])
def test_split_is_only_accepted_after_full_child_readback(split_pipeline, owner, db, keep_root):
    assembly = split_pipeline[0]
    split_pipeline[2].keep_root = keep_root
    view = complete(split_pipeline, owner)
    assert view["readiness"]["package_count"] == view["need"]
    run_ready(split_pipeline)
    current = state(split_pipeline, owner, view)
    assert current["status"] == "preparing" and not current["readiness"]["children"]
    run_ready(split_pipeline)
    current = state(split_pipeline, owner, view)
    assert current["status"] == "ready", current["readiness"]
    assert len(current["readiness"]["children"]) == 3
    assert len(split_pipeline[2].split_calls) == 1
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(OrderUnit)) == 3
        assert (
            tx.scalar(
                select(func.count()).select_from(ShipmentUnit).where(ShipmentUnit.released_at.is_(None))
            )
            == 3
        )
        assigned = tx.scalar(select(MarkingAssignment).where(MarkingAssignment.released_at.is_(None)))
        assert assigned.posting_number in split_pipeline[2].split_children
        if not keep_root:
            assert assigned.posting_number != view["posting_number"]
        assert tx.scalar(select(func.count()).select_from(MarkingUnit)) == 1
    assert all(row["verified"] for row in assembly.queue(owner))


@pytest.mark.parametrize("mode", ["timeout_applied", "malformed_applied"])
def test_lost_ack_recovers_children_without_repeating_split(split_pipeline, owner, mode):
    split_pipeline[2].split_mode = mode
    view = complete(split_pipeline, owner)
    run_ready(split_pipeline)
    run_ready(split_pipeline)
    current = state(split_pipeline, owner, view)
    assert current["status"] == "ready", current["readiness"]
    assert len(split_pipeline[2].split_calls) == 1


@pytest.mark.parametrize("mode", ["pending", "timeout"])
def test_unknown_split_never_becomes_a_scan_error_or_a_second_write(split_pipeline, owner, mode):
    split_pipeline[2].split_mode = mode
    view = complete(split_pipeline, owner)
    for _ in range(3):
        run_ready(split_pipeline)
    current = state(split_pipeline, owner, view)
    assert current["status"] == "preparing" and current["readiness"]["status"] == "checking"
    assert not current["readiness"]["children"]
    assert len(split_pipeline[2].split_calls) == 1


@pytest.mark.parametrize("change", ["code", "gtd", "status", "order", "order_id_only"])
def test_child_proof_failure_does_not_move_any_units_or_codes(split_pipeline, owner, db, change):
    view = complete(split_pipeline, owner)
    run_ready(split_pipeline)
    remote = split_pipeline[2]
    marked = next(
        n
        for n in remote.split_children
        if any(p["is_mandatory_mark_possible"] for p in remote.contexts[n]["products"])
    )
    if change == "code":
        remote.contexts[marked]["products"][0]["exemplars"][0]["marks"][0]["mark"] += "changed"
    elif change == "gtd":
        remote.contexts[marked]["products"][0]["exemplars"][0]["gtd"] += "8"
    elif change == "status":
        remote.details_by_number[marked]["status"] = "awaiting_packaging"
    else:
        remote.details_by_number[marked]["order_id"] += 1
        if change != "order_id_only":
            remote.details_by_number[marked]["order_number"] += "-different"
    run_ready(split_pipeline)
    current = state(split_pipeline, owner, view)
    assert current["status"] == "preparing" and current["readiness"]["status"] == "uncertain"
    assert not current["readiness"]["children"]
    with db.transaction() as tx:
        assigned = tx.scalar(select(MarkingAssignment).where(MarkingAssignment.released_at.is_(None)))
        assert assigned.posting_number == view["posting_number"]
        assert (
            tx.scalar(
                select(func.count()).select_from(ShipmentUnit).where(ShipmentUnit.released_at.is_(None))
            )
            == 3
        )
    assert len(remote.split_calls) == 1


def test_saved_manual_layout_and_its_lost_response_are_idempotent(split_pipeline, owner, db):
    assembly = split_pipeline[0]
    view = all_scans(split_pipeline, owner)
    groups = [[u["id"] for u in view["layout"]["units"]]]
    command_id = uuid4()
    first = call(assembly, owner, view, "layout", groups, command_id=command_id)
    repeated = call(assembly, owner, view, "layout", groups, command_id=command_id)
    assert first["ok"] and repeated["session"]["revision"] == first["session"]["revision"]
    assert first["session"]["scanned"] == view["scanned"]
    view = complete(split_pipeline, owner, first["session"])
    run_ready(split_pipeline)
    run_ready(split_pipeline)
    assert state(split_pipeline, owner, view)["status"] == "ready"
    assert len(split_pipeline[2].split_calls) == 1 and len(split_pipeline[2].split_calls[0]["packages"]) == 1


@pytest.mark.parametrize("bad", ["duplicate", "missing", "foreign", "empty"])
def test_layout_rejects_missing_duplicate_and_foreign_units(split_pipeline, owner, bad):
    from pack_v2.shared.errors import DomainError

    assembly = split_pipeline[0]
    view = open_window(split_pipeline, owner)
    groups = [[u["id"] for u in view["layout"]["units"]]]
    if bad == "duplicate":
        groups.append([groups[0][0]])
    elif bad == "missing":
        groups[0].pop()
    elif bad == "foreign":
        groups[0][0] = str(uuid4())
    else:
        groups.append([])
    with pytest.raises(DomainError):
        call(assembly, owner, view, "layout", groups)
    assert len(assembly.session(owner, UUID(view["id"]))["layout"]["groups"]) == 3


def test_layout_api_requires_csrf_and_own_session(split_pipeline, owner, settings):
    from fastapi.testclient import TestClient

    view = open_window(split_pipeline, owner)
    with TestClient(create_app(settings)) as client:
        client.app.state.assembly = split_pipeline[0]
        login(client)
        body = {
            "command_id": str(uuid4()),
            "lease_token": view["lease_token"],
            "revision": view["revision"],
            "groups": [[u["id"] for u in view["layout"]["units"]]],
        }
        assert client.post(f"/api/v2/assembly/{view['id']}/layout", json=body).status_code == 403


def test_thousand_unit_layout_fits_bounded_api_body(split_pipeline, owner, settings):
    from fastapi.testclient import TestClient

    assembly, _, remote, _ = split_pipeline
    original = deepcopy(remote.postings[0])
    next(p for p in original["products"] if p["sku"] == 6225436164)["quantity"] = 999
    remote.make_context(original)
    remote.postings = [original]
    view = open_window(split_pipeline, owner)
    units = [u["id"] for u in view["layout"]["units"]]
    assert len(units) == 1000
    with TestClient(create_app(settings)) as client:
        client.app.state.assembly = assembly
        login(client)
        csrf = client.get("/api/v2/me").json()["csrf"]
        body = {
            "command_id": str(uuid4()),
            "lease_token": view["lease_token"],
            "revision": view["revision"],
            "groups": [units[:500], units[500:]],
        }
        response = client.post(
            f"/api/v2/assembly/{view['id']}/layout", json=body, headers={"X-CSRF-Token": csrf}
        )
        assert response.status_code == 200, response.text[:500]
        assert len(response.json()["session"]["layout"]["groups"]) == 2


def test_foreign_user_and_stale_revision_cannot_change_layout(split_pipeline, owner, identity):
    from conftest import PASSWORD

    from pack_v2.shared.errors import NotFound

    assembly = split_pipeline[0]
    view = open_window(split_pipeline, owner)
    groups = [[u["id"] for u in view["layout"]["units"]]]
    other = identity.create_user(owner, "split-other", PASSWORD, "packer", frozenset({"pack"}))
    with pytest.raises(NotFound):
        call(assembly, other, view, "layout", groups)
    changed = call(assembly, owner, view, "layout", groups)
    assert changed["ok"]
    stale = call(assembly, owner, view, "layout", [[u["id"]] for u in view["layout"]["units"]])
    assert not stale["ok"] and stale["session"]["layout"]["groups"] == [sorted(group) for group in groups]


def test_background_sync_during_split_does_not_turn_expected_transition_into_scan_failure(
    split_pipeline, owner, db, identity
):
    from conftest import PASSWORD
    from test_orders import execute

    assembly, _, remote, account = split_pipeline
    remote.keep_root = True
    view = complete(split_pipeline, owner)
    run_ready(split_pipeline)
    remote.postings = [deepcopy(raw) for raw in remote.details_by_number.values()]
    execute(assembly.orders, owner, account, db)
    current = state(split_pipeline, owner, view)
    assert current["status"] == "preparing" and current["readiness"]["status"] == "checking"
    other = identity.create_user(owner, "child-packer", PASSWORD, "packer", frozenset({"pack"}))
    child = next(n for n in remote.split_children if n != view["posting_number"])
    result = assembly.open(other, uuid4(), code=child)
    assert not result["ok"] and result["code"] == "split_pending"
    run_ready(split_pipeline)
    assert state(split_pipeline, owner, view)["status"] == "ready"


def test_http_429_is_bounded_and_does_not_repeat_after_third_attempt(split_pipeline, owner, db):
    from datetime import UTC, datetime, timedelta

    from pack_v2.infrastructure.models import Job

    view = complete(split_pipeline, owner)
    split_pipeline[2].split_mode = "rate"
    for _ in range(4):
        run_ready(split_pipeline)
    assert len(split_pipeline[2].split_calls) == 3
    assert state(split_pipeline, owner, view)["status"] == "preparing"
    with db.transaction() as tx:
        row = tx.scalar(select(AssemblyDispatch))
        assert tx.get(Job, row.job_id).available_at >= datetime.now(UTC) + timedelta(seconds=8)


def test_lost_worker_lease_never_sends_split(split_pipeline, owner, db):
    from datetime import UTC, datetime, timedelta

    from pack_v2.infrastructure.jobs import Queue
    from pack_v2.infrastructure.models import Job

    assembly = split_pipeline[0]
    complete(split_pipeline, owner)
    job = Queue(db).claim(120)
    with db.transaction() as tx:
        tx.get(Job, job.id).lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assembly.readiness.perform(job)
    assert split_pipeline[2].split_calls == []


def test_cancel_before_split_keeps_marketplace_unchanged(split_pipeline, owner):
    view = complete(split_pipeline, owner)
    assert call(split_pipeline[0], owner, view, "cancel")["ok"]
    run_ready(split_pipeline)
    assert split_pipeline[2].split_calls == []


def test_cancel_after_split_keeps_reservations_and_reconciles(split_pipeline, owner, db):
    view = complete(split_pipeline, owner)
    run_ready(split_pipeline)
    current = state(split_pipeline, owner, view)
    assert call(split_pipeline[0], owner, current, "cancel")["ok"]
    run_ready(split_pipeline)
    with db.transaction() as tx:
        row = tx.scalar(select(AssemblyDispatch))
        assert row.status == "accepted" and row.split_result
        assert tx.get(AssemblySession, UUID(view["id"])).status == "cancelled"
    assert len(split_pipeline[2].split_calls) == 1


@pytest.mark.parametrize("collapse", [False, True])
def test_source_sync_preserves_split_or_revokes_changed_distribution(split_pipeline, owner, db, collapse):
    from test_orders import execute

    assembly, _, remote, account = split_pipeline
    view = complete(split_pipeline, owner)
    run_ready(split_pipeline)
    run_ready(split_pipeline)
    assert state(split_pipeline, owner, view)["status"] == "ready"
    if collapse:
        remote.details_by_number[view["posting_number"]]["related_postings"] = {"related_posting_numbers": []}
    remote.postings = [deepcopy(raw) for raw in remote.details_by_number.values()]
    execute(assembly.orders, owner, account, db)
    current = state(split_pipeline, owner, view)
    if collapse:
        assert current["status"] == "stale" and current["readiness"]["status"] == "stale"
        assert not current["readiness"]["children"]
        assert not assembly.queue(owner)
        with db.transaction() as tx:
            assert (
                tx.scalar(select(func.count()).select_from(Shipment).where(Shipment.layout_review.is_(True)))
                >= 3
            )
    else:
        assert current["status"] == "ready", current["readiness"]


def test_identical_marked_units_keep_identity_after_renumbering_and_reopen(split_pipeline, owner, db):
    from instance_remote import GTD, mark
    from test_instance_data import KONVERT, MUFT, finish_data

    assembly, data, remote, _ = split_pipeline
    original = deepcopy(remote.postings[0])
    next(p for p in original["products"] if p["sku"] == 634662543)["quantity"] = 2
    remote.make_context(original)
    remote.postings = [original]
    remote.reverse_slots = True
    view = open_window(split_pipeline, owner)
    for serial in ("ABCDEF1234567", "ABCDEF1234568"):
        view = call(assembly, owner, view, "scan", MUFT)["session"]
        view = call(assembly, owner, view, "gtd", UUID(view["last_unit_id"]), GTD)["session"]
        view = call(assembly, owner, view, "scan", mark(serial))["session"]
    view = finish_data(split_pipeline, owner, view)
    for _ in range(2):
        view = call(assembly, owner, view, "scan", KONVERT)["session"]
    units = view["layout"]["units"]
    by_item = defaultdict(list)
    for unit in units:
        by_item[unit["item_id"]].append(unit["id"])
    view = call(assembly, owner, view, "layout", list(by_item.values()))["session"]
    source_unit_ids = {u["id"] for u in units}
    view = complete(split_pipeline, owner, view)
    run_ready(split_pipeline)
    run_ready(split_pipeline)
    current = state(split_pipeline, owner, view)
    assert current["status"] == "ready", current["readiness"]
    with db.transaction() as tx:
        details = list(tx.scalars(select(UnitDetail)))
        assert {str(d.unit_id) for d in details} == source_unit_ids
        assert tx.scalar(select(func.count()).select_from(MarkingUnit)) == 2
        for detail in details:
            values = data.marking.values(tx, detail.shipment_id, [detail.unit_id])
            if not values:
                continue
            number = next(
                c["posting_number"]
                for c in current["readiness"]["children"]
                if c["shipment_id"] == str(detail.shipment_id)
            )
            slot = next(
                e
                for p in remote.contexts[number]["products"]
                for e in p["exemplars"]
                if str(e["exemplar_id"]) == detail.exemplar_id
            )
            assert data.marking.equivalent(values[detail.unit_id].raw, slot["marks"][0]["mark"])
        audit = json.dumps([r.details for r in tx.scalars(select(AuditEvent))])
        assert mark() not in audit and GTD not in audit
    marked_child = next(
        c
        for c in current["readiness"]["children"]
        if any(p["is_mandatory_mark_possible"] for p in remote.contexts[c["posting_number"]]["products"])
    )
    first_open = assembly.open(owner, uuid4(), code=marked_child["posting_number"])
    assert first_open["ok"] and first_open["session"]["status"] == "ready"
    assert call(assembly, owner, first_open["session"], "cancel")["ok"]
    reopened = open_window(split_pipeline, owner, code=marked_child["posting_number"])
    assert reopened["status"] == "scanning" and reopened["need"] == 2
    reopened = finish_data(split_pipeline, owner, reopened)
    assert all(i["mark_status"] == "accepted" for i in reopened["data"]["items"].values())


def test_gtd_obligation_survives_cleared_child_flag(split_pipeline, owner, monkeypatch):
    from instance_remote import GTD, mark
    from test_instance_data import KONVERT, MUFT, finish_data

    assembly, _, remote, _ = split_pipeline
    for product in remote.contexts[remote.postings[0]["posting_number"]]["products"]:
        product["is_gtd_needed"] = True
    view = open_window(split_pipeline, owner)
    for barcode in (MUFT, KONVERT, KONVERT):
        view = call(assembly, owner, view, "scan", barcode)["session"]
        view = call(assembly, owner, view, "gtd", UUID(view["last_unit_id"]), GTD)["session"]
        if barcode == MUFT:
            view = call(assembly, owner, view, "scan", mark())["session"]
    view = finish_data(split_pipeline, owner, view)
    view = complete(split_pipeline, owner, view)
    run_ready(split_pipeline)
    for number in remote.split_children:
        for product in remote.contexts[number]["products"]:
            product["is_gtd_needed"] = False
    original = remote.status_data

    def bad_only_for_unmarked(number):
        result = original(number)
        for product in result["products"]:
            if product["product_id"] == 6225436164:
                for unit in product["exemplars"]:
                    unit["gtd_check_status"], unit["gtd_error_codes"] = "error", ["GTD_INVALID"]
        return result

    monkeypatch.setattr(remote, "status_data", bad_only_for_unmarked)
    run_ready(split_pipeline)
    current = state(split_pipeline, owner, view)
    assert current["status"] == "preparing" and current["readiness"]["status"] == "uncertain"
    assert not current["readiness"]["children"]
