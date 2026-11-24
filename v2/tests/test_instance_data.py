import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import PASSWORD, login
from instance_remote import GTD, GTIN, InstanceRemote, mark
from pydantic import SecretStr
from sqlalchemy import func, select
from test_assembly import call
from test_orders import execute

from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.jobs import Queue
from pack_v2.infrastructure.models import AuditEvent, Job
from pack_v2.integrations.ozon.access import OzonAccessChecker
from pack_v2.integrations.ozon.instances import GET, POSTING, SET, UPDATE, VALIDATE
from pack_v2.integrations.public import Connections
from pack_v2.modules.assembly.models import AssemblyCommand, AssemblySession
from pack_v2.modules.assembly.public import Assembly
from pack_v2.modules.catalog.public import Catalog
from pack_v2.modules.instance_data.models import InstanceContext, Submission, UnitDetail
from pack_v2.modules.instance_data.public import InstanceData
from pack_v2.modules.marking.models import MarkingAssignment, MarkingTransmission, MarkingUnit
from pack_v2.modules.marking.public import Marking
from pack_v2.modules.orders.models import OrderUnit, Shipment
from pack_v2.modules.orders.public import Orders

MUFT = "OZN634662543"
KONVERT = "OZN6225436164"


@pytest.fixture
def pipeline(db, settings, owner):
    remote = InstanceRemote(db)
    box = SecretBox(settings.encryption_key.get_secret_value())
    connections = Connections(db, box, OzonAccessChecker(), remote.factory, remote.factory_instances)
    account = connections.create(owner, "ANEX", "000123", SecretStr("synthetic-instance-key"))
    catalog = Catalog(db, connections)
    orders = Orders(db, connections, catalog)
    marking = Marking(box, settings.secret_key.get_secret_value())
    data = InstanceData(db, connections, orders, marking, box)
    assembly = Assembly(
        db, connections, orders, box, settings.secret_key.get_secret_value(), instance_data=data
    )
    execute(catalog, owner, account, db)
    execute(orders, owner, account, db)
    return assembly, data, remote, account


def job(pipeline, *, due=False):
    assembly, data, _, _ = pipeline
    if due:
        with assembly.db.transaction() as tx:
            for row in tx.scalars(select(Job).where(Job.status == "pending")):
                row.available_at = datetime.now(UTC) - timedelta(seconds=1)
    request = Queue(assembly.db).claim(lease_seconds=120)
    assert request
    (assembly.orders if request.kind == "orders.sync" else data).perform(request)
    return request


def open_window(pipeline, owner, code="ii50082908916"):
    assembly, _, _, _ = pipeline
    result = assembly.open(owner, uuid4(), code=code)
    assert result["ok"], result
    job(pipeline)
    checking = call(assembly, owner, result["session"], "activate")
    assert checking.get("pending"), checking
    job(pipeline)
    result = call(assembly, owner, checking["session"], "activate")
    assert result["ok"], result
    return result["session"]


def target(view):
    return next(
        unit["id"]
        for info in view["data"]["items"].values()
        for unit in info["units"]
        if unit["gtd_required"]
    )


def input_data(pipeline, owner, *, code=None, gtd=GTD, view=None):
    assembly, _, _, _ = pipeline
    view = view or open_window(pipeline, owner)
    if view["scanned"] == 0:
        result = call(assembly, owner, view, "scan", MUFT)
        assert result["ok"], result
        view = result["session"]
    result = call(assembly, owner, view, "gtd", UUID(target(view)), gtd)
    assert result["ok"], result
    result = call(assembly, owner, result["session"], "scan", code or mark())
    assert result["ok"], result
    return result["session"]


def state(pipeline, owner, view):
    return pipeline[0].session(owner, UUID(view["id"]))


def finish_data(pipeline, owner, view):
    job(pipeline, due=True)
    current = state(pipeline, owner, view)
    if current["data"]["status"] == "checking":
        job(pipeline, due=True)
    return state(pipeline, owner, view)


def test_open_requires_real_discovery_before_scans_and_flags_are_separate(pipeline, owner, db):
    assembly, data, remote, _ = pipeline
    initial = assembly.open(owner, uuid4(), code=MUFT)["session"]
    assert initial["scanned"] == 0 and not remote.instance_calls
    job(pipeline)
    waiting = call(assembly, owner, initial, "activate")
    assert waiting["pending"] and waiting["session"]["scanned"] == 0
    assert remote.instance_calls == []
    job(pipeline)
    ready = call(assembly, owner, waiting["session"], "activate")["session"]
    assert ready["scanned"] == 1 and ready["data"]["pending_unit_id"]
    assert [path for path, _, _ in remote.instance_calls] == [GET, POSTING]
    assert not ready["data"]["can_verify"]
    with db.transaction() as tx:
        row = tx.scalar(select(InstanceContext))
        assert row.requirements["634662543"]["cis_market"] == "optional"
        assert row.requirements["634662543"]["cis"] and row.requirements["634662543"]["gtd"]
        assert row.policy_version == 2 and row.checked_at
        assert b"mandatory_mark" not in row.encrypted_snapshot


def test_product_then_mark_binding_gtd_and_full_async_acceptance(pipeline, owner, db):
    assembly, data, remote, _ = pipeline
    view = input_data(pipeline, owner)
    assert view["data"]["status"] == "prepared" and view["scanned"] == 1
    assert all(i["mark_status"] != "accepted" for i in view["data"]["items"].values())
    assert not any(path == SET for path, _, _ in remote.instance_calls)
    job(pipeline)
    checking = state(pipeline, owner, view)
    assert checking["data"]["status"] == "checking" and not checking["data"]["can_verify"]
    with db.transaction() as tx:
        batch = tx.scalar(select(Submission))
        assert batch.set_started_at and batch.status == "checking"
        assert tx.scalar(select(MarkingTransmission.status)) == "pending"
    job(pipeline, due=True)
    accepted = state(pipeline, owner, view)
    assert accepted["data"]["can_verify"]
    assert next(i for i in accepted["data"]["items"].values() if i["cis"])["mark_status"] == "accepted"
    sent = next(payload for path, payload, _ in remote.instance_calls if path == SET)
    assert len(sent["products"]) == 2 and sum(len(p["exemplars"]) for p in sent["products"]) == 3
    marked = next(p for p in sent["products"] if p["product_id"] == 634662543)["exemplars"][0]
    assert marked["gtd"] == GTD and marked["is_gtd_absent"] is False
    assert marked["marks"] == [{"mark_type": "mandatory_mark", "mark": mark()}]
    assert next(wire for path, _, wire in remote.instance_calls if path == SET).count(b"\\u001d") == 2
    for _ in range(2):
        accepted = call(assembly, owner, accepted, "scan", KONVERT)["session"]
    result = call(assembly, owner, accepted, "verify")
    assert result["ok"] and result["session"]["status"] == "verified"
    with db.transaction() as tx:
        assert tx.get(Shipment, UUID(view["shipment_id"])).state == "awaiting_assembly"
        assert tx.scalar(select(MarkingTransmission.status)) == "accepted"
    assert not any("/ship" in path for path, _, _ in remote.instance_calls)


def test_gtd_and_mark_remain_separate_and_incomplete_data_never_transmits(pipeline, owner):
    assembly, _, remote, _ = pipeline
    view = open_window(pipeline, owner)
    view = call(assembly, owner, view, "scan", MUFT)["session"]
    view = call(assembly, owner, view, "scan", mark())["session"]
    assert view["data"]["status"] == "needs_data" and not view["data"]["can_verify"]
    assert not any(path in (SET, VALIDATE) for path, _, _ in remote.instance_calls)
    for _ in range(2):
        view = call(assembly, owner, view, "scan", KONVERT)["session"]
    assert call(assembly, owner, view, "verify")["code"] == "instance_data_required"
    result = call(assembly, owner, view, "gtd", UUID(target(view)), GTD)
    assert result["ok"] and result["session"]["data"]["status"] == "prepared"


def test_next_product_requires_mark_of_specific_last_unit(pipeline, owner):
    assembly, _, _, _ = pipeline
    view = open_window(pipeline, owner)
    view = call(assembly, owner, view, "scan", MUFT)["session"]
    blocked = call(assembly, owner, view, "scan", KONVERT)
    assert blocked["code"] == "mark_required" and blocked["session"]["scanned"] == 1
    not_product = call(assembly, owner, view, "scan", mark())
    assert not_product["ok"] and not_product["session"]["scanned"] == 1


def test_repeated_http_mark_command_is_idempotent_and_no_code_leaks(pipeline, owner, db):
    assembly, data, _, _ = pipeline
    view = open_window(pipeline, owner, code=MUFT)
    command_id = uuid4()
    first = call(assembly, owner, view, "scan", mark(), command_id=command_id)
    repeated = call(assembly, owner, view, "scan", mark(), command_id=command_id)
    assert first["ok"] and repeated["ok"]
    assert mark() not in json.dumps(first)
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(MarkingAssignment)) == 1
        assert tx.scalar(select(func.count()).select_from(MarkingUnit)) == 1
        stored = tx.scalar(select(MarkingUnit))
        assert data.box.open(stored.encrypted_code).decode() == mark()
        assert mark().encode() not in stored.encrypted_code
        assert mark() not in repr([c.outcome for c in tx.scalars(select(AssemblyCommand))])
        assert mark() not in repr([a.details for a in tx.scalars(select(AuditEvent))])


@pytest.mark.parametrize("status", ["awaiting_packaging", "awaiting_deliver"])
def test_both_working_states_fetch_flags_and_already_ready_uses_update(pipeline, owner, db, status):
    assembly, _, remote, account = pipeline
    raw = remote.postings[0]
    raw.update(status=status, substatus="posting_formed")
    remote.make_context(raw)
    execute(assembly.orders, owner, account, db)
    view = input_data(pipeline, owner)
    view = finish_data(pipeline, owner, view)
    assert view["data"]["status"] == "accepted"
    assert sum(path == UPDATE for path, _, _ in remote.instance_calls) == (
        1 if status == "awaiting_deliver" else 0
    )
    assert not any("/ship" in path for path, _, _ in remote.instance_calls)


@pytest.mark.parametrize(
    "failure", ["set_timeout", "set_no_effect_timeout", "update_timeout", "update_no_effect_timeout"]
)
def test_ambiguous_write_is_never_blindly_repeated(pipeline, owner, db, failure):
    assembly, _, remote, account = pipeline
    if failure.startswith("update"):
        remote.postings[0].update(status="awaiting_deliver", substatus="posting_formed")
        remote.make_context(remote.postings[0])
        execute(assembly.orders, owner, account, db)
    view = input_data(pipeline, owner)
    remote.instance_mode = failure
    job(pipeline)
    for _ in range(3):
        current = state(pipeline, owner, view)
        if current["data"]["status"] in ("accepted", "uncertain"):
            break
        job(pipeline, due=True)
    accepted = failure in ("set_timeout", "update_timeout")
    assert (state(pipeline, owner, view)["data"]["status"] == "accepted") is accepted
    assert sum(path == SET for path, _, _ in remote.instance_calls) == 1
    assert sum(path == UPDATE for path, _, _ in remote.instance_calls) <= (
        1 if failure.startswith("update") else 0
    )
    assert not state(pipeline, owner, view)["data"]["can_verify"] if not accepted else True


def test_validate_rejection_turns_m_red_without_set_and_allows_new_code(pipeline, owner):
    assembly, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    remote.instance_mode = "bad_mark"
    job(pipeline)
    rejected = state(pipeline, owner, view)
    assert rejected["data"]["status"] == "rejected"
    assert next(i for i in rejected["data"]["items"].values() if i["cis"])["mark_status"] == "rejected"
    assert not any(path == SET for path, _, _ in remote.instance_calls)
    remote.instance_mode = "ok"
    replacement = call(assembly, owner, rejected, "scan", mark(serial="OTHER00123456"))
    assert replacement["ok"], replacement
    accepted = finish_data(pipeline, owner, replacement["session"])
    assert accepted["data"]["status"] == "accepted"


def test_gtd_rejection_is_separate_and_full_corrected_packet_is_sent(pipeline, owner):
    assembly, _, remote, _ = pipeline
    view = input_data(pipeline, owner, gtd="bad-gtd")
    remote.instance_mode = "bad_gtd"
    job(pipeline)
    job(pipeline, due=True)
    current = state(pipeline, owner, view)
    assert current["data"]["status"] == "rejected"
    unit = next(u for i in current["data"]["items"].values() for u in i["units"] if u["gtd_required"])
    assert unit["gtd_status"] == "rejected"
    remote.instance_mode = "ok"
    correction = call(assembly, owner, current, "gtd", UUID(unit["id"]), GTD)
    assert correction["ok"]
    accepted = finish_data(pipeline, owner, correction["session"])
    assert accepted["data"]["status"] == "accepted"


def test_wrong_full_crypto_echo_never_confirms_previous_or_similar_code(pipeline, owner):
    _, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    job(pipeline)
    remote.instance_mode = "wrong_echo"
    job(pipeline, due=True)
    assert not state(pipeline, owner, view)["data"]["can_verify"]


def test_invalid_local_code_and_gtin_mismatch_show_red_without_binding(pipeline, owner, db):
    assembly, _, _, _ = pipeline
    view = open_window(pipeline, owner, code=MUFT)
    invalid = call(assembly, owner, view, "scan", mark().replace(GTIN, GTIN[:-1] + "0"))
    assert not invalid["ok"]
    assert (
        next(i for i in invalid["session"]["data"]["items"].values() if i["cis"])["mark_status"] == "rejected"
    )
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(MarkingAssignment)) == 0
    valid = call(assembly, owner, invalid["session"], "scan", mark())
    assert valid["ok"]


def test_mark_is_reserved_across_accounts_and_cancel_does_not_release_it(pipeline, owner, db):
    assembly, data, remote, _ = pipeline
    view = input_data(pipeline, owner)
    call(assembly, owner, view, "cancel")
    # Finish the persisted submission independently of the cancelled scanner session.
    finish_data(pipeline, owner, view)
    remote.make_context(remote.postings[0])
    remote.saved.clear()
    second = assembly.connections.create(owner, "Second", "987654", SecretStr("second-key"))
    execute(assembly.orders.catalog, owner, second, db)
    execute(assembly.orders, owner, second, db)
    result = assembly.open(owner, uuid4(), code=MUFT, account_id=second)
    job(pipeline)
    result = call(assembly, owner, result["session"], "activate")
    job(pipeline)
    second_view = call(assembly, owner, result["session"], "activate")["session"]
    conflict = call(assembly, owner, second_view, "scan", mark())
    assert conflict["message"] == "Данный Честный знак уже используется в заказе: " + view["posting_number"]
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(MarkingAssignment)) == 1
        assert tx.scalar(select(MarkingAssignment.released_at)) is None


@pytest.mark.parametrize("fault", ["denied", "missing_flag", "wrong_quantity", "other_required"])
def test_discovery_failure_does_not_mean_no_data_required(pipeline, owner, db, fault):
    assembly, _, remote, _ = pipeline
    context = remote.contexts[remote.postings[0]["posting_number"]]
    if fault == "denied":
        remote.instance_mode = "denied"
    elif fault == "missing_flag":
        # FBS hints already prove CIS/GTD for the first product; an unknown second product still blocks.
        context["products"][1].pop("is_gtd_needed")
    elif fault == "wrong_quantity":
        context["products"][0]["quantity"] += 1
    else:
        context["products"][0]["is_rnpt_needed"] = True
    opened = assembly.open(owner, uuid4(), code=MUFT)["session"]
    job(pipeline)
    waiting = call(assembly, owner, opened, "activate")["session"]
    job(pipeline)
    final = call(assembly, owner, waiting, "activate")
    assert not final["ok"] and final["session"]["scanned"] == 0
    assert not any(path == SET for path, _, _ in remote.instance_calls)
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(OrderUnit)) == 0


def test_two_identical_units_get_individual_codes_and_exemplar_ids(pipeline, owner, db):
    assembly, _, remote, account = pipeline
    raw = remote.postings[0]
    raw["products"][0]["quantity"] = 2
    remote.make_context(raw)
    execute(assembly.orders, owner, account, db)
    view = open_window(pipeline, owner)
    physical = []
    for index in range(2):
        view = call(assembly, owner, view, "scan", MUFT)["session"]
        uid = view["last_unit_id"]
        physical.append(uid)
        view = call(assembly, owner, view, "gtd", UUID(uid), GTD)["session"]
        if index:
            duplicate = call(assembly, owner, view, "scan", mark())
            assert not duplicate["ok"] and duplicate["message"].startswith(
                "Данный Честный знак уже используется"
            )
            view = duplicate["session"]
        result = call(
            assembly, owner, view, "scan", mark(serial="ABCDEF1234567" if index == 0 else "SECOND1234567")
        )
        assert result["ok"], result
        view = result["session"]
    accepted = finish_data(pipeline, owner, view)
    assert accepted["data"]["status"] == "accepted" and len(set(physical)) == 2
    with db.transaction() as tx:
        assert (
            len(set(tx.scalars(select(UnitDetail.exemplar_id).where(UnitDetail.product_id == "634662543"))))
            == 2
        )
        assert tx.scalar(select(func.count()).select_from(MarkingAssignment)) == 2


def test_full_payload_preserves_unedited_rnpt_weight_and_other_markings(pipeline, owner):
    _, _, remote, _ = pipeline
    row = remote.contexts[remote.postings[0]["posting_number"]]["products"][1]["exemplars"][0]
    row.update(
        rnpt="1234567890",
        is_rnpt_absent=False,
        weight=1500.25,
        marks=[{"mark_type": "imei", "mark": "123456789012345"}],
    )
    input_data(pipeline, owner)
    job(pipeline)
    sent = next(data for path, data, _ in remote.instance_calls if path == SET)
    unit = next(p for p in sent["products"] if p["product_id"] == 6225436164)["exemplars"][0]
    assert unit["rnpt"] == "1234567890" and unit["weight"] == 1500.25
    assert unit["marks"] == [{"mark_type": "imei", "mark": "123456789012345"}]


def test_changed_remote_fields_are_not_silently_overwritten(pipeline, owner):
    _, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    remote.contexts[remote.postings[0]["posting_number"]]["products"][1]["exemplars"][0]["weight"] = 777
    job(pipeline)
    assert state(pipeline, owner, view)["data"]["status"] == "stale"
    assert not any(path == SET for path, _, _ in remote.instance_calls)


def test_late_lease_cannot_accept_data_or_send_mutation(pipeline, owner, db):
    assembly, data, remote, _ = pipeline
    view = input_data(pipeline, owner)

    def lose_lease(_request, _payload):
        with db.transaction() as tx:
            pending = tx.scalar(select(Job).where(Job.kind == "instances.submit", Job.status == "running"))
            pending.lease_token = uuid4()

    remote.before_instance = lose_lease
    job(pipeline)
    assert not state(pipeline, owner, view)["data"]["can_verify"]
    assert not any(path == SET for path, _, _ in remote.instance_calls)


def test_api_enforces_pack_owner_csrf_and_never_echoes_full_code(pipeline, owner, identity, client):
    assembly, data, _, _ = pipeline
    client.app.state.assembly = assembly
    client.app.state.connections = assembly.connections
    client.app.state.instance_data = data
    login(client)
    csrf = client.get("/api/v2/me").json()["csrf"]
    view = open_window(pipeline, owner, code=MUFT)
    payload = {
        "command_id": str(uuid4()),
        "lease_token": view["lease_token"],
        "revision": view["revision"],
        "unit_id": target(view),
        "value": GTD,
    }
    assert client.post(f"/api/v2/assembly/{view['id']}/gtd", json=payload).status_code == 403
    assert (
        client.post(
            f"/api/v2/assembly/{view['id']}/gtd", json=payload, headers={"x-csrf-token": csrf}
        ).status_code
        == 200
    )
    raw = {"command_id": str(uuid4()), "lease_token": view["lease_token"], "revision": True, "code": mark()}
    response = client.post(f"/api/v2/assembly/{view['id']}/scan", json=raw, headers={"x-csrf-token": csrf})
    assert response.status_code == 422 and mark() not in response.text
    identity.create_user(owner, "other", PASSWORD, "packer", frozenset({"pack"}))
    client.post("/logout", data={"csrf": csrf})
    login(client, "other")
    assert client.get("/api/v2/assembly/" + view["id"]).status_code == 404


def test_unmarked_order_still_requires_discovery_but_does_not_write(pipeline, owner, db):
    assembly, _, remote, account = pipeline
    raw = remote.postings[0]
    raw["requirements"], raw["optional"] = {}, {}
    for p in raw["products"]:
        p.pop("mandatory_mark", None)
    remote.make_context(raw, cis=False, gtd=False)
    execute(assembly.orders, owner, account, db)
    view = open_window(pipeline, owner)
    for code in (MUFT, KONVERT, KONVERT):
        view = call(assembly, owner, view, "scan", code)["session"]
    assert call(assembly, owner, view, "verify")["ok"]
    assert [path for path, _, _ in remote.instance_calls] == [GET, POSTING]


def test_existing_confirmed_remote_data_is_imported_without_repeated_set(pipeline, owner):
    assembly, _, remote, _ = pipeline
    raw = remote.contexts[remote.postings[0]["posting_number"]]["products"][0]["exemplars"][0]
    raw.update(gtd=GTD, is_gtd_absent=False, marks=[{"mark_type": "mandatory_mark", "mark": mark()}])
    view = open_window(pipeline, owner)
    job(pipeline)
    accepted = state(pipeline, owner, view)
    assert accepted["data"]["status"] == "accepted" and accepted["scanned"] == 0
    assert not any(path == SET for path, _, _ in remote.instance_calls)


def test_optional_flag_dropping_after_submission_does_not_erase_obligation(pipeline, owner):
    _, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    job(pipeline)
    for product in remote.contexts[remote.postings[0]["posting_number"]]["products"]:
        product["is_mandatory_mark_possible"] = False
        product["is_mandatory_mark_needed"] = False
        product["is_gtd_needed"] = False
    job(pipeline, due=True)
    assert state(pipeline, owner, view)["data"]["status"] == "accepted"


def test_definitive_rate_limit_can_retry_after_fresh_reconciliation(pipeline, owner):
    _, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    remote.instance_mode = "set_429"
    job(pipeline)
    remote.instance_mode = "ok"
    accepted = finish_data(pipeline, owner, view)
    assert accepted["data"]["status"] == "accepted"
    assert sum(path == SET for path, _, _ in remote.instance_calls) == 2


def test_finished_worker_without_confirmation_stays_uncertain_on_manual_recheck(pipeline, owner, db):
    assembly, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    remote.instance_mode = "set_no_effect_timeout"
    with db.transaction() as tx:
        tx.scalar(select(Job).where(Job.kind == "instances.submit")).max_attempts = 1
    job(pipeline)
    current = state(pipeline, owner, view)
    assert current["data"]["status"] == "uncertain" and not current["data"]["can_verify"]
    result = call(assembly, owner, current, "check_data")
    assert result["ok"]
    job(pipeline, due=True)
    assert sum(path == SET for path, _, _ in remote.instance_calls) == 1
    assert not state(pipeline, owner, view)["data"]["can_verify"]


def test_cancel_then_resume_uncertain_submission_preserves_binding_and_never_resends(pipeline, owner, db):
    assembly, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    remote.instance_mode = "set_no_effect_timeout"
    with db.transaction() as tx:
        tx.scalar(select(Job).where(Job.kind == "instances.submit")).max_attempts = 1
    job(pipeline)
    current = state(pipeline, owner, view)
    call(assembly, owner, current, "cancel")
    reopened = assembly.open(owner, uuid4(), code=MUFT)["session"]
    job(pipeline)
    activated = call(assembly, owner, reopened, "activate")
    assert activated["ok"] and activated["session"]["data"]["status"] == "uncertain"
    assert activated["session"]["data"]["items"]
    result = call(assembly, owner, activated["session"], "check_data")
    assert result["ok"]
    job(pipeline, due=True)
    assert sum(path == SET for path, _, _ in remote.instance_calls) == 1
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(MarkingAssignment)) == 1


def test_code_scanned_into_gtd_field_is_rejected_without_storage(pipeline, owner, db):
    assembly, data, _, _ = pipeline
    view = open_window(pipeline, owner, code=MUFT)
    raw = mark().replace("\x1d", "")
    result = call(assembly, owner, view, "gtd", UUID(target(view)), raw)
    assert not result["ok"] and raw not in json.dumps(result)
    with db.transaction() as tx:
        values = [data._open(d.encrypted_values) for d in tx.scalars(select(UnitDetail))]
        assert raw not in repr(values)


def test_changed_exemplar_ids_before_dispatch_are_never_guessed(pipeline, owner):
    _, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    remote.contexts[remote.postings[0]["posting_number"]]["products"][0]["exemplars"][0]["exemplar_id"] += (
        9000
    )
    job(pipeline)
    assert state(pipeline, owner, view)["data"]["status"] == "stale"
    assert not any(path == SET for path, _, _ in remote.instance_calls)


def test_chz_acceptance_and_gtd_rejection_have_separate_results(pipeline, owner):
    assembly, _, remote, _ = pipeline
    view = input_data(pipeline, owner, gtd="bad-gtd")
    remote.instance_mode = "bad_gtd"
    job(pipeline)
    job(pipeline, due=True)
    rejected = state(pipeline, owner, view)
    assert rejected["data"]["status"] == "rejected" and not rejected["data"]["can_verify"]
    item = next(i for i in rejected["data"]["items"].values() if i["cis"])
    assert item["mark_status"] == "accepted" and item["units"][0]["gtd_status"] == "rejected"
    changed = call(assembly, owner, rejected, "gtd", UUID(item["units"][0]["id"]), GTD)
    assert changed["ok"]
    assert (
        next(i for i in changed["session"]["data"]["items"].values() if i["cis"])["mark_status"] == "accepted"
    )


def test_ready_order_does_not_show_staged_chz_as_applied_when_gtd_failed(pipeline, owner, db):
    assembly, _, remote, account = pipeline
    remote.postings[0].update(status="awaiting_deliver", substatus="posting_formed")
    remote.make_context(remote.postings[0])
    execute(assembly.orders, owner, account, db)
    view = input_data(pipeline, owner, gtd="bad-gtd")
    remote.instance_mode = "bad_gtd"
    job(pipeline)
    job(pipeline, due=True)
    current = state(pipeline, owner, view)
    assert current["data"]["status"] == "rejected"
    assert next(i for i in current["data"]["items"].values() if i["cis"])["mark_status"] != "accepted"
    assert not any(path == UPDATE for path, _, _ in remote.instance_calls)


def test_concurrent_claim_in_two_accounts_has_one_global_assignment(pipeline, owner, identity, db):
    from concurrent.futures import ThreadPoolExecutor

    assembly, _, _, _ = pipeline
    first = open_window(pipeline, owner, code=MUFT)
    other = identity.create_user(owner, "packer", PASSWORD, "packer", frozenset({"pack"}))
    account = assembly.connections.create(owner, "Second", "998877", SecretStr("another-key"))
    execute(assembly.orders.catalog, owner, account, db)
    execute(assembly.orders, owner, account, db)
    second = assembly.open(other, uuid4(), code=MUFT, account_id=account)["session"]
    job(pipeline)
    second = call(assembly, other, second, "activate")["session"]
    job(pipeline)
    second = call(assembly, other, second, "activate")["session"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(
            pool.map(
                lambda args: call(assembly, args[0], args[1], "scan", mark()),
                [(owner, first), (other, second)],
            )
        )
    assert sum(r["ok"] for r in result) == 1
    assert next(r["message"] for r in result if not r["ok"]).startswith(
        "Данный Честный знак уже используется в заказе: "
    )
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(MarkingAssignment)) == 1


def test_same_serial_with_different_crypto_cannot_replace_existing_identity(pipeline, owner, db):
    assembly, _, _, _ = pipeline
    first = open_window(pipeline, owner, code=MUFT)
    first = call(assembly, owner, first, "scan", mark())["session"]
    call(assembly, owner, first, "cancel")
    second = open_window(pipeline, owner, code=MUFT)
    # A reserved code remains assigned; direct capture of conflicting full data is rejected.
    with db.transaction() as tx:
        context = tx.get(AssemblySession, UUID(second["id"])).instance_context_id
        manifest = assembly.orders.packing_manifest(tx, UUID(second["shipment_id"]))
        with pytest.raises(Exception, match="отличаются полные данные"):
            with tx.begin_nested():
                assembly.instances.capture(
                    tx, owner, context, manifest, UUID(second["last_unit_id"]), mark(crypto="DIFFERENT")
                )
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(MarkingUnit)) == 1


def test_gtd_preserves_printable_unicode_and_market_validates_format(pipeline, owner):
    _, _, remote, _ = pipeline
    value = "10702020/123456/А012345"
    view = input_data(pipeline, owner, gtd=value)
    accepted = finish_data(pipeline, owner, view)
    assert accepted["data"]["can_verify"]
    sent = next(p for path, p, _ in remote.instance_calls if path == SET)
    assert next(p for p in sent["products"] if p["product_id"] == 634662543)["exemplars"][0]["gtd"] == value


def test_malformed_unicode_in_gtd_returns_domain_error(pipeline, owner):
    from pack_v2.shared.errors import DomainError

    assembly, _, _, _ = pipeline
    view = open_window(pipeline, owner, code=MUFT)
    with pytest.raises(DomainError):
        call(assembly, owner, view, "gtd", UUID(target(view)), "bad\ud800")


def test_rejected_imported_mark_still_requires_product_scan_first(pipeline, owner):
    assembly, _, remote, _ = pipeline
    unit = remote.contexts[remote.postings[0]["posting_number"]]["products"][0]["exemplars"][0]
    unit.update(gtd=GTD, is_gtd_absent=False, marks=[{"mark_type": "mandatory_mark", "mark": mark()}])
    view = open_window(pipeline, owner)
    remote.instance_mode = "bad_cis_status"
    job(pipeline)
    rejected = state(pipeline, owner, view)
    assert rejected["data"]["status"] == "rejected" and rejected["data"]["pending_unit_id"] is None
    scanned = call(assembly, owner, rejected, "scan", MUFT)
    assert scanned["ok"] and scanned["session"]["scanned"] == 1
    assert scanned["session"]["data"]["pending_unit_id"]


def test_changed_remote_box_count_is_not_overwritten_by_instance_payload(pipeline, owner):
    _, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    remote.contexts[remote.postings[0]["posting_number"]]["multi_box_qty"] = 2
    job(pipeline)
    assert state(pipeline, owner, view)["data"]["status"] == "stale"
    assert not any(path == SET for path, _, _ in remote.instance_calls)


def test_quantity_change_while_waiting_invalidates_the_submission(pipeline, owner):
    _, _, remote, _ = pipeline
    view = input_data(pipeline, owner)
    product = remote.contexts[remote.postings[0]["posting_number"]]["products"][1]
    product["quantity"] = 1
    product["exemplars"] = product["exemplars"][:1]
    job(pipeline)
    assert state(pipeline, owner, view)["data"]["status"] == "stale"
    assert not any(path == SET for path, _, _ in remote.instance_calls)
