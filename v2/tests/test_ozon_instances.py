import json
from uuid import uuid4

import httpx
import pytest
from instance_remote import mark
from pydantic import SecretStr

from pack_v2.integrations.contracts import OzonCredentials
from pack_v2.integrations.instances import OutcomeUnknown
from pack_v2.integrations.ozon.instances import GET, SET, UPDATE, OzonInstanceAdapter
from pack_v2.integrations.public import AccountSource, ReadError

CREDS = OzonCredentials("000123", SecretStr("synthetic-private-api-key"))
SOURCE = AccountSource(uuid4(), "ozon", 0)


@pytest.mark.parametrize("path", [SET, UPDATE])
def test_empty_200_for_writes_is_only_a_receipt_and_transport_never_retries(path):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200)

    with OzonInstanceAdapter(SOURCE, CREDS, transport=httpx.MockTransport(handle)) as client:
        if path == SET:
            client.submit({"posting_number": "p", "products": []})
        else:
            client.update("p")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["timeout", 500, "malformed", "business-error"])
def test_ambiguous_writes_are_sanitized_and_single_attempts(failure):
    calls = []

    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(mark(), request=request)
        if failure == 500:
            return httpx.Response(500, json={"message": mark()})
        if failure == "malformed":
            return httpx.Response(200, content=mark().encode())
        return httpx.Response(200, json={"code": 13, "message": mark()})

    with OzonInstanceAdapter(SOURCE, CREDS, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(OutcomeUnknown) as error:
            client.submit({"posting_number": "p", "products": []})
    assert len(calls) == 1 and mark() not in str(error.value)


def test_definitive_denial_is_distinct_and_response_body_is_never_exposed():
    with OzonInstanceAdapter(
        SOURCE, CREDS, transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": mark()}))
    ) as client:
        with pytest.raises(ReadError) as error:
            client.submit({"posting_number": "p", "products": []})
    assert error.value.status == 403 and mark() not in str(error.value)


def test_wire_json_preserves_full_crypto_gs_quotes_and_no_double_escaping():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200)

    code = mark(serial='AbC!>QwERt"9x')
    with OzonInstanceAdapter(SOURCE, CREDS, transport=httpx.MockTransport(handle)) as client:
        client.submit(
            {
                "posting_number": "p",
                "products": [{"exemplars": [{"marks": [{"mark": code, "mark_type": "mandatory_mark"}]}]}],
            }
        )
    wire = calls[0].content
    assert json.loads(wire)["products"][0]["exemplars"][0]["marks"][0]["mark"] == code
    assert wire.count(b"\\u001d") == 2 and b"\\\\u001d" not in wire and b"\x1d" not in wire


@pytest.mark.parametrize(
    "malformed", ["missing-quantity", "duplicate-id", "wrong-mark-type", "wrong-posting", "truthy-flag"]
)
def test_discovery_rejects_incomplete_or_ambiguous_provider_contract(malformed):
    unit = {"exemplar_id": 1, "marks": []}
    product = {
        "product_id": 1,
        "quantity": 1,
        "exemplars": [unit],
        "is_gtd_needed": False,
        "is_mandatory_mark_needed": False,
        "is_mandatory_mark_possible": False,
    }
    data = {"posting_number": "p", "products": [product]}
    if malformed == "missing-quantity":
        product.pop("quantity")
    elif malformed == "duplicate-id":
        product.update(quantity=2, exemplars=[unit, dict(unit)])
    elif malformed == "wrong-mark-type":
        unit["marks"] = [{"mark_type": "KIZ", "mark": mark()}]
    elif malformed == "wrong-posting":
        data["posting_number"] = "other"
    else:
        product["is_gtd_needed"] = "false"
    with OzonInstanceAdapter(
        SOURCE, CREDS, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    ) as client:
        with pytest.raises(ReadError):
            client.discover("p")


def test_instance_client_cannot_call_shipping_or_an_arbitrary_host():
    with OzonInstanceAdapter(
        SOURCE, CREDS, transport=httpx.MockTransport(lambda r: pytest.fail("No request allowed"))
    ) as client:
        for path in ("/v4/posting/fbs/ship", "https://evil.invalid" + GET, "//evil.invalid" + SET):
            with pytest.raises(ValueError):
                client._call(path, {})
            with pytest.raises(ValueError):
                client._call(path, {}, write=True)
