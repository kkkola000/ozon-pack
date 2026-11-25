from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import login
from pydantic import SecretStr
from sqlalchemy import select
from test_assembly import call
from test_instance_data import job, open_window, state, target
from test_orders import execute
from yandex_remote import BARCODE, GTD, ITEM, ORDER, YandexRemote, mark, offer, order

from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.models import AuditEvent
from pack_v2.integrations.contracts import YandexCredentials
from pack_v2.integrations.models import AccountSecret
from pack_v2.integrations.public import AccountSource, Connections, OutcomeUnknown, ReadError
from pack_v2.integrations.yandex.instances import normalized
from pack_v2.integrations.yandex.reader import YandexReadAdapter
from pack_v2.integrations.yandex.transport import YandexTransport
from pack_v2.modules.assembly.public import Assembly
from pack_v2.modules.catalog.models import Offer
from pack_v2.modules.catalog.public import Catalog
from pack_v2.modules.documents.public import Documents
from pack_v2.modules.instance_data.models import Submission
from pack_v2.modules.instance_data.public import InstanceData
from pack_v2.modules.instance_data.yandex_policy import STATUS_MESSAGES, SUBSTATUS_MESSAGES
from pack_v2.modules.marking.models import MarkingAssignment
from pack_v2.modules.marking.public import Marking
from pack_v2.modules.orders.public import Orders
from pack_v2.shared.errors import DomainError

SOURCE = AccountSource(uuid4(), "yandex", 1)
CREDS = YandexCredentials("123", SecretStr("synthetic-yandex-key"))


@pytest.fixture
def pipeline(db, settings, owner):
    remote = YandexRemote()
    box = SecretBox(settings.encryption_key.get_secret_value())
    connections = Connections(
        db,
        box,
        None,
        yandex_checker=remote.checker(),
        yandex_reader_factory=remote.reader,
        yandex_instance_factory=remote.instances,
    )
    account = connections.create(owner, "Яндекс", "123", CREDS.api_key, marketplace="yandex")
    catalog = Catalog(db, connections)
    orders = Orders(db, connections, catalog)
    data = InstanceData(db, connections, orders, Marking(box, settings.secret_key.get_secret_value()), box)
    documents = Documents(db, connections, orders, box)
    assembly = Assembly(
        db,
        connections,
        orders,
        box,
        settings.secret_key.get_secret_value(),
        instance_data=data,
        documents=documents,
    )
    execute(catalog, owner, account, db)
    execute(orders, owner, account, db)
    assert catalog.list_offers(owner).total == 1
    assert orders.list_shipments(owner).total == 1
    return assembly, data, remote, account


def fill(pipeline, owner, *, view=None, code=None):
    assembly = pipeline[0]
    view = view or open_window(pipeline, owner, ORDER)
    view = call(assembly, owner, view, "scan", BARCODE)["session"]
    gtd = call(assembly, owner, view, "gtd", UUID(target(view)), GTD)
    assert gtd["ok"], gtd
    scanned = call(assembly, owner, gtd["session"], "scan", code or mark())
    assert scanned["ok"], scanned
    return scanned["session"]


def settle(pipeline, owner, view):
    job(pipeline, due=True)
    return state(pipeline, owner, view)


def unit(view):
    return next(u for i in view["data"]["items"].values() for u in i["units"])


def test_reader_official_shapes_price_identity_and_privacy():
    remote = YandexRemote()
    with remote.reader(SOURCE, CREDS) as client:
        pages = list(client.iter_catalog())
        row = pages[0].items[0]
        assert row.source_id == "SYN-MARKED" and row.marking_declared
        assert {c.origin for c in row.barcodes} == {"yandex"}
        assert pages[-1].is_last and not pages[0].is_last
        shipment = client.get_shipment(ORDER)
        assert shipment.state == "awaiting_assembly" and shipment.items[0].unit_price == Decimal("2000.0000")
        assert shipment.items[0].optional_fields == ("cis",)
        assert ("item_id", ITEM) in [(r.kind, r.value) for r in shipment.items[0].product_references]
        assert "PRIVATE" not in repr(shipment)
        assert "seller-box-888" in repr(shipment)
    checks = remote.checker().check(CREDS)
    assert all(c.status == "passed" for c in checks)
    assert {c.code for c in checks} == {"orders", "products", "campaigns"}
    assert all(m in ("GET", "POST") for m, *_ in remote.calls)


def test_pipeline_full_write_status_readback_and_no_shipping(pipeline, owner, db):
    view = fill(pipeline, owner)
    assert view["marketplace"] == "yandex" and not view["shipping_supported"]
    assert not view["layout"]["editable"] and not view["data"]["can_verify"]
    assert not unit(view)["gtd_omission_allowed"]
    view = settle(pipeline, owner, view)
    assert view["data"]["status"] == "checking"
    pipeline[2].cis_status = "OK"
    view = settle(pipeline, owner, view)
    assert view["data"]["can_verify"] and unit(view)["cis_status"] == "accepted"
    assert unit(view)["market_cis_status"] == "OK"
    writes = [c for c in pipeline[2].calls if c[0] == "PUT"]
    assert len(writes) == 1
    packet = writes[0][2]
    assert packet == {
        "allowRemove": False,
        "boxes": [{"items": [{"id": int(ITEM), "fullCount": 1, "instances": [{"cis": mark(), "gtd": GTD}]}]}],
    }
    assert b"\\u001d" in writes[0][3] and b"\\\\u001d" not in writes[0][3]
    assert all("/ship" not in c[1] for c in pipeline[2].calls)
    assert call(pipeline[0], owner, view, "complete")["code"] == "unsupported"
    assert call(pipeline[0], owner, view, "verify")["ok"]
    with db.transaction() as tx:
        batch = tx.scalar(select(Submission))
        assert batch.status == "accepted" and batch.set_started_at
        assert b"91ABCD" not in batch.encrypted_payload


@pytest.mark.parametrize("status", ["FAILED", "IN_PROGRESS", "NOT_ON_VALIDATION", "OK", "INVALID"])
def test_cis_states_are_not_http_success(pipeline, owner, status):
    view = fill(pipeline, owner)
    settle(pipeline, owner, view)
    remote = pipeline[2]
    remote.cis_status = status
    if status == "INVALID":
        remote.substatus = "WRONG_OWNER_INN"
    view = settle(pipeline, owner, view)
    assert unit(view)["market_cis_status"] == status
    assert unit(view)["market_cis_message"] == (
        SUBSTATUS_MESSAGES["WRONG_OWNER_INN"] if status == "INVALID" else STATUS_MESSAGES[status]
    )
    assert view["data"]["can_verify"] == (status == "OK")
    assert unit(view)["cis_status"] == (
        "accepted" if status == "OK" else "rejected" if status in ("FAILED", "INVALID") else "saved"
    )


@pytest.mark.parametrize("applied", [True, False])
def test_ambiguous_put_reconciles_without_second_write(pipeline, owner, applied):
    view = fill(pipeline, owner)
    remote = pipeline[2]
    remote.timeout, remote.apply = True, applied
    settle(pipeline, owner, view)
    remote.timeout, remote.cis_status = None, "OK"
    for _ in range(3):
        view = settle(pipeline, owner, view)
        if view["data"]["can_verify"]:
            break
    assert view["data"]["can_verify"] is applied
    assert len([c for c in remote.calls if c[0] == "PUT"]) == 1


def test_ready_to_ship_cannot_write_but_keeps_local_history(pipeline, owner, db):
    pipeline[2].raw["substatus"] = "READY_TO_SHIP"
    view = fill(pipeline, owner)
    view = settle(pipeline, owner, view)
    assert view["data"]["status"] == "rejected"
    assert "Ожидает сборки" in view["data"]["message"]
    assert not any(c[0] == "PUT" for c in pipeline[2].calls)
    with db.transaction() as tx:
        assert (
            tx.scalar(
                select(AuditEvent).where(AuditEvent.kind == "yandex_instance_data_not_written")
            ).details["message"]
            == "Не записан ЧЗ на маркете"
        )
    assert pipeline[0].documents.view


def test_yandex_gtd_omission_rejected_and_no_network(pipeline, owner):
    view = open_window(pipeline, owner, ORDER)
    view = call(pipeline[0], owner, view, "scan", BARCODE)["session"]
    result = pipeline[0].gtd(
        owner,
        uuid4(),
        UUID(view["id"]),
        UUID(view["lease_token"]),
        view["revision"],
        UUID(target(view)),
        "",
        absent=True,
    )
    assert not result["ok"] and "номер ГТД" in result["message"]
    assert not any(c[0] == "PUT" for c in pipeline[2].calls)


@pytest.mark.parametrize(
    "full_readback,full_status,accepted", [(False, False, False), (False, True, True), (True, False, True)]
)
def test_short_cis_alone_never_proves_crypto_saved(pipeline, owner, full_readback, full_status, accepted):
    view = fill(pipeline, owner)
    settle(pipeline, owner, view)
    remote = pipeline[2]
    remote.cis_status, remote.status_full = "OK", full_status
    if not full_readback:
        # Preserve full value for status, remove it from the order and layout readback.
        old_handle = remote.handle

        def handle(request):
            response = old_handle(request)
            if request.url.path.endswith("/orders"):
                data = response.json()
                for order in data["orders"]:
                    for item in order["items"]:
                        for instance in item.get("instances", []):
                            instance["cis"] = instance["cis"].split("\x1d")[0]
                    for box in order["delivery"]["boxesLayout"]:
                        for item in box["items"]:
                            for instance in item.get("instances", []):
                                instance["cis"] = instance["cis"].split("\x1d")[0]
                return httpx.Response(200, json=data)
            return response

        remote.transport = httpx.MockTransport(handle)
    view = settle(pipeline, owner, view)
    assert view["data"]["can_verify"] is accepted


def test_gtd_needs_own_exact_readback_even_with_cis_ok(pipeline, owner):
    view = fill(pipeline, owner)
    settle(pipeline, owner, view)
    remote = pipeline[2]
    remote.cis_status = "OK"
    for item in remote.raw["items"]:
        for value in item["instances"]:
            value.pop("gtd")
    for box in remote.raw["delivery"]["boxesLayout"]:
        for item in box["items"]:
            for value in item["instances"]:
                value.pop("gtd")
    view = settle(pipeline, owner, view)
    # This is an external edit, not permission to silently overwrite or report GTD accepted.
    assert not view["data"]["can_verify"] and unit(view)["gtd_status"] != "accepted"
    assert len([c for c in remote.calls if c[0] == "PUT"]) == 1


def test_cancel_and_reopen_keeps_pending_write_reconcilable(pipeline, owner):
    view = fill(pipeline, owner)
    settle(pipeline, owner, view)
    assert call(pipeline[0], owner, state(pipeline, owner, view), "cancel")["ok"]
    pipeline[2].cis_status = "OK"
    # The old submission remains durable after cancelling only the local scanner window.
    settle(pipeline, owner, view)
    reopened = open_window(pipeline, owner, ORDER)
    assert unit(reopened)["cis_status"] != "rejected"
    settle(pipeline, owner, reopened)
    reopened = state(pipeline, owner, reopened)
    assert reopened["data"]["can_verify"]
    assert len([c for c in pipeline[2].calls if c[0] == "PUT"]) == 1


@pytest.mark.parametrize("substatus", sorted(SUBSTATUS_MESSAGES))
def test_all_official_invalid_reasons(substatus):
    from pack_v2.modules.instance_data import yandex_policy as yp

    raw = order()
    raw["items"][0]["instances"] = [{"cis": mark(), "gtd": GTD}]
    fresh = normalized(raw, SOURCE)
    payload = {"products": fresh.products}
    ready, errors, results = yp.checked(
        fresh,
        payload,
        {ITEM: {"gtd": True}},
        {(ITEM, str(fresh.products[0]["exemplars"][0]["exemplar_id"])): "unit"},
        {
            "items": [
                {"id": int(ITEM), "cis": [{"value": mark(), "status": "INVALID", "substatus": substatus}]}
            ]
        },
    )
    assert not ready and errors == {"unit": ["cis"]}
    assert results["unit"]["cis_message"] == SUBSTATUS_MESSAGES[substatus]


def test_full_layout_preserves_other_fields_and_two_parts_of_one_unit(pipeline, owner):
    remote = pipeline[2]
    remote.raw["delivery"]["boxesLayout"] = [
        {"boxId": i, "items": [{"id": int(ITEM), "partialCount": {"current": i, "total": 2}}]} for i in (1, 2)
    ]
    remote.raw["items"][0]["instances"] = [
        {"rnpt": "1234567890", "countryCode": "CN", "uin": "preserve-native"}
    ]
    # These are native values. The panel doesn't add a UIN input or manufacture a UIN.
    view = fill(pipeline, owner)
    settle(pipeline, owner, view)
    packet = next(c[2] for c in remote.calls if c[0] == "PUT")
    assert len(packet["boxes"]) == 2
    for i, box in enumerate(packet["boxes"], 1):
        assert box["items"][0]["partialCount"] == {"current": i, "total": 2}
        assert box["items"][0]["instances"] == [
            {"cis": mark(), "gtd": GTD, "rnpt": "1234567890", "countryCode": "CN", "uin": "preserve-native"}
        ]


def test_multiple_identical_partial_goods_are_blocked_without_guessing():
    raw = order()
    raw["items"][0]["count"] = 2
    raw["delivery"]["boxesLayout"] = [
        {"items": [{"id": int(ITEM), "partialCount": {"current": 1, "total": 2}}]}
    ]
    with pytest.raises(DomainError, match="сверка раскладки"):
        normalized(raw, SOURCE)


def test_catalog_pagination_archive_default_and_loop_guard():
    calls = []

    def handle(request):
        data = __import__("json").loads(request.content)
        calls.append((data, dict(request.url.params)))
        if data["archived"]:
            row = offer()
            row["offer"]["offerId"] = "archived-item"
            return httpx.Response(200, json={"status": "OK", "result": {"offerMappings": [row]}})
        return httpx.Response(200, json={"status": "OK", "result": {"offerMappings": [offer()]}})

    with YandexReadAdapter(SOURCE, CREDS, transport=httpx.MockTransport(handle)) as client:
        pages = list(client.iter_catalog())
    assert pages[1].items[0].archived and pages[1].is_last
    assert all(p["limit"] == "100" for _, p in calls)

    def repeat(request):
        return httpx.Response(200, json={"orders": [], "paging": {"nextPageToken": "same"}})

    with YandexReadAdapter(SOURCE, CREDS, transport=httpx.MockTransport(repeat)) as client:
        with pytest.raises(ReadError, match="страниц"):
            list(client.pages(client.http.orders_path, {}, limit=50, envelope=False))


@pytest.mark.parametrize("status", [200, 302, 403, 500])
def test_transport_single_mutation_attempt_and_no_unlisted_operations(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, content=b"bad JSON")

    transport = YandexTransport(CREDS, transport=httpx.MockTransport(handle), instances=True)
    try:
        expected = ReadError if status in (302, 403) else OutcomeUnknown
        with pytest.raises(expected):
            transport.once("PUT", f"/v2/campaigns/777/orders/{ORDER}/boxes", {"boxes": []}, write=True)
        assert len(calls) == 1
        with pytest.raises(ValueError):
            transport.once("PUT", f"/v2/campaigns/777/orders/{ORDER}/identifiers", {}, write=True)
        assert len(calls) == 1
    finally:
        transport.close()


def test_cabinet_encryption_identity_scope_and_pages(pipeline, owner, db, client):
    assembly, _, remote, account = pipeline
    connections = assembly.connections
    client.app.state.connections = connections
    client.app.state.catalog = assembly.orders.catalog
    client.app.state.orders = assembly.orders
    login(client)
    accounts = client.get("/api/v2/accounts").json()
    assert accounts[0]["business_id"] == "123" and accounts[0]["client_id"] is None
    assert "synthetic-yandex-key" not in repr(accounts)
    with db.transaction() as tx:
        assert all(b"synthetic-yandex-key" not in s.ciphertext for s in tx.scalars(select(AccountSecret)))
        assert tx.scalar(select(Offer)).marking_declared is True
    saved = connections.list_accounts(owner)[0]
    assert all(c["status"] == "passed" for c in connections.verify(owner, account, saved.revision))
    for page in ("/settings?tab=accounts", "/orders", "/products"):
        response = client.get(page)
        assert response.status_code == 200 and "Яндекс Маркет" in response.text
        assert "PRIVATE BUYER" not in response.text and "PRIVATE SECRET" not in response.text
    assert "Business ID" in client.get("/settings?tab=accounts").text
    with pytest.raises(DomainError, match="Business ID"):
        connections.create(owner, "Дубль", "00123", CREDS.api_key, marketplace="yandex")
    # Same external ID on a different provider is an independent cabinet.
    connections.create(owner, "Ozon", "123", SecretStr("synthetic-ozon-key"))
    assert len(connections.list_accounts(owner)) == 2
    source = next(s for s in connections.sync_accounts() if s.account_id == account)
    with pytest.raises(DomainError):
        with connections.shipping_client(source):
            pass
    with pytest.raises(DomainError):
        with connections.label_client(source):
            pass


def test_replacement_releases_old_only_after_exact_confirmation(pipeline, owner, db):
    view = fill(pipeline, owner)
    settle(pipeline, owner, view)
    remote = pipeline[2]
    remote.cis_status = "INVALID"
    remote.substatus = "INVALID_CRYPTO_TAIL"
    view = settle(pipeline, owner, view)
    result = call(pipeline[0], owner, view, "scan", mark('TEST"SERIAL02'))
    assert result["ok"], result
    view = result["session"]
    with db.transaction() as tx:
        old = tx.scalar(select(MarkingAssignment).where(MarkingAssignment.retired_at.is_not(None)))
        assert old.released_at is None
    settle(pipeline, owner, view)
    remote.cis_status, remote.substatus = "OK", None
    view = settle(pipeline, owner, view)
    assert view["data"]["can_verify"]
    with db.transaction() as tx:
        old = tx.scalar(select(MarkingAssignment).where(MarkingAssignment.retired_at.is_not(None)))
        assert old.released_at is not None and old.release_reason == "replacement_confirmed"


def test_same_gtin_different_serial_and_tail_cannot_be_accepted():
    from pack_v2.modules.instance_data import yandex_policy as yp

    raw = order()
    raw["items"][0]["instances"] = [{"cis": mark(), "gtd": GTD}]
    desired = normalized(raw, SOURCE)
    other = mark('TEST"SERIAL02')
    raw["items"][0]["instances"] = [{"cis": other, "gtd": GTD}]
    fresh = normalized(raw, SOURCE)
    ready, _, results = yp.checked(
        fresh,
        {"products": desired.products},
        {ITEM: {"gtd": True}},
        {(ITEM, str(fresh.products[0]["exemplars"][0]["exemplar_id"])): "unit"},
        {"items": [{"id": int(ITEM), "cis": [{"value": other, "status": "OK"}]}]},
    )
    assert not ready and results["unit"]["cis"] == "pending"
    # Even identical GTIN+serial with a different cryptographic tail is not full-code proof.
    wrong_tail = mark().replace("abcdef", "ABCDEF")
    raw["items"][0]["instances"] = [{"cis": wrong_tail, "gtd": GTD}]
    fresh = normalized(raw, SOURCE)
    ready, _, results = yp.checked(
        fresh,
        {"products": desired.products},
        {ITEM: {"gtd": True}},
        {(ITEM, str(fresh.products[0]["exemplars"][0]["exemplar_id"])): "unit"},
        {"items": [{"id": int(ITEM), "cis": [{"value": wrong_tail, "status": "OK"}]}]},
    )
    assert not ready and results["unit"]["cis"] == "pending"


def test_native_instance_order_does_not_change_local_unit_binding():
    from copy import deepcopy
    from types import SimpleNamespace as Obj

    from pack_v2.modules.instance_data import yandex_policy as yp

    first, second = mark(), mark('TEST"SERIAL02')
    raw = order()
    raw["items"][0]["count"] = 2
    raw["items"][0]["instances"] = [{"cis": second, "gtd": GTD}, {"cis": first, "gtd": GTD}]
    raw["delivery"]["boxesLayout"][0]["items"][0]["fullCount"] = 2
    snapshot = normalized(raw, SOURCE)
    value = {"products": deepcopy(snapshot.products), "native": deepcopy(snapshot.posting["yandex"])}
    units = [Obj(item_id="item", unit_id="one", ordinal=1), Obj(item_id="item", unit_id="two", ordinal=2)]
    item = Obj(references=[("item_id", ITEM)])
    # Only unit 2 is already known; the first unmarked unit must not steal its native code.
    yp.align_snapshot(value, units, {"item": item}, {}, {"two": Obj(raw=second)})
    rows = value["products"][0]["exemplars"]
    assert yp.mark_of(rows[0]) == first and yp.mark_of(rows[1]) == second


def test_partial_readback_waits_and_does_not_resubmit(pipeline, owner):
    view = fill(pipeline, owner)
    settle(pipeline, owner, view)
    remote = pipeline[2]
    remote.cis_status = "OK"
    original = remote.handle
    import json

    def delayed(request):
        response = original(request)
        if request.url.path.endswith("/orders"):
            value = response.json()
            for order in value["orders"]:
                for item in order["items"]:
                    for instance in item.get("instances", []):
                        instance.pop("cis", None)
                for box in order["delivery"]["boxesLayout"]:
                    for item in box["items"]:
                        for instance in item.get("instances", []):
                            instance.pop("cis", None)
            return httpx.Response(200, content=json.dumps(value))
        return response

    remote.transport = httpx.MockTransport(delayed)
    view = settle(pipeline, owner, view)
    assert view["data"]["status"] == "checking" and not view["data"]["can_verify"]
    remote.transport = httpx.MockTransport(original)
    view = settle(pipeline, owner, view)
    assert view["data"]["can_verify"]
    assert len([c for c in remote.calls if c[0] == "PUT"]) == 1


def test_partial_goods_merge_fields_and_reject_conflicting_codes():
    raw = order()
    raw["delivery"]["boxesLayout"] = [
        {
            "items": [
                {
                    "id": int(ITEM),
                    "partialCount": {"current": 1, "total": 2},
                    "instances": [{"rnpt": "preserved"}],
                }
            ]
        },
        {
            "items": [
                {
                    "id": int(ITEM),
                    "partialCount": {"current": 2, "total": 2},
                    "instances": [{"cis": mark(), "countryCode": "CN"}],
                }
            ]
        },
    ]
    snapshot = normalized(raw, SOURCE)
    value = snapshot.products[0]["exemplars"][0]
    assert (
        value["rnpt"] == "preserved" and value["countryCode"] == "CN" and value["marks"][0]["mark"] == mark()
    )
    raw["delivery"]["boxesLayout"][0]["items"][0]["instances"][0]["cis"] = mark('TEST"SERIAL02')
    with pytest.raises(DomainError, match="разные коды"):
        normalized(raw, SOURCE)


def test_external_box_edit_stops_before_put(pipeline, owner):
    view = fill(pipeline, owner)
    pipeline[2].raw["delivery"]["boxesLayout"] = [
        {"items": [{"id": int(ITEM), "partialCount": {"current": i, "total": 2}}]} for i in (1, 2)
    ]
    view = settle(pipeline, owner, view)
    assert view["data"]["status"] == "stale"
    assert not any(c[0] == "PUT" for c in pipeline[2].calls)


def test_full_packet_keeps_other_order_items(pipeline, owner):
    from copy import deepcopy

    remote = pipeline[2]
    card = offer()
    card["offer"].update(offerId="another-offer", markingDeclared=False, barcodes=["MRKTOTHER"])
    card["mapping"]["marketSku"] = 9876
    remote.additional_cards.append(card)
    execute(pipeline[0].orders.catalog, owner, pipeline[3], pipeline[0].db)
    other = deepcopy(remote.raw["items"][0])
    other.update(
        id=int(ITEM) + 1,
        offerId="another-offer",
        requiredInstanceTypes=[],
        instances=[{"countryCode": "CN", "rnpt": "unchanged"}],
    )
    remote.raw["items"].append(other)
    remote.raw["delivery"]["boxesLayout"][0]["items"].append({"id": other["id"], "fullCount": 1})
    view = fill(pipeline, owner)
    settle(pipeline, owner, view)
    payload = next(c[2] for c in remote.calls if c[0] == "PUT")
    assert payload["boxes"][0]["items"][1] == {
        "id": other["id"],
        "fullCount": 1,
        "instances": [{"countryCode": "CN", "rnpt": "unchanged"}],
    }


def test_box_codes_stay_in_boxes_when_native_item_array_is_permuted():
    from copy import deepcopy
    from types import SimpleNamespace as Obj

    from pack_v2.modules.instance_data import yandex_policy as yp

    first, second = mark(), mark('TEST"SERIAL02')
    raw = order()
    raw["items"][0]["count"] = 2
    raw["items"][0]["instances"] = [{"cis": first, "gtd": GTD}, {"cis": second, "gtd": GTD}]
    raw["delivery"]["boxesLayout"] = [
        {"items": [{"id": int(ITEM), "fullCount": 1, "instances": [{"cis": code, "gtd": GTD}]}]}
        for code in (second, first)
    ]
    snapshot = normalized(raw, SOURCE)
    native = snapshot.posting["yandex"]
    assert [b["items"][0]["slots"] for b in native["boxes"]] == [
        [int(snapshot.products[0]["exemplars"][1]["exemplar_id"])],
        [int(snapshot.products[0]["exemplars"][0]["exemplar_id"])],
    ]
    context = {"posting_number": ORDER, "products": snapshot.products, "native": native}
    details = [
        Obj(product_id=ITEM, exemplar_id=str(e["exemplar_id"]), unit_id=str(i), values={})
        for i, e in enumerate(snapshot.products[0]["exemplars"], 1)
    ]
    payload = yp.to_request(context, details, {})
    assert [b["items"][0]["instances"][0]["cis"] for b in payload["boxes"]] == [second, first]
    assert yp.fresh_matches(snapshot, context, payload)
    changed = deepcopy(raw)
    changed["delivery"]["boxesLayout"].reverse()
    assert not yp.fresh_matches(normalized(changed, SOURCE), context, payload)


@pytest.mark.parametrize("status", ["DELIVERY", "DELIVERED", "CANCELLED"])
def test_old_substatus_never_authorizes_write_after_status_change(pipeline, owner, status):
    view = fill(pipeline, owner)
    remote = pipeline[2]
    remote.raw["status"] = status
    # Even an inconsistent or lagging substatus must not reopen a delivered/cancelled order.
    assert normalized(remote.raw, SOURCE).posting["status"] == "unavailable"
    view = settle(pipeline, owner, view)
    assert not view["data"]["can_verify"]
    assert not any(c[0] == "PUT" for c in remote.calls)
