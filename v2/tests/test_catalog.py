import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import PASSWORD, login
from pydantic import SecretStr
from sqlalchemy import func, select

from pack_v2.cli import worker
from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.jobs import Queue
from pack_v2.infrastructure.models import Job
from pack_v2.integrations.ozon.access import OzonAccessChecker
from pack_v2.integrations.ozon.reader import OzonReadAdapter
from pack_v2.integrations.public import Connections
from pack_v2.modules.catalog.models import CatalogSync, Offer, OfferCode, OfferRef, Product, ProductCode
from pack_v2.modules.catalog.public import Catalog
from pack_v2.shared.errors import DomainError, Forbidden

KEY = "synthetic-catalog-secret"


class OzonMock:
    def __init__(self, db):
        self.db = db
        raw = json.loads((Path(__file__).parent / "fixtures/ozon/catalog.json").read_text())
        self.listed = raw["list"]["result"]["items"]
        self.details = raw["details"]["items"]
        self.calls, self.clients = [], []
        self.mode, self.callback = "ok", None

    def factory(self, source, credentials):
        client = OzonReadAdapter(source, credentials, transport=httpx.MockTransport(self.handle), attempts=1)
        self.clients.append(client)
        return client

    def handle(self, request):
        assert self.db.engine.pool.checkedout() == 0
        data = json.loads(request.content)
        self.calls.append((request.url.path, data, request.headers["client-id"]))
        if self.callback:
            callback, self.callback = self.callback, None
            callback(request, data)
        if self.mode == "denied":
            return httpx.Response(403, json={"message": KEY})
        if self.mode == "rate":
            return httpx.Response(429, headers={"retry-after": "120"}, json={"message": KEY})
        if request.url.path == "/v3/product/list":
            if data["last_id"]:
                if self.mode == "partial":
                    return httpx.Response(503)
                return httpx.Response(200, json={"result": {"items": []}})
            return httpx.Response(
                200,
                json={"result": {"items": self.listed, "last_id": "next", "total_items": len(self.listed)}},
            )
        assert request.url.path == "/v3/product/info/list"
        details = [r for r in self.details if str(r["id"]) in data["product_id"]]
        if self.mode == "missing":
            details = details[:1]
        return httpx.Response(200, json={"items": details})


@pytest.fixture
def setup_catalog(db, settings, owner):
    remote = OzonMock(db)
    connections = Connections(
        db, SecretBox(settings.encryption_key.get_secret_value()), OzonAccessChecker(), remote.factory
    )
    account = connections.create(owner, "ANEX", "000123", SecretStr(KEY))
    catalog = Catalog(db, connections)
    return catalog, connections, account, remote


def execute(catalog, owner, account, db):
    run_id = catalog.request_sync(owner, account)[0]
    job = Queue(db).claim(lease_seconds=120)
    assert job and job.kind == "catalog.sync"
    catalog.perform(job)
    return run_id, job


def records(db):
    with db.transaction() as tx:
        return list(tx.scalars(select(Offer).order_by(Offer.name)))


def run_record(db, run_id):
    with db.transaction() as tx:
        return tx.get(CatalogSync, run_id)


def test_request_is_atomic_deduplicated_and_does_not_call_ozon(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: catalog.request_sync(owner, account)[0], range(4)))
    assert len(set(ids)) == 1 and remote.calls == []
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(Job)) == 1
        assert tx.scalar(select(func.count()).select_from(CatalogSync)) == 1
        job = tx.scalar(select(Job))
        assert job.payload == {"run_id": str(ids[0])} and KEY not in repr(job.payload)


def test_full_read_saves_all_codes_and_scoped_refs_and_finishes_atomically(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    run_id, job = execute(catalog, owner, account, db)
    result = catalog.list_offers(owner)
    assert result.total == 2 and result.pages == 1
    row = next(r for r in result.items if r.article == "PF-M02")
    assert row.codes == ("04660291070982", "OZN634662543")
    assert ("sku", "6220834780") in row.references and ("product_id", "101") in row.references
    assert row.archived is False and row.source_present
    run = run_record(db, run_id)
    assert (run.status, run.pages, run.items) == ("succeeded", 2, 2)
    with db.transaction() as tx:
        assert tx.get(Job, job.id).status == "succeeded"
        assert tx.scalar(select(func.count()).select_from(Product)) == 0
        assert all(r.last_seen_run_id == run_id for r in tx.scalars(select(Offer)))
    assert catalog.sync_status(owner)[0].last_success_at == run.finished_at
    assert all(c.http.client.is_closed for c in remote.clients)


def test_repeat_updates_same_ids_without_duplicate_rows_or_revision_churn(setup_catalog, owner, db):
    catalog, _, account, _ = setup_catalog
    execute(catalog, owner, account, db)
    first = {r.id: r.revision for r in records(db)}
    execute(catalog, owner, account, db)
    assert {r.id: r.revision for r in records(db)} == first
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(OfferCode)) == 3
        assert tx.scalar(select(func.count()).select_from(OfferRef).where(OfferRef.kind == "product_id")) == 2


def test_account_isolation_same_remote_ids_and_codes_are_independent(setup_catalog, owner, db):
    catalog, connections, first, remote = setup_catalog
    second = connections.create(owner, "Другой", "456", SecretStr("second-key"))
    execute(catalog, owner, first, db)
    execute(catalog, owner, second, db)
    assert catalog.list_offers(owner).total == 4
    assert catalog.list_offers(owner, account_id=first).total == 2
    assert catalog.list_offers(owner, account_id=second).total == 2
    assert {row[2] for row in remote.calls} == {"000123", "456"}
    assert len({r.id for r in records(db)}) == 4


def test_partial_pass_preserves_unseen_card_and_last_success_until_complete(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    first, _ = execute(catalog, owner, account, db)
    remote.listed = remote.listed[:1]
    remote.mode = "partial"
    second, job = execute(catalog, owner, account, db)
    assert run_record(db, second).status == "retrying"
    assert all(r.source_present for r in records(db))
    assert catalog.sync_status(owner)[0].last_success_at == run_record(db, first).finished_at
    with db.transaction() as tx:
        saved = tx.get(Job, job.id)
        assert saved.status == "pending"
        saved.available_at = datetime.now(UTC) - timedelta(seconds=1)
    remote.mode = "ok"
    retry = Queue(db).claim()
    catalog.perform(retry)
    assert run_record(db, second).status == "succeeded"
    assert catalog.list_offers(owner, state="missing").total == 1
    assert catalog.list_offers(owner, query="OZN2113641643").total == 1
    assert catalog.list_offers(owner).total == 2


def test_incomplete_page_not_published_as_complete_and_no_fake_success_time(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    remote.mode = "missing"
    run, _ = execute(catalog, owner, account, db)
    assert records(db) == [] and run_record(db, run).status == "retrying"
    assert catalog.sync_status(owner)[0].last_success_at is None


def test_empty_full_catalog_marks_missing_without_archiving_or_deleting(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    execute(catalog, owner, account, db)
    before = {r.id: r.archived for r in records(db)}
    remote.listed = []
    run, _ = execute(catalog, owner, account, db)
    assert run_record(db, run).status == "succeeded"
    assert {r.id: r.archived for r in records(db)} == before
    assert all(not r.source_present for r in records(db))


def test_missing_detail_fields_do_not_erase_previous_values(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    execute(catalog, owner, account, db)
    previous = {r.article: r for r in catalog.list_offers(owner).items}
    for row in remote.details:
        for key in ("barcodes", "name", "primary_image", "is_archived"):
            row.pop(key, None)
    for row in remote.listed:
        row.pop("archived", None)
    execute(catalog, owner, account, db)
    for row in catalog.list_offers(owner).items:
        before = previous[row.article]
        assert (row.name, row.codes, row.archived, row.image_url) == (
            before.name,
            before.codes,
            before.archived,
            before.image_url,
        )
        assert row.revision == before.revision


def test_marketplace_changes_update_barcodes_but_preserve_main_card_and_manual_codes(
    setup_catalog, owner, db
):
    catalog, _, account, remote = setup_catalog
    execute(catalog, owner, account, db)
    offer = next(r for r in catalog.list_offers(owner).items if r.article == "PF-M02")
    with db.transaction() as tx:
        product = Product(name="Основной товар", article="Мой артикул")
        tx.add(product)
        tx.flush()
        product_id = product.id
        tx.get(Offer, offer.id).product_id = product_id
        tx.add(ProductCode(product_id=product_id, kind="other", value="my-main-code", origin="manual"))
        tx.add(OfferCode(offer_id=offer.id, kind="other", value="manual-code", origin="manual"))
    detail = next(r for r in remote.details if r["id"] == 101)
    detail["barcodes"] = ["OZN-new"]
    detail["name"] = "Название с площадки"
    execute(catalog, owner, account, db)
    after = next(r for r in catalog.list_offers(owner).items if r.id == offer.id)
    assert after.codes == ("OZN-new", "manual-code") and after.name == "Название с площадки"
    with db.transaction() as tx:
        assert tx.get(Offer, offer.id).product_id == product_id
        assert tx.get(Product, product_id).name == "Основной товар"
        assert (
            tx.scalar(select(ProductCode.value).where(ProductCode.product_id == product_id)) == "my-main-code"
        )


def test_duplicate_authoritative_reference_rolls_back_whole_page(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    for row in remote.details:
        row["sku"] = 999
        row["sources"] = []
    run, _ = execute(catalog, owner, account, db)
    assert run_record(db, run).status == "failed" and records(db) == []


def test_rotated_credentials_during_http_cannot_commit_old_results(setup_catalog, owner, db):
    catalog, connections, account, remote = setup_catalog
    remote.callback = lambda request, data: connections.update(
        owner, account, 0, "ANEX", SecretStr("new-key")
    )
    run, _ = execute(catalog, owner, account, db)
    assert records(db) == [] and run_record(db, run).status == "stale"
    new_run, _ = execute(catalog, owner, account, db)
    assert run_record(db, new_run).credential_revision == 2
    assert run_record(db, new_run).status == "succeeded"


def test_disabled_account_stops_read_and_keeps_history(setup_catalog, owner, db):
    catalog, connections, account, remote = setup_catalog
    remote.callback = lambda request, data: connections.set_active(owner, account, 0, False)
    run, _ = execute(catalog, owner, account, db)
    assert run_record(db, run).status == "stale" and records(db) == []
    with pytest.raises(DomainError):
        catalog.request_sync(owner, account)


def test_expired_worker_cannot_write_or_finish_reclaimed_job(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    newer = []
    run_id = catalog.request_sync(owner, account)[0]
    old = Queue(db).claim()

    def reclaim(request, data):
        with db.transaction() as tx:
            tx.get(Job, old.id).lease_until = datetime.now(UTC) - timedelta(seconds=1)
        newer.append(Queue(db).claim())

    remote.callback = reclaim
    catalog.perform(old)
    assert records(db) == [] and run_record(db, run_id).status == "running"
    catalog.perform(newer[0])
    assert len(records(db)) == 2 and run_record(db, run_id).status == "succeeded"
    assert not Queue(db).finish(old)


def test_rate_limit_delay_survives_retry_exhaustion_and_scheduler_respects_it(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    remote.mode = "rate"
    run_id = catalog.request_sync(owner, account)[0]
    with db.transaction() as tx:
        tx.get(Job, tx.get(CatalogSync, run_id).job_id).max_attempts = 1
    job = Queue(db).claim()
    catalog.perform(job)
    run = run_record(db, run_id)
    assert run.status == "failed" and run.retry_at > datetime.now(UTC) + timedelta(seconds=110)
    with pytest.raises(DomainError, match="позже"):
        catalog.request_sync(owner, account)
    catalog.schedule(15)
    assert len(catalog.sync_status(owner)) == 1
    assert KEY not in repr(catalog.sync_status(owner))


def test_failed_last_lease_recoverable_and_not_displayed_as_still_running(setup_catalog, owner, db):
    catalog, _, account, _ = setup_catalog
    run_id = catalog.request_sync(owner, account)[0]
    job = Queue(db).claim()
    with db.transaction() as tx:
        saved = tx.get(Job, job.id)
        saved.max_attempts = 1
        saved.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        tx.get(CatalogSync, run_id).status = "running"
    assert catalog.sync_status(owner)[0].status == "failed"
    new_run = catalog.request_sync(owner, account)[0]
    assert new_run != run_id and run_record(db, run_id).status == "failed"


def test_catalog_worker_dispatches_read_job_and_scheduler_does_not_duplicate(
    setup_catalog, owner, db, identity
):
    catalog, _, account, _ = setup_catalog
    run_id = catalog.request_sync(owner, account)[0]
    worker(db, identity, once=True, catalog=catalog, catalog_minutes=15)
    assert run_record(db, run_id).status == "succeeded"
    catalog.schedule(15)
    catalog.schedule(0)
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(CatalogSync)) == 1
        tx.get(CatalogSync, run_id).created_at -= timedelta(minutes=16)
    catalog.schedule(15)
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(CatalogSync)) == 2


@pytest.mark.parametrize(
    "query,total",
    [
        ("Тестовые", 1),
        ("PF-M02", 1),
        ("634662543", 1),
        ("101", 1),
        ("04660291070982", 1),
        ("OZN634662543", 1),
        ("4660291070982", 0),
        ("%", 0),
        ("_", 0),
    ],
)
def test_search_and_leading_zeros(setup_catalog, owner, db, query, total):
    catalog, _, account, _ = setup_catalog
    execute(catalog, owner, account, db)
    assert catalog.list_offers(owner, query=query).total == total


def test_unknown_fields_new_card_not_falsely_active(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    remote.listed = [{"product_id": 101}]
    remote.details = [{"id": 101}]
    execute(catalog, owner, account, db)
    item = catalog.list_offers(owner).items[0]
    assert item.archived is None and catalog.list_offers(owner, state="active").total == 0


def test_ui_and_api_read_only_database_and_queue_does_not_do_http(setup_catalog, owner, db, client):
    catalog, connections, account, remote = setup_catalog
    client.app.state.catalog, client.app.state.connections = catalog, connections
    login(client)
    execute(catalog, owner, account, db)
    calls = len(remote.calls)
    response = client.get("/products?account_id=&state=all&q=04660291070982")
    assert response.status_code == 200 and "Тестовые муфты" in response.text
    assert "OZN634662543" in response.text and KEY not in response.text
    assert client.get("/api/v2/products").json()["total"] == 2
    assert len(remote.calls) == calls
    assert client.post("/api/v2/catalog/sync", json={}).status_code == 403
    csrf = client.get("/api/v2/me").json()["csrf"]
    queued = client.post(
        "/api/v2/catalog/sync", json={"account_id": str(account)}, headers={"x-csrf-token": csrf}
    )
    assert queued.status_code == 202 and len(queued.json()["run_ids"]) == 1
    assert len(remote.calls) == calls
    assert client.post("/products/sync", data={"csrf": csrf, "account_id": ""}).status_code == 303
    assert client.post("/products/sync", data={"csrf": csrf, "account_id": "bad"}).status_code == 422
    assert client.get("/products?page=0").status_code == 422
    assert client.get("/products?state=anything").status_code == 422
    assert client.get("/api/v2/catalog/sync").status_code == 200


def test_worker_with_products_permission_can_read_but_cannot_schedule(setup_catalog, owner, identity, client):
    catalog, connections, account, _ = setup_catalog
    packer = identity.create_user(owner, "catalog-reader", PASSWORD, "packer", frozenset({"products"}))
    client.app.state.catalog, client.app.state.connections = catalog, connections
    login(client, "catalog-reader")
    assert client.get("/products").status_code == 200
    assert client.get("/api/v2/products").status_code == 200
    assert client.get("/api/v2/catalog/sync").status_code == 200
    with pytest.raises(Forbidden):
        catalog.request_sync(packer, account)
    csrf = client.get("/api/v2/me").json()["csrf"]
    assert client.post("/api/v2/catalog/sync", json={}, headers={"x-csrf-token": csrf}).status_code == 403
    assert client.get("/api/v2/accounts").status_code == 403


def test_catalog_requires_section_for_read_and_login_for_api(setup_catalog, owner, identity, client):
    catalog, connections, _, _ = setup_catalog
    client.app.state.catalog, client.app.state.connections = catalog, connections
    assert client.get("/api/v2/products").status_code == 401
    identity.create_user(owner, "no-catalog", PASSWORD, "packer", frozenset({"pack"}))
    login(client, "no-catalog")
    assert client.get("/products").status_code == 403
    assert client.get("/api/v2/catalog/sync").status_code == 403


def test_html_escapes_external_product_names(setup_catalog, owner, db, client):
    catalog, connections, account, remote = setup_catalog
    remote.details[0]["name"] = '<script>alert("test")</script>'
    execute(catalog, owner, account, db)
    client.app.state.catalog, client.app.state.connections = catalog, connections
    login(client)
    html = client.get("/products").text
    assert "<script>alert(" not in html and "&lt;script&gt;" in html


def test_pagination_is_bounded_and_does_not_repeat_rows(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    remote.listed = [{"product_id": number} for number in range(1000, 1031)]
    remote.details = [
        {"id": number, "name": "Товар " + str(number), "barcodes": []} for number in range(1000, 1031)
    ]
    execute(catalog, owner, account, db)
    first, second = catalog.list_offers(owner), catalog.list_offers(owner, page=2)
    assert first.total == 31 and first.pages == 2 and len(first.items) == 30 and len(second.items) == 1
    assert not ({row.id for row in first.items} & {row.id for row in second.items})


def test_completion_rollback_does_not_publish_missing_flags_or_false_success(
    setup_catalog, owner, db, monkeypatch, caplog
):
    from pack_v2.modules.catalog import repository

    catalog, _, account, remote = setup_catalog
    first, _ = execute(catalog, owner, account, db)
    remote.listed = remote.listed[:1]
    real_complete = repository.complete

    def fail_after_marking(tx, account_id, run_id):
        real_complete(tx, account_id, run_id)
        raise RuntimeError(KEY)

    monkeypatch.setattr(repository, "complete", fail_after_marking)
    failed, job = execute(catalog, owner, account, db)
    assert run_record(db, failed).status == "failed"
    assert all(row.source_present for row in records(db))
    assert catalog.sync_status(owner)[0].last_success_at == run_record(db, first).finished_at
    assert KEY not in caplog.text and KEY not in repr(catalog.sync_status(owner))
    with db.transaction() as tx:
        assert tx.get(Job, job.id).status == "failed"


def test_manual_code_with_same_value_is_preserved_without_revision_churn(setup_catalog, owner, db):
    catalog, _, account, _ = setup_catalog
    execute(catalog, owner, account, db)
    with db.transaction() as tx:
        code = tx.scalar(select(OfferCode).where(OfferCode.value == "OZN634662543"))
        code.origin = "manual"
    revisions = {r.id: r.revision for r in records(db)}
    execute(catalog, owner, account, db)
    assert {r.id: r.revision for r in records(db)} == revisions
    with db.transaction() as tx:
        assert tx.scalar(select(OfferCode.origin).where(OfferCode.value == "OZN634662543")) == "manual"


def test_settings_admin_without_products_can_check_sync_status_but_cannot_read_catalog(
    setup_catalog, owner, identity, client
):
    catalog, connections, _, _ = setup_catalog
    identity.create_user(owner, "settings-only", PASSWORD, "admin", frozenset())
    client.app.state.catalog, client.app.state.connections = catalog, connections
    login(client, "settings-only")
    assert client.get("/api/v2/catalog/sync").status_code == 200
    assert client.get("/products").status_code == 403
    assert client.get("/settings?tab=accounts").status_code == 200


def test_article_rename_replaces_current_reference_without_changing_card_identity(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    execute(catalog, owner, account, db)
    before = next(r for r in catalog.list_offers(owner).items if r.article == "PF-M02")
    next(row for row in remote.details if row["id"] == 101)["offer_id"] = "NEW-ARTICLE"
    execute(catalog, owner, account, db)
    after = next(r for r in catalog.list_offers(owner).items if r.id == before.id)
    assert after.article == "NEW-ARTICLE"
    assert ("offer_id", "PF-M02") not in after.references and ("offer_id", "NEW-ARTICLE") in after.references


def test_database_rejects_cross_account_last_seen_run(setup_catalog, owner, db):
    from sqlalchemy.exc import IntegrityError

    catalog, connections, first, _ = setup_catalog
    run, _ = execute(catalog, owner, first, db)
    second = connections.create(owner, "Чужой кабинет", "789", SecretStr("another-key"))
    with pytest.raises(IntegrityError), db.transaction() as tx:
        tx.add(Offer(account_id=second, name="wrong reference", last_seen_run_id=run))


def test_retry_after_is_kept_on_pending_job_and_duplicate_click_returns_same_run(setup_catalog, owner, db):
    catalog, _, account, remote = setup_catalog
    remote.mode = "rate"
    run, job = execute(catalog, owner, account, db)
    assert run_record(db, run).status == "retrying"
    with db.transaction() as tx:
        assert tx.get(Job, job.id).available_at > datetime.now(UTC) + timedelta(seconds=110)
    assert catalog.request_sync(owner, account) == [run]
    assert Queue(db).claim() is None
