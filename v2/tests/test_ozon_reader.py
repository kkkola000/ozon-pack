import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from pack_v2.integrations.contracts import OzonCredentials
from pack_v2.integrations.ozon import normalize
from pack_v2.integrations.ozon.reader import OzonReadAdapter
from pack_v2.integrations.public import AccountSource, ReadError

NOW = datetime(2026, 11, 21, tzinfo=UTC)
SOURCE = AccountSource(uuid4(), "ozon", 1)
CREDS = OzonCredentials("000123", SecretStr("synthetic-secret"))
FIXTURES = Path(__file__).parent / "fixtures" / "ozon"


def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


def adapter(handle, **kwargs):
    return OzonReadAdapter(
        SOURCE, CREDS, transport=httpx.MockTransport(handle), clock=lambda: NOW, wait=lambda _: None, **kwargs
    )


def posting(**changes):
    raw = fixture("posting")
    raw.update(changes)
    return raw


def refs(rows):
    return {(r.kind, r.value) for r in rows}


def test_catalog_lazy_pages_details_joined_by_product_id_not_response_order():
    raw, calls = fixture("catalog"), []

    def handle(request):
        data = json.loads(request.content)
        calls.append((request.url.path, data))
        if request.url.path == "/v3/product/info/list":
            assert data["product_id"] == ["101", "102"]
            return httpx.Response(200, json=raw["details"])
        assert data["filter"] == {"visibility": "ALL"}
        return httpx.Response(200, json=raw["last"] if data["last_id"] else raw["list"])

    with adapter(handle) as reader:
        pages = reader.iter_catalog()
        assert not calls
        page = next(pages)
        assert len(calls) == 2
        assert [i.source_id for i in page.items] == ["101", "102"]
        assert page.total_hint == 2 and page.next_cursor == "catalog-next" and not page.is_last
        item = page.items[0]
        assert item.article == "PF-M02" and item.source == SOURCE and item.observed_at == NOW
        assert refs(item.references) >= {("product_id", "101"), ("sku", "634662543"), ("sku", "6220834780")}
        assert [(c.value, c.kind) for c in item.barcodes] == [
            ("04660291070982", "gtin"),
            ("OZN634662543", "marketplace"),
        ]
        assert item.image_url == "https://cdn.example.invalid/101.png"
        assert page.items[1].archived is True
        final = next(pages)
        assert final.is_last and final.next_cursor is None and final.items == ()
        assert list(pages) == []
    assert reader.http.client.is_closed
    with pytest.raises(FrozenInstanceError):
        item.article = "changed"


def test_detail_chunks_bounded_and_no_total_based_truncation():
    chunks = []

    def handle(request):
        data = json.loads(request.content)
        if request.url.path.endswith("/info/list"):
            chunks.append(data["product_id"])
            return httpx.Response(200, json={"items": [{"id": int(i)} for i in reversed(data["product_id"])]})
        if data["last_id"]:
            return httpx.Response(200, json={"result": {"items": []}})
        return httpx.Response(
            200,
            json={
                "result": {"items": [{"product_id": i} for i in range(1, 206)], "last_id": "next", "total": 1}
            },
        )

    with adapter(handle) as reader:
        pages = list(reader.iter_catalog())
    assert len(pages[0].items) == 205 and pages[-1].is_last
    assert list(map(len, chunks)) == [100, 100, 5]
    assert pages[0].total_hint == 1


@pytest.mark.parametrize("details", [[], [{"id": 101}, {"id": 101}], [{"id": 101}, {"id": 999}]])
def test_incomplete_duplicate_or_unexpected_details_never_emit_catalog_page(details):
    raw = fixture("catalog")

    def handle(request):
        return httpx.Response(
            200, json={"items": details} if request.url.path.endswith("/info/list") else raw["list"]
        )

    with adapter(handle) as reader:
        with pytest.raises(ReadError) as error:
            next(reader.iter_catalog())
    assert error.value.kind == "incomplete"


@pytest.mark.parametrize("dataset", ["catalog", "shipments"])
def test_repeated_cursor_and_duplicate_ids_are_not_silent_completion(dataset):
    def handle(request):
        if request.url.path.endswith("/info/list"):
            return httpx.Response(200, json={"items": [{"id": 101}]})
        if dataset == "catalog":
            return httpx.Response(200, json={"result": {"items": [{"product_id": 101}], "last_id": "same"}})
        return httpx.Response(200, json={"postings": [posting()], "has_next": True, "cursor": "same"})

    with adapter(handle) as reader:
        pages = reader.iter_catalog() if dataset == "catalog" else reader.iter_shipments(NOW, NOW)
        assert not next(pages).is_last
        with pytest.raises(ReadError) as error:
            next(pages)
    assert error.value.kind == "pagination"


def test_max_pages_stops_incomplete_pass_instead_of_marking_complete():
    def handle(request):
        if request.url.path.endswith("/info/list"):
            return httpx.Response(200, json={"items": [{"id": 101}]})
        return httpx.Response(200, json={"items": [{"product_id": 101}], "last_id": "next"})

    with adapter(handle, max_pages=1) as reader:
        pages = reader.iter_catalog()
        assert not next(pages).is_last
        with pytest.raises(ReadError) as error:
            next(pages)
    assert error.value.kind == "pagination"


def test_shipments_paging_filters_timezone_aliases_and_exact_money():
    calls = []

    def handle(request):
        data = json.loads(request.content)
        calls.append(data)
        return httpx.Response(
            200,
            json={"postings": [], "has_next": False}
            if data["cursor"]
            else {"postings": [posting()], "has_next": True, "cursor": "opaque-next"},
        )

    local = NOW.astimezone(timezone(timedelta(hours=3)))
    with adapter(handle) as reader:
        pages = list(reader.iter_shipments(local, local + timedelta(days=1)))
    assert calls[0]["filter"] == {
        "since": "2026-11-21T00:00:00.000Z",
        "to": "2026-11-22T00:00:00.000Z",
        "statuses": ["awaiting_deliver", "awaiting_packaging"],
    }
    assert calls[0]["limit"] == 100 and calls[1]["cursor"] == "opaque-next"
    assert calls[0]["with"]["financial_data"] is False
    result = pages[0].items[0]
    assert refs(result.references) >= {("label_barcode", "ii50082908916"), ("tracking_number", "00054321")}
    assert refs(result.order_references) == {("order_id", "59511155"), ("order_number", "59511155-0034")}
    assert result.parent_reference.value == "59511155-0034-0"
    assert result.package_count_hint == 2 and len(result.related_references) == 2
    assert result.state == "awaiting_assembly" and not result.transition_failed
    assert result.deadline == datetime(2026, 11, 22, 9, tzinfo=UTC)
    line = next(i for i in result.items if i.source_line_id == "sku:634662543")
    assert line.unit_price == Decimal("1234.50") and line.currency == "RUB"
    assert line.required_fields == ("gtd",) and line.optional_fields == ("cis",)
    assert not result.requirements_complete
    rendered = repr(asdict(result))
    assert "synthetic-private" not in rendered and "synthetic-secret" not in rendered


@pytest.mark.parametrize(
    "changes",
    [
        {"has_next": True, "cursor": ""},
        {"has_next": True, "cursor": "n", "postings": []},
        {"has_next": "true"},
        {"cursor": "n"},
    ],
)
def test_incomplete_fbs_pagination_not_success(changes):
    data = {"postings": [posting()]}
    data.update(changes)
    with adapter(lambda r: httpx.Response(200, json=data)) as reader:
        with pytest.raises(ReadError):
            list(reader.iter_shipments(NOW, NOW))


@pytest.mark.parametrize(
    "since,until,states",
    [
        (NOW.replace(tzinfo=None), NOW, None),
        (NOW, NOW - timedelta(days=1), None),
        (NOW, NOW, ()),
        (NOW, NOW, ("unknown",)),
    ],
)
def test_invalid_filter_never_sends_request(since, until, states):
    def handle(request):
        pytest.fail("Invalid filter must be rejected locally")

    with adapter(handle) as reader:
        with pytest.raises(ValueError):
            list(reader.iter_shipments(since, until, states=states))


def test_all_statuses_is_explicit_and_not_default():
    def handle(request):
        assert "statuses" not in json.loads(request.content)["filter"]
        return httpx.Response(200, json={"postings": [], "has_next": False})

    with adapter(handle) as reader:
        assert list(reader.iter_shipments(NOW, NOW, states=None))[0].is_last


@pytest.mark.parametrize(
    "method,arg,path",
    [
        ("get_shipment", "59511155-0034-1", "/v3/posting/fbs/get"),
        ("find_shipment_by_barcode", "ii50082908916", "/v2/posting/fbs/get-by-barcode"),
    ],
)
def test_direct_reads_and_ii_lookup_use_declared_endpoints(method, arg, path):
    def handle(request):
        assert request.url.path == path
        data = json.loads(request.content)
        if method == "get_shipment":
            assert data["posting_number"] == arg and data["with"]["product_exemplars"] is False
        else:
            assert data == {"barcode": arg}
        return httpx.Response(200, json={"result": posting(status="awaiting_deliver")})

    with adapter(handle) as reader:
        assert getattr(reader, method)(arg).state == "awaiting_dispatch"


@pytest.mark.parametrize("method", ["get_shipment", "find_shipment_by_barcode"])
def test_only_404_is_no_match_other_errors_remain_visible(method):
    for status in (404, 400, 403):
        with adapter(lambda r, status=status: httpx.Response(status)) as reader:
            if status == 404:
                assert getattr(reader, method)("59511155-0034-1") is None
            else:
                with pytest.raises(ReadError) as error:
                    getattr(reader, method)("59511155-0034-1")
                assert error.value.status == status


def test_wrong_posting_returned_cannot_be_assigned_to_requested_order():
    with adapter(
        lambda r: httpx.Response(200, json={"result": posting(posting_number="different")})
    ) as reader:
        with pytest.raises(ReadError):
            reader.get_shipment("59511155-0034-1")


def test_reduced_barcode_response_does_not_invent_parent_boxes_or_requirements():
    raw = {
        "posting_number": "A-0001-1",
        "status": "awaiting_deliver",
        "products": [{"sku": 101, "quantity": 1}],
    }
    with adapter(lambda r: httpx.Response(200, json={"result": raw})) as reader:
        result = reader.find_shipment_by_barcode("ii00000001")
    assert result.parent_reference is None and result.order_references == ()
    assert result.package_count_hint is None and not result.requirements_complete
    assert "requirements" not in result.fields_present and "label_codes" not in result.fields_present
    assert refs(result.references) == {("posting_number", "A-0001-1")}


@pytest.mark.parametrize(
    "status,substatus,state,failed",
    [
        ("awaiting_deliver", "ship_failed", "unknown", True),
        ("delivered", None, "delivered", False),
        ("cancelled", None, "cancelled", False),
        ("new_status", None, "unknown", False),
    ],
)
def test_unknown_status_and_failed_transition_cannot_be_declared_ready(status, substatus, state, failed):
    result = normalize.shipment(SOURCE, posting(status=status, substatus=substatus), NOW)
    assert (result.state, result.transition_failed) == (state, failed)
    assert result.source_status == status and result.source_substatus == substatus


def test_positive_requirement_hints_preserved_but_not_complete_and_no_inference_from_old_marks():
    raw = posting(
        requirements={
            "products_requiring_mandatory_mark": [634662543],
            "products_requiring_rnpt": [634662543],
        },
        optional={},
    )
    result = normalize.shipment(SOURCE, raw, NOW)
    line = next(i for i in result.items if i.source_line_id == "sku:634662543")
    assert line.required_fields == ("cis", "rnpt") and not result.requirements_complete
    raw["requirements"] = {}
    result = normalize.shipment(SOURCE, raw, NOW)
    assert all(not i.required_fields for i in result.items) and not result.requirements_complete


@pytest.mark.parametrize("quantity", [0, -1, True, "1.5", 2**32])
def test_invalid_quantity_cannot_make_assembly_complete(quantity):
    raw = posting()
    raw["products"][0]["quantity"] = quantity
    with pytest.raises(ReadError):
        normalize.shipment(SOURCE, raw, NOW)


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-0.01", True, 1.2])
def test_invalid_money_rejected_without_rounding(price):
    raw = posting()
    raw["products"][0]["price"] = price
    with pytest.raises(ReadError):
        normalize.shipment(SOURCE, raw, NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("shipment_date", "2026-11-22T12:00:00"),
        ("products", []),
        ("multi_box_qty", False),
        ("scanit", {"unexpected": "value"}),
    ],
)
def test_malformed_shipment_not_accepted(field, value):
    with pytest.raises(ReadError):
        normalize.shipment(SOURCE, posting(**{field: value}), NOW)


def test_duplicate_sku_cannot_double_count_units():
    raw = posting()
    raw["products"].append(deepcopy(raw["products"][0]))
    with pytest.raises(ReadError):
        normalize.shipment(SOURCE, raw, NOW)


def test_unknown_offer_fields_and_opaque_codes_not_replaced_with_defaults():
    result = normalize.offer(SOURCE, {"product_id": 101, "offer_id": "  000abc  "}, {"id": 101}, NOW)
    assert result.archived is None and "archived" not in result.fields_present
    assert result.barcodes == () and "barcodes" not in result.fields_present
    assert result.article == "  000abc  " and "article" in result.fields_present
    detail = {"id": 101, "name": "Тестовый\n товар", "barcodes": ["04660291070982", "ozn00123", "own-code"]}
    result = normalize.offer(SOURCE, {"product_id": 101}, detail, NOW)
    assert result.name == "Тестовый товар"
    assert [c.kind for c in result.barcodes] == ["gtin", "other", "marketplace"]


@pytest.mark.parametrize(
    "changes",
    [
        {"barcodes": [123]},
        {"sources": {}},
        {"primary_image": ["javascript:alert(1)"]},
        {"primary_image": "https://user:pass@example.invalid/x"},
        {"primary_image": "https://[broken/"},
        {"is_archived": 1},
    ],
)
def test_malformed_catalog_detail_never_produces_snapshot(changes):
    with pytest.raises(ReadError):
        normalize.offer(SOURCE, {"product_id": 101}, {"id": 101, **changes}, NOW)
