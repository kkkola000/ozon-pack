"""Assembly uses real PostgreSQL and a mocked read-only Ozon adapter."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from conftest import PASSWORD, login
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from test_orders import execute, posting
from test_orders import setup_orders as setup_orders

from pack_v2.cli import migration_config
from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.jobs import Queue
from pack_v2.infrastructure.models import AuditEvent, Job
from pack_v2.modules.assembly.models import AssemblyCommand, AssemblySession, AssemblyUnit
from pack_v2.modules.assembly.public import Assembly
from pack_v2.modules.catalog.models import Offer, OfferCode, Product, ProductCode, ProductComponent
from pack_v2.modules.orders.models import (
    OrderLine,
    OrderSync,
    OrderUnit,
    Package,
    Shipment,
    ShipmentUnit,
)
from pack_v2.shared.errors import Conflict, Forbidden, NotFound

MUFT = "OZN634662543"
KONVERT = "OZN6225436164"


@pytest.fixture
def assembly(setup_orders, owner, db, settings):
    orders, catalog, connections, account, remote = setup_orders
    execute(catalog, owner, account, db)
    execute(orders, owner, account, db)
    service = Assembly(
        db,
        connections,
        orders,
        SecretBox(settings.encryption_key.get_secret_value()),
        settings.secret_key.get_secret_value(),
    )
    return service


def work(assembly):
    job = Queue(assembly.db).claim(lease_seconds=120)
    assert job and job.kind == "orders.sync"
    assembly.orders.perform(job)
    return job


def call(assembly, actor, view, action, *args, command_id=None):
    return getattr(assembly, action)(
        actor, command_id or uuid4(), UUID(view["id"]), UUID(view["lease_token"]), view["revision"], *args
    )


def opened(assembly, owner, *, code="ii50082908916", shipment_id=None):
    result = assembly.open(
        owner, uuid4(), **({"shipment_id": shipment_id} if shipment_id else {"code": code})
    )
    assert result["ok"] and result["session"]["status"] == "opening", result
    work(assembly)
    result = call(assembly, owner, result["session"], "activate")
    assert result["ok"] and result["session"]["status"] == "scanning", result
    return result["session"]


def complete(assembly, owner, view):
    for item in view["items"]:
        for _ in range(item["need"] - item["scanned"]):
            view = call(assembly, owner, view, "scan", item["codes"][0])["session"]
    result = call(assembly, owner, view, "verify")
    assert result["ok"]
    return result["session"]


def count(db, model):
    with db.transaction() as tx:
        return tx.scalar(select(func.count()).select_from(model))


def expire(assembly, view):
    with assembly.db.transaction() as tx:
        tx.get(AssemblySession, UUID(view["id"])).lease_until = datetime.now(UTC) - timedelta(seconds=1)


def test_open_is_queued_then_creates_stable_physical_units(assembly, owner, db):
    remote = assembly.connections.reader_factory.__self__
    before = len(remote.calls)
    result = assembly.open(owner, uuid4(), code="ii50082908916")
    assert len(remote.calls) == before and count(db, OrderUnit) == 0
    assert result["session"]["status"] == "opening"
    waiting = call(assembly, owner, result["session"], "activate")
    assert waiting["pending"] and waiting["session"]["scanned"] == 0
    work(assembly)
    view = call(assembly, owner, result["session"], "activate")["session"]
    assert view["need"] == 3 and view["scanned"] == 0
    assert count(db, OrderUnit) == count(db, ShipmentUnit) == count(db, AssemblyUnit) == 3
    assert count(db, OrderLine) == 2 and count(db, Package) == 0
    with db.transaction() as tx:
        assert {r.quantity_source for r in tx.scalars(select(OrderLine))} == {"allocated"}
        assert all(r.price is None for r in tx.scalars(select(OrderLine)))
    assert [path for path, _, _ in remote.calls[before:]] == ["/v3/posting/fbs/get"]


def test_product_opens_and_initial_scan_is_counted_only_once(assembly, owner, db):
    request_id = uuid4()
    request = assembly.open(owner, request_id, code=MUFT)
    with db.transaction() as tx:
        encrypted = tx.get(AssemblySession, UUID(request["session"]["id"])).initial_code
        assert MUFT.encode() not in encrypted
        assert assembly.box.open(encrypted).decode() == MUFT
    work(assembly)
    view = call(assembly, owner, request["session"], "activate")["session"]
    assert view["scanned"] == 1 and view["last_unit_id"]
    replay = assembly.open(owner, request_id, code=MUFT)
    assert replay["session"]["scanned"] == 1
    assert call(assembly, owner, view, "activate")["session"]["scanned"] == 1
    with db.transaction() as tx:
        assert tx.get(AssemblySession, UUID(view["id"])).initial_code is None


def test_scan_retry_is_idempotent_but_next_scan_counts_next_unit(assembly, owner, db):
    view = opened(assembly, owner)
    command_id = uuid4()
    first = call(assembly, owner, view, "scan", KONVERT, command_id=command_id)
    repeated = call(assembly, owner, view, "scan", KONVERT, command_id=command_id)
    assert first["unit_id"] == repeated["unit_id"] and repeated["session"]["scanned"] == 1
    second = call(assembly, owner, repeated["session"], "scan", KONVERT)
    assert second["unit_id"] != first["unit_id"] and second["session"]["scanned"] == 2
    extra = call(assembly, owner, second["session"], "scan", KONVERT)
    assert not extra["ok"] and extra["code"] == "already_counted" and extra["session"]["scanned"] == 2
    stale = call(assembly, owner, view, "scan", MUFT)
    assert not stale["ok"] and stale["code"] == "revision_conflict"
    with pytest.raises(Conflict):
        call(assembly, owner, view, "scan", MUFT, command_id=command_id)
    assert count(db, AuditEvent) > 0


def test_concurrent_same_request_counts_once(assembly, owner):
    view, request_id = opened(assembly, owner), uuid4()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: call(assembly, owner, view, "scan", KONVERT, command_id=request_id), range(4))
        )
    assert len({r["unit_id"] for r in results}) == 1
    assert assembly.session(owner, UUID(view["id"]))["scanned"] == 1


def test_concurrent_different_requests_cannot_use_same_revision(assembly, owner):
    view = opened(assembly, owner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call(assembly, owner, view, "scan", KONVERT), range(2)))
    assert sum(r["ok"] for r in results) == 1
    assert assembly.session(owner, UUID(view["id"]))["scanned"] == 1


@pytest.mark.parametrize(
    "code",
    [
        "not-in-order",
        "010466029107098221TEST-SERIAL\x1d91ABCD\x1d92private-crypto",
        "<gs>010466029107098221TEST",
        "\\u001d010466029107098221TEST",
    ],
)
def test_unknown_or_marking_scan_never_becomes_a_product_or_stored_payload(assembly, owner, db, code):
    view = opened(assembly, owner)
    result = call(assembly, owner, view, "scan", code)
    assert not result["ok"] and result["session"]["scanned"] == 0 and code not in json.dumps(result)
    with db.transaction() as tx:
        stored = [(r.fingerprint, r.outcome) for r in tx.scalars(select(AssemblyCommand))]
        audits = [r.details for r in tx.scalars(select(AuditEvent))]
    assert code not in repr(stored) and code not in repr(audits)


def test_verify_requires_all_units_and_does_not_mark_marketplace_ready(assembly, owner, db):
    view = opened(assembly, owner)
    assert call(assembly, owner, view, "verify")["code"] == "incomplete"
    view = complete(assembly, owner, view)
    assert view["status"] == "verified" and view["scanned"] == view["need"] == 3
    assert assembly.current(owner) is None
    assert assembly.queue(owner)[0]["verified"]
    with db.transaction() as tx:
        shipment = tx.get(Shipment, UUID(view["shipment_id"]))
        assert shipment.state == "awaiting_assembly" and not shipment.requirements_complete
        assert tx.scalar(select(AuditEvent).where(AuditEvent.kind == "assembly_products_verified"))
    assert {path for path, _, _ in assembly.connections.reader_factory.__self__.calls} <= {
        "/v3/product/list",
        "/v3/product/info/list",
        "/v4/posting/fbs/list",
        "/v3/posting/fbs/get",
    }
    assert assembly.open(owner, uuid4(), code="ii50082908916")["code"] == "already_verified"


def test_cancel_keeps_allocations_and_history_then_reuses_unit_ids(assembly, owner, db):
    view = opened(assembly, owner, code=MUFT)
    with db.transaction() as tx:
        ids = set(tx.scalars(select(OrderUnit.id)))
    result = call(assembly, owner, view, "cancel")
    assert result["ok"] and assembly.current(owner) is None
    new = opened(assembly, owner)
    assert new["scanned"] == 0 and new["id"] != view["id"]
    with db.transaction() as tx:
        assert set(tx.scalars(select(OrderUnit.id))) == ids
        assert all(u.released_at is None for u in tx.scalars(select(ShipmentUnit)))
        assert any(
            u.scanned_at
            for u in tx.scalars(select(AssemblyUnit).where(AssemblyUnit.session_id == UUID(view["id"])))
        )


def test_expired_lease_resumes_scans_for_same_user_with_new_token(assembly, owner):
    view = opened(assembly, owner, code=MUFT)
    expire(assembly, view)
    assert assembly.current(owner)["status"] == "expired"
    resumed = opened(assembly, owner)
    assert resumed["id"] == view["id"] and resumed["lease_token"] != view["lease_token"]
    assert resumed["scanned"] == 1
    assert call(assembly, owner, view, "scan", KONVERT)["code"] == "lease_changed"


def test_lease_expiry_blocks_mutations_and_get_does_not_renew(assembly, owner, db):
    view = opened(assembly, owner)
    with db.transaction() as tx:
        before = tx.get(AssemblySession, UUID(view["id"])).lease_until
    assembly.session(owner, UUID(view["id"]))
    with db.transaction() as tx:
        assert tx.get(AssemblySession, UUID(view["id"])).lease_until == before
    expire(assembly, view)
    assert call(assembly, owner, view, "scan", MUFT)["session"]["status"] == "expired"


def test_one_user_and_one_shipment_claim_and_no_foreign_access(assembly, owner, identity, db):
    other = identity.create_user(owner, "packer", PASSWORD, "packer", frozenset({"pack"}))
    view = opened(assembly, owner, code=MUFT)
    assert assembly.open(other, uuid4(), code="ii50082908916")["code"] == "busy"
    with pytest.raises(NotFound):
        assembly.session(other, UUID(view["id"]))
    with pytest.raises(NotFound):
        call(assembly, other, view, "scan", MUFT)
    expire(assembly, view)
    new = opened(assembly, other)
    assert new["id"] != view["id"] and new["scanned"] == 0
    assert call(assembly, owner, view, "scan", MUFT)["code"] == "revision_conflict"
    assert count(db, OrderUnit) == 3


@pytest.mark.parametrize("change", ["quantity", "sku", "cancelled", "delivery", "missing", "ship_failed"])
def test_changed_or_unconfirmed_source_invalidates_old_scans(assembly, owner, db, change):
    view = opened(assembly, owner, code=MUFT)
    remote = assembly.connections.reader_factory.__self__
    row = remote.postings[0]
    if change == "quantity":
        row["products"][1]["quantity"] = 1
    elif change == "sku":
        row["products"][0]["sku"] = 999999
    elif change in ("cancelled", "delivery", "ship_failed"):
        row["status"] = {
            "cancelled": "cancelled",
            "delivery": "delivering",
            "ship_failed": "awaiting_deliver",
        }[change]
        row["substatus"] = "ship_failed" if change == "ship_failed" else row["status"]
    elif change == "missing":
        remote.postings = []
        remote.details_by_number.clear()
    execute(assembly.orders, owner, UUID(view["account_id"]), db)
    stale = call(assembly, owner, view, "scan", KONVERT)
    assert not stale["ok"] and stale["session"]["status"] == "stale"
    assert stale["session"]["scanned"] == 1


def test_awaiting_dispatch_and_cosmetic_updates_preserve_product_scans(assembly, owner, db):
    view = opened(assembly, owner, code=MUFT)
    row = assembly.connections.reader_factory.__self__.postings[0]
    row.update(status="awaiting_deliver", substatus="posting_formed")
    row["products"][0].update(name="Новое имя", price="999.00")
    execute(assembly.orders, owner, UUID(view["account_id"]), db)
    result = call(assembly, owner, view, "scan", KONVERT)
    assert result["ok"] and result["session"]["scanned"] == 2


@pytest.mark.parametrize("change", ["disable", "rotate"])
def test_account_change_fences_old_requests(assembly, owner, change):
    view = opened(assembly, owner)
    account = next(a for a in assembly.connections.list_accounts(owner) if str(a.id) == view["account_id"])
    if change == "disable":
        assembly.connections.set_active(owner, account.id, account.revision, False)
    else:
        assembly.connections.update(
            owner, account.id, account.revision, account.title, SecretStr("replacement-key")
        )
    result = call(assembly, owner, view, "scan", MUFT)
    assert not result["ok"] and result["session"]["status"] == "stale"
    assert call(assembly, owner, result["session"], "cancel")["ok"]


def test_rotated_key_allows_new_verification_after_old_proof(assembly, owner):
    view = complete(assembly, owner, opened(assembly, owner))
    account = assembly.connections.list_accounts(owner)[0]
    assembly.connections.update(owner, account.id, account.revision, account.title, SecretStr("new-key"))
    assert not assembly.queue(owner)[0]["verified"]
    assert opened(assembly, owner, code=MUFT)["scanned"] == 1
    assert assembly.session(owner, UUID(view["id"]))["status"] == "stale"


@pytest.mark.parametrize("mode", ["denied", "rate"])
def test_remote_error_is_blocked_or_waiting_not_false_ready(assembly, owner, mode):
    remote = assembly.connections.reader_factory.__self__
    remote.fbs_mode = mode
    view = assembly.open(owner, uuid4(), code=MUFT)["session"]
    work(assembly)
    result = call(assembly, owner, view, "activate")
    assert not result["ok"] and result["session"]["scanned"] == 0
    assert result.get("pending") if mode == "rate" else result["session"]["status"] == "blocked"
    assert "synthetic-orders-secret" not in json.dumps(result)
    if mode == "denied":
        remote.fbs_mode = "ok"
        view = call(assembly, owner, result["session"], "retry")["session"]
        work(assembly)
        assert call(assembly, owner, view, "activate")["session"]["scanned"] == 1


def test_point_job_does_not_replace_full_sync_status_and_can_coexist(assembly, owner, db):
    before = assembly.orders.sync_status(owner)[0]
    view = assembly.open(owner, uuid4(), code=MUFT)["session"]
    full_id = assembly.orders.request_sync(owner, UUID(view["account_id"]))[0]
    with db.transaction() as tx:
        point = tx.get(OrderSync, tx.get(AssemblySession, UUID(view["id"])).refresh_run_id)
        assert point.mode == "point" and point.id != full_id
        assert tx.get(OrderSync, full_id).mode == "full"
    work(assembly)
    assert assembly.orders.sync_status(owner)[0].id == full_id != before.id


def test_late_target_response_cannot_overwrite_newer_source(assembly, owner, db):
    view = assembly.open(owner, uuid4(), code=MUFT)["session"]
    remote = assembly.connections.reader_factory.__self__

    def newer(_request, _data):
        with db.transaction() as tx:
            row = tx.get(Shipment, UUID(view["shipment_id"]))
            row.revision += 1
            row.warehouse_name = "Более новая версия"

    remote.fbs_callback = newer
    work(assembly)
    result = call(assembly, owner, view, "activate")
    assert result.get("pending")
    with db.transaction() as tx:
        assert tx.get(Shipment, UUID(view["shipment_id"])).warehouse_name == "Более новая версия"


@pytest.mark.parametrize("problem", ["unmatched", "no_codes", "components"])
def test_unsupported_composition_does_not_partially_allocate(assembly, owner, db, problem):
    remote = assembly.connections.reader_factory.__self__
    with db.transaction() as tx:
        offer = tx.scalar(select(Offer).where(Offer.article == "PF-M02"))
        if problem == "unmatched":
            remote.details_by_number["59511155-0034-1"]["products"][0]["sku"] = 987654321
        elif problem == "no_codes":
            tx.execute(delete(OfferCode).where(OfferCode.offer_id == offer.id))
        else:
            product = Product(id=uuid4(), name="Комплект")
            component = Product(id=uuid4(), name="Деталь")
            tx.add_all([product, component])
            tx.flush()
            offer.product_id = product.id
            tx.add(
                ProductComponent(product_id=product.id, component_id=component.id, quantity=1, part_key="a")
            )
    view = assembly.open(owner, uuid4(), code="ii50082908916")["session"]
    work(assembly)
    result = call(assembly, owner, view, "activate")
    assert not result["ok"] and result["session"]["status"] == "blocked"
    assert count(db, OrderUnit) == count(db, OrderLine) == count(db, ShipmentUnit) == 0


def test_ambiguous_shared_barcode_is_not_arbitrarily_counted(assembly, owner, db):
    with db.transaction() as tx:
        tx.add_all(
            OfferCode(offer_id=o.id, kind="barcode", value="shared", origin="manual")
            for o in tx.scalars(select(Offer))
        )
    view = opened(assembly, owner)
    result = call(assembly, owner, view, "scan", "shared")
    assert result["code"] == "ambiguous" and result["session"]["scanned"] == 0


def test_explicit_main_product_codes_are_accepted_across_linked_offers(assembly, owner, db):
    with db.transaction() as tx:
        product = Product(id=uuid4(), name="Основная карточка")
        tx.add(product)
        tx.flush()
        tx.scalar(select(Offer).where(Offer.article == "PF-M02")).product_id = product.id
        tx.add(ProductCode(product_id=product.id, kind="barcode", value="own-000001", origin="manual"))
    view = opened(assembly, owner, code="own-000001")
    assert view["scanned"] == 1


def test_remote_downsize_blocks_replanning_without_releasing_units(assembly, owner, db):
    view = opened(assembly, owner)
    call(assembly, owner, view, "cancel")
    remote = assembly.connections.reader_factory.__self__
    remote.details_by_number["59511155-0034-1"]["products"][1]["quantity"] = 1
    request = assembly.open(owner, uuid4(), code="ii50082908916")
    work(assembly)
    result = call(assembly, owner, request["session"], "activate")
    assert not result["ok"] and result["session"]["status"] == "blocked"
    assert count(db, OrderUnit) == 3
    with db.transaction() as tx:
        assert all(r.released_at is None for r in tx.scalars(select(ShipmentUnit)))


def test_next_product_order_excludes_verified_and_busy_and_sorts_deadline(assembly, owner, identity, db):
    complete(assembly, owner, opened(assembly, owner))
    remote = assembly.connections.reader_factory.__self__
    second = posting(
        "other-2",
        order_id=777,
        order_number="other",
        parent_posting_number=None,
        scanit="ii-other",
        shipment_date="2026-11-23T10:00:00Z",
    )
    remote.postings.append(second)
    remote.details_by_number["other-2"] = second
    account = assembly.connections.list_accounts(owner)[0].id
    execute(assembly.orders, owner, account, db)
    other = identity.create_user(owner, "packer", PASSWORD, "packer", frozenset({"pack"}))
    view = opened(assembly, other, code=MUFT)
    assert view["posting_number"] == "other-2"
    assert assembly.open(owner, uuid4(), code=MUFT)["code"] == "not_found"
    assert assembly.open(other, uuid4(), code="59511155-0034-1")["code"] == "other_session"


def test_api_authorization_csrf_no_echo_and_gets_do_not_schedule(assembly, owner, identity, client, db):
    client.app.state.assembly = assembly
    client.app.state.connections = assembly.connections
    identity.create_user(owner, "packer", PASSWORD, "packer", frozenset({"pack"}))
    assert client.get("/api/v2/assembly/current").status_code == 401
    login(client, "packer")
    response = client.get("/pack")
    assert response.status_code == 200
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', response.text).group(1)
    before = count(db, Job)
    assert client.get("/api/v2/assembly/current").json() == {"session": None}
    assert count(db, Job) == before
    payload = {"command_id": str(uuid4()), "code": MUFT}
    assert client.post("/api/v2/assembly/open", json=payload).status_code == 403
    response = client.post("/api/v2/assembly/open", json=payload, headers={"x-csrf-token": csrf})
    assert response.status_code == 200 and response.json()["session"]["status"] == "opening"
    assert client.get("/api/v2/orders").status_code == 403
    assert client.get("/api/v2/products").status_code == 403
    secret = "PRIVATE-SCANNED-PAYLOAD" * 250
    response = client.post(
        "/api/v2/assembly/open",
        json={"command_id": str(uuid4()), "code": secret},
        headers={"x-csrf-token": csrf},
    )
    assert response.status_code == 422 and "PRIVATE-SCANNED-PAYLOAD" not in response.text
    assert client.post("/api/v2/assembly/" + str(uuid4()) + "/finish", json={}).status_code == 404
    assert (
        client.post(
            "/api/v2/assembly/open",
            content=b"x" * 70000,
            headers={"x-csrf-token": csrf, "content-type": "application/json"},
        ).status_code
        == 413
    )


def test_no_pack_permission_rejected(assembly, owner, identity):
    user = identity.create_user(owner, "viewer", PASSWORD, "packer", frozenset({"orders"}))
    with pytest.raises(Forbidden):
        assembly.open(user, uuid4(), code=MUFT)
    with pytest.raises(Forbidden):
        assembly.current(user)


def test_owner_scoped_command_cannot_reference_foreign_session(assembly, owner, identity, db):
    other = identity.create_user(owner, "packer", PASSWORD, "packer", frozenset({"pack"}))
    view = opened(assembly, owner)
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(
            AssemblyCommand(
                user_id=other.id,
                command_id=uuid4(),
                fingerprint="a" * 64,
                action="scan",
                session_id=UUID(view["id"]),
                outcome={},
            )
        )


def test_populated_migration_downgrade_fences_point_jobs_and_preserves_orders(assembly, owner, db, settings):
    view = opened(assembly, owner)
    call(assembly, owner, view, "cancel")
    assembly.open(owner, uuid4(), code=MUFT)
    before = count(db, Shipment)
    try:
        command.downgrade(migration_config(settings), "0004_orders")
        assert count(db, Shipment) == before
        with db.transaction() as tx:
            assert tx.scalar(select(func.count()).select_from(OrderUnit)) == 3
        command.upgrade(migration_config(settings), "head")
        command.check(migration_config(settings))
        with db.transaction() as tx:
            assert not list(tx.scalars(select(OrderSync).where(OrderSync.mode == "point")))
    finally:
        command.upgrade(migration_config(settings), "head")


def test_same_label_across_accounts_requires_scope(assembly, owner, db):
    connections = assembly.connections
    second = connections.create(owner, "Another Ozon", "987654", SecretStr("other-account-key"))
    execute(assembly.orders.catalog, owner, second, db)
    execute(assembly.orders, owner, second, db)
    ambiguous = assembly.open(owner, uuid4(), code="ii50082908916")
    assert ambiguous["code"] == "ambiguous" and ambiguous["session"] is None
    selected = assembly.open(owner, uuid4(), code="ii50082908916", account_id=second)
    assert selected["ok"] and selected["session"]["account_id"] == str(second)
    with pytest.raises(NotFound):
        assembly.open(
            owner, uuid4(), shipment_id=UUID(selected["session"]["shipment_id"]), account_id=uuid4()
        )


def test_concurrent_open_does_not_allow_two_packers(assembly, owner, identity, db):
    other = identity.create_user(owner, "packer", PASSWORD, "packer", frozenset({"pack"}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda actor: assembly.open(actor, uuid4(), code="ii50082908916"), [owner, other])
        )
    assert sum(r["ok"] for r in results) == 1
    assert {r["code"] for r in results} == {"opening", "busy"}
    assert count(db, AssemblySession) == 1


def test_verified_source_cancel_and_restore_requires_new_product_proof(assembly, owner, db):
    view = complete(assembly, owner, opened(assembly, owner))
    remote = assembly.connections.reader_factory.__self__
    remote.postings[0].update(status="cancelled", substatus="cancelled")
    execute(assembly.orders, owner, UUID(view["account_id"]), db)
    assert assembly.session(owner, UUID(view["id"]))["status"] == "stale"
    remote.postings[0].update(status="awaiting_deliver", substatus="posting_formed")
    remote.details_by_number["59511155-0034-1"] = remote.postings[0]
    execute(assembly.orders, owner, UUID(view["account_id"]), db)
    assert not assembly.queue(owner)[0]["verified"]
    assert opened(assembly, owner)["scanned"] == 0


def test_point_missing_never_invents_cancellation(assembly, owner, db):
    view = assembly.open(owner, uuid4(), code="ii50082908916")["session"]
    assembly.connections.reader_factory.__self__.details_by_number.clear()
    work(assembly)
    result = call(assembly, owner, view, "activate")
    assert result["session"]["status"] == "blocked" and not result["ok"]
    with db.transaction() as tx:
        shipment = tx.get(Shipment, UUID(view["shipment_id"]))
        assert shipment.needs_review and shipment.state == "awaiting_assembly"


def test_live_codes_can_change_without_renumbering_units(assembly, owner, db):
    view = opened(assembly, owner)
    with db.transaction() as tx:
        offer = tx.scalar(select(Offer).where(Offer.article == "PF-M02"))
        tx.execute(delete(OfferCode).where(OfferCode.offer_id == offer.id))
        tx.add(OfferCode(offer_id=offer.id, kind="barcode", value="new-code", origin="manual"))
    assert call(assembly, owner, view, "scan", MUFT)["code"] == "wrong_product"
    result = call(assembly, owner, view, "scan", "new-code")
    assert result["ok"] and result["session"]["scanned"] == 1
    assert count(db, OrderUnit) == 3


def test_heartbeat_renews_lease_without_consuming_a_scan_revision(assembly, owner, db):
    view = opened(assembly, owner)
    with db.transaction() as tx:
        tx.get(AssemblySession, UUID(view["id"])).lease_until = datetime.now(UTC) + timedelta(seconds=30)
    result = assembly.heartbeat(owner, UUID(view["id"]), UUID(view["lease_token"]))
    assert result["ok"] and result["session"]["revision"] == view["revision"]
    assert datetime.fromisoformat(result["session"]["lease_until"]) > datetime.now(UTC) + timedelta(
        seconds=100
    )
    assert call(assembly, owner, view, "scan", MUFT)["ok"]


@pytest.mark.parametrize("code", ["\x00", "bad\ud800", "\r\n"])
def test_invalid_scanner_text_is_safe_and_never_reaches_database(assembly, owner, code):
    from pack_v2.shared.errors import DomainError

    with pytest.raises(DomainError):
        assembly.open(owner, uuid4(), code=code)
    view = opened(assembly, owner)
    with pytest.raises(DomainError):
        call(assembly, owner, view, "scan", code)
    assert assembly.session(owner, UUID(view["id"]))["scanned"] == 0
