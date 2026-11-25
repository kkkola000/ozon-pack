import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import PASSWORD, login
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from test_catalog import OzonMock

from pack_v2.cli import worker
from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.jobs import Queue
from pack_v2.infrastructure.models import Job
from pack_v2.integrations.ozon.access import OzonAccessChecker
from pack_v2.integrations.ozon.reader import OzonReadAdapter
from pack_v2.integrations.public import Connections
from pack_v2.modules.catalog.public import Catalog
from pack_v2.modules.orders.models import (
    Order,
    OrderLine,
    OrderSync,
    OrderUnit,
    Package,
    Shipment,
    ShipmentItem,
)
from pack_v2.modules.orders.public import Orders

KEY = "synthetic-orders-secret"


def posting(number="59511155-0034-1", **fields):
    raw = json.loads((Path(__file__).parent / "fixtures/ozon/posting.json").read_text())
    raw["posting_number"] = number
    raw.update(fields)
    return raw


class OrderRemote(OzonMock):
    def __init__(self, db):
        super().__init__(db)
        for row in self.listed:
            if row["product_id"] == 102:
                row["offer_id"] = "PF-F02"
        for row in self.details:
            if row["id"] == 102:
                row.update(
                    offer_id="PF-F02",
                    name="Тестовый конверт",
                    sku=6225436164,
                    sources=[],
                    barcodes=["OZN6225436164"],
                )
        self.postings = [posting()]
        self.details_by_number = {p["posting_number"]: deepcopy(p) for p in self.postings}
        self.fbs_mode = "ok"
        self.fbs_callback = None

    def factory(self, source, credentials):
        client = OzonReadAdapter(
            source, credentials, transport=httpx.MockTransport(self.handle), attempts=1, posting_page_size=2
        )
        self.clients.append(client)
        return client

    def handle(self, request):
        if request.url.path.startswith("/v3/product/"):
            return super().handle(request)
        assert self.db.engine.pool.checkedout() == 0
        data = json.loads(request.content)
        self.calls.append((request.url.path, data, request.headers["client-id"]))
        if self.fbs_callback:
            callback, self.fbs_callback = self.fbs_callback, None
            callback(request, data)
        if self.fbs_mode == "denied":
            return httpx.Response(403, json={"message": KEY})
        if self.fbs_mode == "rate":
            return httpx.Response(429, headers={"retry-after": "120"}, json={"message": KEY})
        if request.url.path == "/v4/posting/fbs/list":
            offset = int(data["cursor"][2:]) if data["cursor"] else 0
            if self.fbs_mode == "partial" and offset:
                return httpx.Response(503)
            statuses = data["filter"].get("statuses")
            rows = [p for p in self.postings if not statuses or p["status"] in statuses]
            selected = rows[offset : offset + data["limit"]]
            more = offset + len(selected) < len(rows)
            return httpx.Response(
                200,
                json={
                    "postings": selected,
                    "has_next": more,
                    "cursor": "p:" + str(offset + len(selected)) if more else "",
                },
            )
        assert request.url.path == "/v3/posting/fbs/get"
        assert data["with"]["product_exemplars"] is False
        raw = self.details_by_number.get(data["posting_number"])
        return (
            httpx.Response(404, json={"message": "not found"})
            if raw is None
            else httpx.Response(200, json={"result": raw})
        )


@pytest.fixture
def setup_orders(db, settings, owner):
    remote = OrderRemote(db)
    connections = Connections(
        db, SecretBox(settings.encryption_key.get_secret_value()), OzonAccessChecker(), remote.factory
    )
    account = connections.create(owner, "ANEX", "000123", SecretStr(KEY))
    catalog = Catalog(db, connections)
    orders = Orders(db, connections, catalog)
    return orders, catalog, connections, account, remote


def execute(service, owner, account, db):
    run_id = service.request_sync(owner, account)[0]
    job = Queue(db).claim(lease_seconds=120)
    assert job
    service.perform(job)
    return run_id, job


def saved_run(db, run_id):
    with db.transaction() as tx:
        return tx.get(OrderSync, run_id)


def counts(db):
    with db.transaction() as tx:
        return {
            model.__tablename__: tx.scalar(select(func.count()).select_from(model))
            for model in (Order, Shipment, ShipmentItem, OrderLine, OrderUnit, Package)
        }


def test_request_deduplicated_without_http_and_records_scope(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: orders.request_sync(owner, account)[0], range(4)))
    assert len(set(ids)) == 1 and remote.calls == []
    run = saved_run(db, ids[0])
    assert run.mode == "full" and run.until - run.since == timedelta(days=30)
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(OrderSync)) == 1
        assert tx.get(Job, run.job_id).payload == {"run_id": str(run.id)}


def test_roundtrip_orders_items_catalog_matching_and_no_physical_units(setup_orders, owner, db):
    orders, catalog, _, account, remote = setup_orders
    execute(catalog, owner, account, db)
    run, job = execute(orders, owner, account, db)
    assert saved_run(db, run).status == "succeeded"
    result = orders.list_shipments(owner)
    assert result.total == 1
    shipment = result.items[0]
    assert shipment.posting_number == "59511155-0034-1" and shipment.order_number == "59511155-0034"
    assert ("label_barcode", "ii50082908916") in shipment.references
    assert shipment.deadline == datetime(2026, 11, 22, 9, tzinfo=UTC)
    assert len(shipment.items) == 2 and all(i.match_state == "matched" for i in shipment.items)
    item = next(i for i in shipment.items if i.article == "PF-M02")
    assert item.quantity == 1 and item.price == "1234.5000" and item.currency == "RUB"
    assert item.codes == ("04660291070982", "OZN634662543")
    assert item.required_fields == ("gtd",) and item.optional_fields == ("cis",)
    assert not shipment.requirements_complete and shipment.package_count_hint == 2
    assert counts(db) == {
        "orders": 1,
        "shipments": 1,
        "shipment_items": 2,
        "order_lines": 0,
        "order_units": 0,
        "packages": 0,
    }
    with db.transaction() as tx:
        assert tx.get(Job, job.id).status == "succeeded"
    assert all(client.http.client.is_closed for client in remote.clients)
    assert "synthetic-private" not in repr(result) and KEY not in repr(result)


def test_repeated_pass_preserves_ids_and_content_revisions(setup_orders, owner, db):
    orders, _, _, account, _ = setup_orders
    execute(orders, owner, account, db)
    with db.transaction() as tx:
        first = {
            m.__tablename__: {r.id: r.revision for r in tx.scalars(select(m))}
            for m in (Order, Shipment, ShipmentItem)
        }
    execute(orders, owner, account, db)
    with db.transaction() as tx:
        second = {
            m.__tablename__: {r.id: r.revision for r in tx.scalars(select(m))}
            for m in (Order, Shipment, ShipmentItem)
        }
    assert first == second


def test_split_shipments_share_only_explicit_order_and_parent_relationship(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    parent = posting("59511155-0034-0", parent_posting_number=None, status="cancelled", substatus="cancelled")
    child = posting("59511155-0034-2", scanit="ii-second")
    remote.postings = [remote.postings[0], child, parent]
    run, _ = execute(orders, owner, account, db)
    assert saved_run(db, run).status == "succeeded"
    assert counts(db)["orders"] == 1 and counts(db)["shipments"] == 3
    rows = orders.list_shipments(owner).items
    parent_view = next(r for r in rows if r.posting_number.endswith("-0"))
    for row in rows:
        if row.id != parent_view.id:
            assert row.parent_id == parent_view.id
            assert len(orders.shipment(owner, row.id).siblings) == 2
    assert counts(db)["packages"] == 0 and counts(db)["order_units"] == 0


def test_no_parent_is_inferred_from_related_postings(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    remote.postings[0].pop("parent_posting_number")
    execute(orders, owner, account, db)
    result = orders.list_shipments(owner).items[0]
    assert result.parent_id is None and result.parent_number is None
    assert len(result.related_numbers) == 2


@pytest.mark.parametrize("scenario", ["self", "cycle", "other-order"])
def test_invalid_parent_relationships_never_publish_links(setup_orders, owner, db, scenario):
    orders, _, _, account, remote = setup_orders
    if scenario == "self":
        remote.postings = [posting(parent_posting_number="59511155-0034-1")]
    elif scenario == "cycle":
        remote.postings = [
            posting(parent_posting_number="59511155-0034-2"),
            posting("59511155-0034-2", parent_posting_number="59511155-0034-1"),
        ]
    else:
        remote.postings = [
            posting(parent_posting_number="different-1"),
            posting("different-1", order_id=999, order_number="different", parent_posting_number=None),
        ]
    run, _ = execute(orders, owner, account, db)
    assert saved_run(db, run).status == "failed"
    with db.transaction() as tx:
        assert all(r.parent_id is None for r in tx.scalars(select(Shipment)))


def test_identical_numbers_in_other_accounts_do_not_merge(setup_orders, owner, db):
    orders, _, connections, first, _ = setup_orders
    second = connections.create(owner, "Второй", "456", SecretStr("another-orders-key"))
    execute(orders, owner, first, db)
    execute(orders, owner, second, db)
    assert counts(db)["orders"] == 2 and counts(db)["shipments"] == 2
    assert orders.list_shipments(owner, account_id=first).total == 1
    assert orders.list_shipments(owner, account_id=second).total == 1


def test_composition_changes_retire_observations_without_deleting_or_allocating_units(
    setup_orders, owner, db
):
    orders, _, _, account, remote = setup_orders
    execute(orders, owner, account, db)
    before = orders.list_shipments(owner).items[0]
    with db.transaction() as tx:
        line = OrderLine(
            account_id=account, order_id=before.order_id, name="Существующая единица", quantity=1
        )
        tx.add(line)
        tx.flush()
        tx.add(OrderUnit(order_id=before.order_id, line_id=line.id, ordinal=1))
    remote.postings[0]["products"] = remote.postings[0]["products"][:1]
    remote.postings[0]["products"][0]["quantity"] = 3
    execute(orders, owner, account, db)
    current = orders.shipment(owner, before.id).shipment
    assert len([i for i in current.items if i.source_present]) == 1
    assert len([i for i in current.items if not i.source_present]) == 1
    assert next(i for i in current.items if i.source_present).quantity == 3
    assert counts(db)["order_units"] == 1 and counts(db)["shipment_items"] == 2


def test_missing_list_entry_is_rechecked_and_only_confirmed_status_changes(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    execute(orders, owner, account, db)
    remote.postings = []
    remote.details_by_number["59511155-0034-1"]["status"] = "cancelled"
    remote.details_by_number["59511155-0034-1"]["substatus"] = "cancelled"
    run, _ = execute(orders, owner, account, db)
    shipment = orders.list_shipments(owner).items[0]
    assert (
        saved_run(db, run).status == "succeeded"
        and shipment.state == "cancelled"
        and not shipment.needs_review
    )
    assert any(path == "/v3/posting/fbs/get" for path, _, _ in remote.calls)


def test_404_does_not_cancel_order_or_delete_composition(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    execute(orders, owner, account, db)
    remote.postings = []
    remote.details_by_number = {}
    run, _ = execute(orders, owner, account, db)
    row = orders.list_shipments(owner).items[0]
    assert saved_run(db, run).status == "succeeded"
    assert row.needs_review and row.state == "awaiting_assembly" and len(row.items) == 2
    assert orders.list_shipments(owner, state="awaiting_assembly").total == 0
    assert orders.list_shipments(owner, state="review").total == 1


def test_partial_list_never_runs_missing_reconciliation_or_claims_success(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    first, _ = execute(orders, owner, account, db)
    remote.postings = [
        posting("new-" + str(i), order_id=100 + i, order_number="order-" + str(i)) for i in range(3)
    ]
    remote.details_by_number = {}
    remote.fbs_mode = "partial"
    remote.calls = []
    partial, job = execute(orders, owner, account, db)
    assert saved_run(db, partial).status == "retrying"
    assert not any(path == "/v3/posting/fbs/get" for path, _, _ in remote.calls)
    assert all(not row.needs_review for row in orders.list_shipments(owner).items)
    assert orders.sync_status(owner)[0].last_success_at == saved_run(db, first).finished_at
    with db.transaction() as tx:
        tx.get(Job, job.id).available_at = datetime.now(UTC) - timedelta(seconds=1)
    remote.fbs_mode = "ok"
    orders.perform(Queue(db).claim())
    assert saved_run(db, partial).status == "succeeded"
    assert counts(db)["shipments"] == 4


def test_catalog_loaded_later_relinks_terminal_observations_without_recreating_them(setup_orders, owner, db):
    orders, catalog, _, account, remote = setup_orders
    remote.postings[0]["status"] = "delivered"
    remote.postings[0]["substatus"] = "delivered"
    execute(orders, owner, account, db)
    before = orders.list_shipments(owner).items[0]
    assert all(i.match_state == "unmatched" for i in before.items)
    execute(catalog, owner, account, db)
    remote.postings = []
    execute(orders, owner, account, db)
    after = orders.list_shipments(owner).items[0]
    assert after.id == before.id and {r.id for r in after.items} == {r.id for r in before.items}
    assert all(i.match_state == "matched" for i in after.items)


def test_known_sku_takes_precedence_and_article_alone_does_not_override_it(setup_orders, owner, db):
    orders, catalog, _, account, remote = setup_orders
    execute(catalog, owner, account, db)
    row = remote.postings[0]["products"][0]
    row["offer_id"] = "PF-F02"
    execute(orders, owner, account, db)
    item = next(i for i in orders.list_shipments(owner).items[0].items if i.source_line_id == "sku:634662543")
    assert item.match_state == "matched" and "OZN634662543" in item.codes
    row["sku"] = 999999
    execute(orders, owner, account, db)
    item = next(i for i in orders.list_shipments(owner).items[0].items if i.source_line_id == "sku:999999")
    assert item.match_state == "unmatched" and item.offer_id is None


def test_unknown_fields_keep_previous_values_and_do_not_clear_requirement_hints(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    execute(orders, owner, account, db)
    previous = orders.list_shipments(owner).items[0]
    raw = remote.postings[0]
    for key in (
        "shipment_date",
        "in_process_at",
        "delivery_method",
        "parent_posting_number",
        "scanit",
        "barcodes",
        "multi_box_qty",
        "requirements",
        "optional",
    ):
        raw.pop(key, None)
    for item in raw["products"]:
        for key in ("price", "currency_code", "offer_id", "name"):
            item.pop(key, None)
    execute(orders, owner, account, db)
    current = orders.list_shipments(owner).items[0]
    assert (
        current.deadline,
        current.warehouse_name,
        current.parent_number,
        current.package_count_hint,
        current.references,
    ) == (
        previous.deadline,
        previous.warehouse_name,
        previous.parent_number,
        previous.package_count_hint,
        previous.references,
    )
    assert [(i.name, i.article, i.price, i.required_fields, i.optional_fields) for i in current.items] == [
        (i.name, i.article, i.price, i.required_fields, i.optional_fields) for i in previous.items
    ]


def test_alias_provenance_preserves_unobserved_scanner_code_then_clears_explicitly(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    execute(orders, owner, account, db)
    raw = remote.postings[0]
    raw.pop("scanit")
    raw["barcodes"]["lower_barcode"] = "new-lower"
    execute(orders, owner, account, db)
    refs = orders.list_shipments(owner).items[0].references
    assert ("label_barcode", "ii50082908916") in refs and ("label_barcode", "new-lower") in refs
    assert ("label_barcode", "00054321") not in refs
    raw["scanit"] = None
    execute(orders, owner, account, db)
    assert ("label_barcode", "ii50082908916") not in orders.list_shipments(owner).items[0].references


def test_one_label_from_two_fields_is_kept_until_both_origins_are_cleared(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    raw = remote.postings[0]
    raw["barcodes"]["upper_barcode"] = "ii50082908916"
    execute(orders, owner, account, db)
    raw["scanit"] = None
    execute(orders, owner, account, db)
    assert ("label_barcode", "ii50082908916") in orders.list_shipments(owner).items[0].references
    raw["barcodes"]["upper_barcode"] = ""
    execute(orders, owner, account, db)
    assert ("label_barcode", "ii50082908916") not in orders.list_shipments(owner).items[0].references


@pytest.mark.parametrize("price", ["1.23456", "100000000000000"])
def test_prices_outside_storage_precision_not_silently_rounded(setup_orders, owner, db, price):
    orders, _, _, account, remote = setup_orders
    remote.postings[0]["products"][0]["price"] = {"amount": price, "currency": "RUB"}
    run, _ = execute(orders, owner, account, db)
    assert saved_run(db, run).status == "failed" and counts(db)["shipments"] == 0


def test_automatic_sync_uses_working_list_then_periodic_full_refresh(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    run, _ = execute(orders, owner, account, db)
    with db.transaction() as tx:
        tx.get(OrderSync, run).created_at -= timedelta(minutes=6)
    orders.schedule(5)
    job = Queue(db).claim()
    orders.perform(job)
    with db.transaction() as tx:
        current = tx.scalar(select(OrderSync).where(OrderSync.job_id == job.id))
        assert current.mode == "working" and current.status == "succeeded"
        current.created_at -= timedelta(minutes=6)
        tx.get(OrderSync, run).finished_at -= timedelta(days=2)
    orders.schedule(5)
    next_job = Queue(db).claim()
    orders.perform(next_job)
    with db.transaction() as tx:
        assert tx.scalar(select(OrderSync.mode).where(OrderSync.job_id == next_job.id)) == "full"
    calls = [data for path, data, _ in remote.calls if path == "/v4/posting/fbs/list"]
    assert "statuses" not in calls[0]["filter"] and "statuses" in calls[1]["filter"]


def test_rotation_and_expired_lease_cannot_publish_old_results(setup_orders, owner, db):
    orders, _, connections, account, remote = setup_orders
    remote.fbs_callback = lambda request, data: connections.update(
        owner, account, 0, "ANEX", SecretStr("new-key")
    )
    run, _ = execute(orders, owner, account, db)
    assert saved_run(db, run).status == "stale" and counts(db)["shipments"] == 0
    reclaimed = []
    old_run = orders.request_sync(owner, account)[0]
    old_job = Queue(db).claim()

    def reclaim(request, data):
        with db.transaction() as tx:
            tx.get(Job, old_job.id).lease_until = datetime.now(UTC) - timedelta(seconds=1)
        reclaimed.append(Queue(db).claim())

    remote.fbs_callback = reclaim
    orders.perform(old_job)
    assert counts(db)["shipments"] == 0
    orders.perform(reclaimed[0])
    assert saved_run(db, old_run).status == "succeeded" and not Queue(db).finish(old_job)


def test_cross_account_observations_and_runs_rejected_by_database(setup_orders, owner, db):
    orders, catalog, connections, account, _ = setup_orders
    run, _ = execute(orders, owner, account, db)
    shipment = orders.list_shipments(owner).items[0]
    other = connections.create(owner, "Другой", "456", SecretStr("different"))
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.get(Shipment, shipment.id).last_seen_run_id = run
        tx.get(Shipment, shipment.id).account_id = other
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(
            ShipmentItem(
                account_id=other, shipment_id=shipment.id, source_line_id="sku:1", name="wrong", quantity=1
            )
        )
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.get(Shipment, shipment.id).parent_id = shipment.id


def test_job_worker_dispatches_orders_and_keeps_catalog_handler(setup_orders, owner, db, identity):
    orders, catalog, _, account, _ = setup_orders
    run = orders.request_sync(owner, account)[0]
    worker(db, identity, once=True, catalog=catalog, catalog_minutes=0, orders=orders, orders_minutes=0)
    assert saved_run(db, run).status == "succeeded"


@pytest.mark.parametrize(
    "query",
    [
        "59511155-0034-1",
        "59511155-0034",
        "ii50082908916",
        "00054321",
        "634662543",
        "PF-M02",
        "Тестовые",
        "04660291070982",
    ],
)
def test_search_posting_ii_root_order_sku_and_product_barcode(setup_orders, owner, db, query):
    orders, catalog, _, account, _ = setup_orders
    execute(catalog, owner, account, db)
    execute(orders, owner, account, db)
    assert orders.list_shipments(owner, query=query).total == 1


def test_ambiguous_alias_returns_all_matches_and_does_not_merge_shipments(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    remote.postings.append(posting("59511155-0034-2"))
    execute(orders, owner, account, db)
    assert orders.list_shipments(owner, query="ii50082908916").total == 2
    assert counts(db)["shipments"] == 2


def test_web_api_authorization_csrf_and_database_only_reads(setup_orders, owner, db, client, identity):
    orders, catalog, connections, account, remote = setup_orders
    client.app.state.orders, client.app.state.catalog, client.app.state.connections = (
        orders,
        catalog,
        connections,
    )
    assert client.get("/api/v2/orders").status_code == 401
    login(client)
    execute(orders, owner, account, db)
    before = len(remote.calls)
    response = client.get("/orders?account_id=&q=ii50082908916")
    assert response.status_code == 200 and "59511155-0034-1" in response.text
    data = client.get("/api/v2/orders").json()
    shipment_id = data["items"][0]["id"]
    assert isinstance(data["items"][0]["items"][0]["price"], str)
    assert client.get("/orders/" + shipment_id).status_code == 200
    assert client.get("/api/v2/orders/" + shipment_id).status_code == 200
    assert len(remote.calls) == before and KEY not in response.text
    assert client.post("/api/v2/orders/sync", json={}).status_code == 403
    csrf = client.get("/api/v2/me").json()["csrf"]
    assert (
        client.post(
            "/api/v2/orders/sync", json={"account_id": str(account)}, headers={"x-csrf-token": csrf}
        ).status_code
        == 202
    )
    assert client.post("/orders/sync", data={"csrf": csrf, "account_id": ""}).status_code == 303
    assert client.get("/orders?page=0").status_code == 422
    assert client.post("/orders/sync", data={"csrf": csrf, "account_id": "bad"}).status_code == 422
    assert client.get("/api/v2/orders/sync").status_code == 200
    assert client.get("/api/v2/orders/not-a-uuid").status_code == 422
    assert client.post("/api/v2/orders/sync", content=b"x" * 20000).status_code == 413
    identity.create_user(owner, "orders-reader", PASSWORD, "packer", frozenset({"orders"}))
    assert client.post("/logout", data={"csrf": csrf}).status_code == 303
    login(client, "orders-reader")
    assert (
        client.get("/orders").status_code == 200 and client.get("/orders/" + shipment_id).status_code == 200
    )
    assert client.get("/api/v2/orders/sync").status_code == 200
    assert client.get("/products").status_code == 403
    csrf = client.get("/api/v2/me").json()["csrf"]
    assert client.post("/api/v2/orders/sync", json={}, headers={"x-csrf-token": csrf}).status_code == 403


def test_ship_failed_not_ready_and_missing_substatus_does_not_clear_known_failure(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    raw = remote.postings[0]
    raw.update(status="awaiting_deliver", substatus="ship_failed")
    execute(orders, owner, account, db)
    assert orders.list_shipments(owner).items[0].transition_failed
    assert orders.list_shipments(owner, state="awaiting_dispatch").total == 0
    raw.pop("substatus")
    execute(orders, owner, account, db)
    assert orders.list_shipments(owner).items[0].state == "unknown"
    raw["substatus"] = "awaiting_deliver"
    execute(orders, owner, account, db)
    assert orders.list_shipments(owner, state="awaiting_dispatch").total == 1


def test_incomplete_root_identity_not_invented_from_posting_number(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    remote.postings[0].pop("order_id")
    remote.postings[0].pop("order_number")
    run, _ = execute(orders, owner, account, db)
    assert saved_run(db, run).status == "retrying" and counts(db)["orders"] == 0


def test_existing_shipment_cannot_be_silently_moved_to_different_order(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    execute(orders, owner, account, db)
    before = orders.list_shipments(owner).items[0]
    remote.postings[0].update(order_id=999, order_number="999-NEW")
    run, _ = execute(orders, owner, account, db)
    assert saved_run(db, run).status == "failed"
    assert orders.list_shipments(owner).items[0].order_id == before.order_id
    assert counts(db)["orders"] == 1


def test_read_only_detail_escapes_external_names(setup_orders, owner, db, client):
    orders, catalog, connections, account, remote = setup_orders
    remote.postings[0]["products"][0]["name"] = '<script>alert("test")</script>'
    execute(orders, owner, account, db)
    client.app.state.orders, client.app.state.catalog, client.app.state.connections = (
        orders,
        catalog,
        connections,
    )
    login(client)
    shipment = orders.list_shipments(owner).items[0]
    html = client.get("/orders/" + str(shipment.id)).text
    assert "<script>alert(" not in html and "&lt;script&gt;" in html


def test_pagination_is_bounded_and_stable(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    remote.postings = [posting("posting-" + str(i), parent_posting_number=None) for i in range(31)]
    execute(orders, owner, account, db)
    first, second = orders.list_shipments(owner), orders.list_shipments(owner, page=2)
    assert first.total == 31 and first.pages == 2 and len(first.items) == 30 and len(second.items) == 1
    assert not ({r.id for r in first.items} & {r.id for r in second.items})


def test_completion_failure_does_not_publish_success_or_parent_links(
    setup_orders, owner, db, monkeypatch, caplog
):
    from pack_v2.modules.orders import repository

    orders, _, _, account, remote = setup_orders
    first, _ = execute(orders, owner, account, db)
    remote.postings.append(posting("59511155-0034-0", parent_posting_number=None))
    complete = repository.complete

    def failed(tx, run):
        complete(tx, run)
        raise RuntimeError(KEY)

    monkeypatch.setattr(orders.sync, "complete", failed)
    run, _ = execute(orders, owner, account, db)
    assert saved_run(db, run).status == "failed"
    assert orders.sync_status(owner)[0].last_success_at == saved_run(db, first).finished_at
    with db.transaction() as tx:
        assert all(s.parent_id is None for s in tx.scalars(select(Shipment)))
    assert KEY not in caplog.text


def test_status_history_records_only_confirmed_changes_without_polling_duplicates(setup_orders, owner, db):
    from pack_v2.infrastructure.models import AuditEvent

    orders, _, _, account, remote = setup_orders
    execute(orders, owner, account, db)
    execute(orders, owner, account, db)
    remote.postings[0].update(status="awaiting_deliver", substatus="awaiting_deliver")
    execute(orders, owner, account, db)
    with db.transaction() as tx:
        history = tx.scalars(
            select(AuditEvent)
            .where(AuditEvent.kind == "shipment_status_observed")
            .order_by(AuditEvent.created_at)
        ).all()
        assert [r.details["state"] for r in history] == ["awaiting_assembly", "awaiting_dispatch"]
        assert all(
            KEY not in json.dumps(r.details) and "synthetic-private" not in json.dumps(r.details)
            for r in history
        )


def test_queued_command_reads_window_current_at_execution_not_at_old_click(setup_orders, owner, db):
    orders, _, _, account, remote = setup_orders
    run = orders.request_sync(owner, account)[0]
    with db.transaction() as tx:
        saved = tx.get(OrderSync, run)
        saved.since -= timedelta(days=2)
        saved.until -= timedelta(days=2)
    before = datetime.now(UTC)
    orders.perform(Queue(db).claim())
    saved = saved_run(db, run)
    assert saved.status == "succeeded" and saved.until >= before
    assert saved.until - saved.since == timedelta(days=30)
