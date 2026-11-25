import json
from copy import deepcopy

import httpx
import pytest
from test_ozon_instances import CREDS, SOURCE

from pack_v2.integrations.instances import OutcomeUnknown
from pack_v2.integrations.ozon.shipping import SHIP, OzonShippingAdapter
from pack_v2.integrations.reading import ReadError

PAYLOAD = {
    "posting_number": "100-200-1",
    "packages": [
        {"products": [{"product_id": 123, "quantity": 1}]},
        {"products": [{"product_id": 123, "quantity": 1}]},
    ],
}


@pytest.mark.parametrize("mode", ["ok", "duplicate", "short", "timeout", "403", "429", "500"])
def test_split_has_one_http_attempt_and_requires_all_distinct_child_names(mode):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == SHIP and json.loads(request.content) == PAYLOAD
        if mode == "timeout":
            raise httpx.ReadTimeout("private synthetic payload", request=request)
        if mode in ("403", "429", "500"):
            return httpx.Response(int(mode), json={"message": "private synthetic payload"})
        values = (
            ["100-200-1", "100-200-2"]
            if mode == "ok"
            else ["same", "same"]
            if mode == "duplicate"
            else ["100-200-1"]
        )
        return httpx.Response(200, json={"result": values})

    with OzonShippingAdapter(SOURCE, CREDS, transport=httpx.MockTransport(handle)) as client:
        if mode == "ok":
            assert client.split(PAYLOAD) == ("100-200-1", "100-200-2")
        else:
            with pytest.raises(ReadError if mode in ("403", "429") else OutcomeUnknown) as result:
                client.split(PAYLOAD)
            assert "private synthetic payload" not in str(result.value)
    assert len(calls) == 1


@pytest.mark.parametrize("bad", ["one", "empty", "bool", "duplicate_sku", "extra"])
def test_invalid_split_never_reaches_http(bad):
    data = deepcopy(PAYLOAD)
    if bad == "one":
        data["packages"].pop()
    elif bad == "empty":
        data["packages"][1]["products"] = []
    elif bad == "bool":
        data["packages"][1]["products"][0]["quantity"] = True
    elif bad == "duplicate_sku":
        data["packages"][1]["products"] *= 2
    else:
        data["packages"][1]["private"] = "unsupported"
    with OzonShippingAdapter(
        SOURCE, CREDS, transport=httpx.MockTransport(lambda request: pytest.fail("No HTTP"))
    ) as client:
        with pytest.raises(ValueError):
            client.split(data)
