"""Legacy marking/GTD behavior through v2 public APIs and durable Ozon jobs."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from alembic import command
from conftest import login
from instance_remote import GTD, mark
from pydantic import ValidationError
from sqlalchemy import func, select
from test_assembly import call
from test_instance_data import (
    KONVERT,
    MUFT,
    finish_data,
    input_data,
    job,
    open_window,
    state,
    target,
)
from test_instance_data import pipeline as pipeline

from pack_v2.cli import migration_config
from pack_v2.integrations.ozon.instances import SET, VALIDATE
from pack_v2.modules.assembly.api import Gtd
from pack_v2.modules.instance_data import policy
from pack_v2.modules.instance_data.models import Submission, UnitDetail
from pack_v2.modules.marking.models import MarkingAssignment


def omit(pipeline, owner, view, *, command_id=None):
    return pipeline[0].gtd(
        owner,
        command_id or uuid4(),
        UUID(view["id"]),
        UUID(view["lease_token"]),
        view["revision"],
        UUID(target(view)),
        "",
        absent=True,
    )


def gtd_only(remote):
    remote.postings[0]["optional"] = {}
    remote.make_context(remote.postings[0], cis=False)


def gtd_unit(view):
    return next(u for i in view["data"]["items"].values() for u in i["units"] if u["gtd_required"])


def test_gtd_only_omission_skips_mark_validation_and_completes_after_real_confirmation(pipeline, owner, db):
    assembly, data, remote, _ = pipeline
    gtd_only(remote)
    view = open_window(pipeline, owner, code=MUFT)
    assert gtd_unit(view)["gtd_omitted"] is False
    assert not view["data"]["can_verify"]
    request = uuid4()
    saved = omit(pipeline, owner, view, command_id=request)
    assert saved["ok"] and gtd_unit(saved["session"])["gtd_omitted"]
    assert (
        omit(pipeline, owner, view, command_id=request)["session"]["revision"] == saved["session"]["revision"]
    )
    assert not saved["session"]["data"]["can_verify"]
    job(pipeline)
    assert not state(pipeline, owner, view)["data"]["can_verify"]
    accepted = finish_data(pipeline, owner, view)
    assert accepted["data"]["can_verify"] and gtd_unit(accepted)["gtd_status"] == "accepted"
    assert not any(path == VALIDATE for path, _, _ in remote.instance_calls)
    packet = next(p for path, p, _ in remote.instance_calls if path == SET)
    unit = packet["products"][0]["exemplars"][0]
    assert unit["is_gtd_absent"] is True and "gtd" not in unit
    with db.transaction() as tx:
        detail = tx.get(UnitDetail, UUID(target(view)))
        assert data._open(detail.encrypted_values)["gtd"] is None
        assert tx.scalar(select(func.count()).select_from(Submission)) == 1
    for _ in range(2):
        accepted = call(assembly, owner, accepted, "scan", KONVERT)["session"]
    scanned = call(assembly, owner, accepted, "verify")
    assert scanned["ok"] and scanned["session"]["status"] == "verified"


def test_omission_with_mark_preserves_full_code_and_still_validates(pipeline, owner):
    assembly, _, remote, _ = pipeline
    view = open_window(pipeline, owner, code=MUFT)
    view = omit(pipeline, owner, view)["session"]
    view = call(assembly, owner, view, "scan", mark())["session"]
    accepted = finish_data(pipeline, owner, view)
    assert accepted["data"]["can_verify"]
    assert next(i for i in accepted["data"]["items"].values() if i["cis"])["mark_status"] == "accepted"
    assert sum(path == VALIDATE for path, _, _ in remote.instance_calls) == 1
    packet = next(p for path, p, _ in remote.instance_calls if path == SET)
    assert packet["products"][0]["exemplars"][0]["marks"][0]["mark"] == mark()


@pytest.mark.parametrize("mode", ["missing_flag", "old_number", "error", "processing", "FAILED", "unknown"])
def test_no_http_200_shortcut_for_gtd_absence(pipeline, owner, monkeypatch, mode):
    _, _, remote, _ = pipeline
    gtd_only(remote)
    view = open_window(pipeline, owner, code=MUFT)
    view = omit(pipeline, owner, view)["session"]
    job(pipeline)
    original = remote.status_data

    def changed(number):
        response = original(number)
        unit = response["products"][0]["exemplars"][0]
        if mode == "missing_flag":
            unit.pop("is_gtd_absent")
        elif mode == "old_number":
            unit["gtd"] = GTD
        else:
            unit["gtd_check_status"] = mode
        return response

    monkeypatch.setattr(remote, "status_data", changed)
    job(pipeline, due=True)
    current = state(pipeline, owner, view)
    assert not current["data"]["can_verify"]
    assert gtd_unit(current)["gtd_status"] != "accepted"
    assert sum(path == SET for path, _, _ in remote.instance_calls) == 1


def test_explicit_absence_needs_flag_but_not_a_number_check(pipeline, owner, monkeypatch):
    _, _, remote, _ = pipeline
    gtd_only(remote)
    view = omit(pipeline, owner, open_window(pipeline, owner, code=MUFT))["session"]
    original = remote.status_data

    def absent_without_status(number):
        response = original(number)
        unit = response["products"][0]["exemplars"][0]
        unit.pop("gtd_check_status")
        unit["gtd"] = ""
        return response

    monkeypatch.setattr(remote, "status_data", absent_without_status)
    assert finish_data(pipeline, owner, view)["data"]["can_verify"]


def test_unchanged_gtd_choice_is_noop_and_number_replaces_omission(pipeline, owner, db):
    assembly, _, remote, _ = pipeline
    gtd_only(remote)
    view = omit(pipeline, owner, open_window(pipeline, owner, code=MUFT))["session"]
    view = finish_data(pipeline, owner, view)
    with db.transaction() as tx:
        revision = tx.get(UnitDetail, UUID(target(view))).revision
    view = omit(pipeline, owner, view)["session"]
    with db.transaction() as tx:
        assert tx.get(UnitDetail, UUID(target(view))).revision == revision
        assert tx.scalar(select(func.count()).select_from(Submission)) == 1
    changed = call(assembly, owner, view, "gtd", UUID(target(view)), GTD)
    assert changed["ok"]
    view = finish_data(pipeline, owner, changed["session"])
    assert view["data"]["can_verify"] and not gtd_unit(view)["gtd_omitted"]
    packet = [p for path, p, _ in remote.instance_calls if path == SET][-1]
    assert packet["products"][0]["exemplars"][0]["gtd"] == GTD
    assert packet["products"][0]["exemplars"][0]["is_gtd_absent"] is False


@pytest.mark.parametrize("choice", [{}, {"absent": "true"}, {"absent": 1}, {"absent": True, "value": GTD}])
def test_gtd_api_choice_is_explicit_and_strict(choice):
    with pytest.raises(ValidationError):
        Gtd(command_id=uuid4(), lease_token=uuid4(), revision=1, unit_id=uuid4(), **choice)


def test_api_omission_has_csrf_revision_and_idempotency(pipeline, owner, client):
    assembly, _, remote, _ = pipeline
    gtd_only(remote)
    client.app.state.assembly = assembly
    login(client)
    csrf = client.get("/api/v2/me").json()["csrf"]
    view = open_window(pipeline, owner, code=MUFT)
    payload = {
        "command_id": str(uuid4()),
        "lease_token": view["lease_token"],
        "revision": view["revision"],
        "unit_id": target(view),
        "absent": True,
    }
    url = f"/api/v2/assembly/{view['id']}/gtd"
    assert client.post(url, json=payload).status_code == 403
    saved = client.post(url, json=payload, headers={"x-csrf-token": csrf})
    assert saved.status_code == 200 and gtd_unit(saved.json()["session"])["gtd_omitted"]
    replayed = client.post(url, json=payload, headers={"x-csrf-token": csrf})
    assert (
        replayed.status_code == 200
        and replayed.json()["session"]["revision"] == saved.json()["session"]["revision"]
    )


def test_same_rejected_mark_can_be_explicitly_rescanned(pipeline, owner, db):
    assembly, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    remote.instance_mode = "bad_mark"
    job(pipeline)
    rejected = state(pipeline, owner, view)
    remote.instance_mode = "ok"
    rescan = call(assembly, owner, rejected, "scan", mark())
    assert rescan["ok"]
    assert finish_data(pipeline, owner, rescan["session"])["data"]["can_verify"]
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(MarkingAssignment)) == 1
        assert tx.scalar(select(func.count()).select_from(Submission)) == 2


def test_retired_code_stays_reserved_until_replacement_is_confirmed(pipeline, owner, db):
    assembly, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    remote.instance_mode = "bad_mark"
    job(pipeline)
    rejected = state(pipeline, owner, view)
    remote.instance_mode = "ok"
    replacement = call(assembly, owner, rejected, "scan", mark(serial="REPLACEMENT01"))
    with db.transaction() as tx:
        assignments = list(tx.scalars(select(MarkingAssignment).order_by(MarkingAssignment.created_at)))
        assert len(assignments) == 2
        assert assignments[0].retired_at and not assignments[0].released_at
        assert assignments[1].retired_at is None
    assert finish_data(pipeline, owner, replacement["session"])["data"]["can_verify"]
    with db.transaction() as tx:
        old = tx.scalar(select(MarkingAssignment).where(MarkingAssignment.retired_at.is_not(None)))
        assert old.released_at and old.release_reason == "replacement_confirmed"


def test_native_deletion_after_cancel_does_not_resurrect_old_mark_and_dropped_flags(pipeline, owner, db):
    assembly, _, remote, _ = pipeline
    view = finish_data(pipeline, owner, input_data(pipeline, owner))
    assert view["data"]["can_verify"]
    call(assembly, owner, view, "cancel")
    number = view["posting_number"]
    native = remote.contexts[number]["products"][0]
    native["exemplars"][0]["marks"] = []
    native["is_mandatory_mark_possible"] = False
    native["is_gtd_needed"] = False
    count = sum(path == SET for path, _, _ in remote.instance_calls)
    view = open_window(pipeline, owner, code=MUFT)
    unit = gtd_unit(view)
    assert unit["cis_status"] == "empty" and not view["data"]["can_verify"]
    with db.transaction() as tx:
        old = tx.scalar(select(MarkingAssignment))
        assert old.retired_at and not old.released_at
    assert sum(path == SET for path, _, _ in remote.instance_calls) == count
    new = call(assembly, owner, view, "scan", mark(serial="NEWNATIVE0001"))
    assert new["ok"], new
    assert finish_data(pipeline, owner, new["session"])["data"]["can_verify"]
    with db.transaction() as tx:
        old = tx.scalar(select(MarkingAssignment).where(MarkingAssignment.retired_at.is_not(None)))
        assert old.released_at


def test_applied_absence_requires_exact_flags_and_no_duplicate_instances():
    unit = {"exemplar_id": 1, "is_gtd_absent": True, "is_rnpt_absent": True, "marks": []}
    payload = {"products": [{"product_id": 2, "exemplars": [unit]}]}
    applied = deepcopy(unit)
    applied.pop("marks")
    posting = {
        "status": "awaiting_deliver",
        "product_exemplars": {"products": [{"sku": 2, "exemplars": [applied]}]},
    }
    assert policy.applied(posting, payload)
    applied["is_gtd_absent"] = False
    assert not policy.applied(posting, payload)
    applied["is_gtd_absent"] = True
    posting["product_exemplars"]["products"][0]["exemplars"].append(deepcopy(applied))
    assert not policy.applied(posting, payload)


def test_downgrade_does_not_reactivate_retired_assignments(pipeline, owner, db, settings):
    assembly, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    remote.instance_mode = "bad_mark"
    job(pipeline)
    rejected = state(pipeline, owner, view)
    assert call(assembly, owner, rejected, "scan", mark(serial="REPLACEMENT01"))["ok"]
    with pytest.raises(RuntimeError, match="replacements await confirmation"):
        command.downgrade(migration_config(settings), "0008_split")
    with db.transaction() as tx:
        assignments = list(tx.scalars(select(MarkingAssignment)))
        assert sum(a.released_at is None for a in assignments) == 2
        assert sum(a.retired_at is not None for a in assignments) == 1


def test_gtd_only_packet_with_existing_native_mark_still_validates(pipeline, owner):
    _, _, remote, _ = pipeline
    gtd_only(remote)
    product = remote.contexts[remote.postings[0]["posting_number"]]["products"][0]
    product["is_jw_uin_needed"] = True
    product["exemplars"][0]["marks"] = [{"mark_type": "jw_uin", "mark": "synthetic-native-jewelry-code"}]
    view = open_window(pipeline, owner, code=MUFT)
    assert view["status"] == "scanning"
    view = omit(pipeline, owner, view)["session"]
    assert finish_data(pipeline, owner, view)["data"]["can_verify"]
    assert any(path == VALIDATE for path, _, _ in remote.instance_calls)
    sent = next(p for path, p, _ in remote.instance_calls if path == SET)
    assert sent["products"][0]["exemplars"][0]["marks"] == product["exemplars"][0]["marks"]


def test_missing_uin_does_not_request_jewelry_data_for_ordinary_products(pipeline, owner):
    _, _, remote, _ = pipeline
    product = remote.contexts[remote.postings[0]["posting_number"]]["products"][0]
    product["is_jw_uin_needed"] = True
    view = open_window(pipeline, owner, code=MUFT)
    assert view["status"] == "scanning"
    assert finish_data(pipeline, owner, input_data(pipeline, owner, view=view))["data"]["can_verify"]


def test_same_gtd_can_retry_rejected_result_without_rescanning_accepted_mark(pipeline, owner):
    assembly, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    remote.instance_mode = "bad_gtd"
    view = finish_data(pipeline, owner, view)
    assert view["data"]["status"] == "rejected"
    assert gtd_unit(view)["cis_status"] == "accepted"
    remote.instance_mode = "ok"
    retry = call(assembly, owner, view, "gtd", UUID(target(view)), GTD)
    assert retry["ok"]
    assert finish_data(pipeline, owner, retry["session"])["data"]["can_verify"]


def test_native_replacement_keeps_only_confirmed_code_after_reopen(pipeline, owner, db):
    assembly, data, remote, _ = pipeline
    view = finish_data(pipeline, owner, input_data(pipeline, owner))
    call(assembly, owner, view, "cancel")
    replacement = mark(serial="EXTERNAL00001")
    native = remote.contexts[view["posting_number"]]["products"][0]["exemplars"][0]
    native["marks"] = [{"mark_type": "mandatory_mark", "mark": replacement}]
    view = open_window(pipeline, owner, code=MUFT)
    view = finish_data(pipeline, owner, view)
    assert view["data"]["can_verify"]
    with db.transaction() as tx:
        active = data.marking.values(tx, UUID(view["shipment_id"]), [UUID(target(view))])
        assert next(iter(active.values())).raw == replacement
        old = tx.scalar(select(MarkingAssignment).where(MarkingAssignment.retired_at.is_not(None)))
        assert old.released_at


def test_pending_replacement_does_not_free_old_global_reservation(pipeline, owner, db):
    from pack_v2.modules.marking.public import UnitBinding
    from pack_v2.modules.orders.models import OrderUnit, ShipmentUnit
    from pack_v2.shared.errors import Conflict

    assembly, data, remote, _ = pipeline
    view = input_data(pipeline, owner)
    remote.instance_mode = "bad_mark"
    job(pipeline)
    rejected = state(pipeline, owner, view)
    new = call(assembly, owner, rejected, "scan", mark(serial="REPLACEMENT01"))
    remote.instance_mode = "processing"
    job(pipeline)
    job(pipeline, due=True)
    assert not state(pipeline, owner, new["session"])["data"]["can_verify"]
    with db.transaction() as tx:
        old = tx.scalar(select(MarkingAssignment).where(MarkingAssignment.retired_at.is_not(None)))
        assert old.released_at is None
        other = tx.scalar(
            select(ShipmentUnit).where(
                ShipmentUnit.shipment_id == old.shipment_id, ShipmentUnit.unit_id != old.unit_id
            )
        )
        other_unit = tx.get(OrderUnit, other.unit_id)
        binding = UnitBinding(
            old.account_id, other_unit.order_id, old.shipment_id, other.unit_id, other.id, old.posting_number
        )
        with pytest.raises(Conflict, match="Данный Честный знак уже используется в заказе:"):
            data.marking.capture(tx, owner.id, binding, mark())
    remote.instance_mode = "ok"
    assert finish_data(pipeline, owner, new["session"])["data"]["can_verify"]


def test_reactivated_mark_does_not_inherit_old_successful_check(pipeline, owner, db):
    _, data, _, _ = pipeline
    finish_data(pipeline, owner, input_data(pipeline, owner))
    with db.transaction() as tx:
        current = tx.scalar(select(MarkingAssignment))
        batches = list(tx.scalars(select(Submission.id)))
        assert data.marking.proven(tx, batches, [current.id]) == {current.id}
        data.marking.retire(tx, current.id, owner.id, "native_removed")
        from pack_v2.modules.marking.public import UnitBinding

        binding = UnitBinding(
            current.account_id,
            current.order_id,
            current.shipment_id,
            current.unit_id,
            current.allocation_id,
            current.posting_number,
        )
        assignment, changed = data.marking.capture(tx, owner.id, binding, mark())
        assert changed and assignment == current.id
        assert not data.marking.proven(tx, batches, [current.id])


@pytest.mark.parametrize("applied", [True, False])
def test_ready_posting_omission_requires_update_and_actual_application(
    pipeline, owner, db, monkeypatch, applied
):
    from test_orders import execute

    from pack_v2.integrations.ozon.instances import UPDATE

    assembly, _, remote, account = pipeline
    remote.postings[0].update(status="awaiting_deliver", substatus="posting_formed")
    gtd_only(remote)
    execute(assembly.orders, owner, account, db)
    if not applied:
        original = remote.applied

        def missing_declaration(number):
            data = original(number)
            unit = data["product_exemplars"]["products"][0]["exemplars"][0]
            unit["is_gtd_absent"] = False
            return data

        monkeypatch.setattr(remote, "applied", missing_declaration)
    view = omit(pipeline, owner, open_window(pipeline, owner, code=MUFT))["session"]
    view = finish_data(pipeline, owner, view)
    assert view["data"]["can_verify"] is applied
    assert (gtd_unit(view)["gtd_status"] == "accepted") is applied
    assert sum(path == SET for path, _, _ in remote.instance_calls) == 1
    assert sum(path == UPDATE for path, _, _ in remote.instance_calls) == 1
    assert not any(path == VALIDATE for path, _, _ in remote.instance_calls)
    if not applied:
        job(pipeline, due=True)
        assert not state(pipeline, owner, view)["data"]["can_verify"]
        assert sum(path == UPDATE for path, _, _ in remote.instance_calls) == 1
