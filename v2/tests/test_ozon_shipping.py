import json
from copy import deepcopy

import httpx
import pytest
from test_ozon_instances import CREDS, SOURCE

from pack_v2.integrations.instances import OutcomeUnknown
from pack_v2.integrations.ozon.shipping import SHIP, OzonShippingAdapter
from pack_v2.integrations.ozon.transport import OzonTransport
from pack_v2.integrations.reading import ReadError

PAYLOAD = {"posting_number": "0001-0002-1", "packages": [{"products": [{"product_id": 123, "quantity": 2}]}]}


@pytest.mark.parametrize("mode", ["ok", "timeout", "malformed", "empty", "unexpected", "500", "denied"])
def test_ship_is_single_attempt_and_ack_never_becomes_ready(mode):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == SHIP and json.loads(request.content) == PAYLOAD
        if mode == "timeout":
            raise httpx.ReadTimeout("private-code", request=request)
        if mode == "empty":
            return httpx.Response(200)
        if mode == "malformed":
            return httpx.Response(200, content=b"private-code")
        if mode in ("500", "denied"):
            return httpx.Response(500 if mode == "500" else 403, json={"message": "private-code"})
        return httpx.Response(200, json={"result": [PAYLOAD["posting_number"] if mode == "ok" else "other"]})

    with OzonShippingAdapter(SOURCE, CREDS, transport=httpx.MockTransport(handle)) as client:
        if mode == "ok":
            assert client.ship(PAYLOAD) is None
        else:
            with pytest.raises(ReadError if mode == "denied" else OutcomeUnknown) as result:
                client.ship(PAYLOAD)
            assert "private-code" not in str(result.value)
    assert len(calls) == 1


def test_general_read_client_cannot_ship():
    with OzonTransport(
        CREDS, transport=httpx.MockTransport(lambda request: pytest.fail("No HTTP"))
    ) as reader:
        with pytest.raises(ValueError):
            reader.read(SHIP, PAYLOAD)


@pytest.mark.parametrize("bad", ["split", "float", "bool", "duplicate", "extra"])
def test_ship_rejects_unsupported_or_inexact_payload_before_http(bad):
    data = deepcopy(PAYLOAD)
    if bad == "split":
        data["packages"] *= 2
    elif bad == "float":
        data["packages"][0]["products"][0]["quantity"] = 1.5
    elif bad == "bool":
        data["packages"][0]["products"][0]["product_id"] = True
    elif bad == "duplicate":
        data["packages"][0]["products"] *= 2
    else:
        data["unapproved"] = True
    with OzonShippingAdapter(
        SOURCE, CREDS, transport=httpx.MockTransport(lambda request: pytest.fail("No HTTP"))
    ) as client:
        with pytest.raises(ValueError):
            client.ship(data)
