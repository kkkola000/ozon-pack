import json
from dataclasses import asdict

import httpx
import pytest
from pydantic import SecretStr

from pack_v2.integrations.contracts import OzonCredentials
from pack_v2.integrations.ozon.access import BASE_URL, OzonAccessChecker

KEY = "synthetic-api-key-not-a-real-credential"
CREDENTIALS = OzonCredentials("000123", SecretStr(KEY))
RESPONSES = {
    "/v4/posting/fbs/list": {"postings": []},
    "/v3/product/list": {"result": {"items": []}},
    "/v1/returns/list": {"returns": []},
}


def test_only_read_endpoints_minimal_payload_and_correct_headers():
    seen = []

    def handle(request):
        seen.append(request)
        assert str(request.url).startswith(BASE_URL + "/")
        assert request.method == "POST"
        assert request.headers["Client-Id"] == "000123"
        assert request.headers["Api-Key"] == KEY
        assert json.loads(request.content)["limit"] == 1
        return httpx.Response(200, json=RESPONSES[request.url.path])

    result = OzonAccessChecker(httpx.MockTransport(handle)).check(CREDENTIALS)
    assert {str(r.url.path) for r in seen} == set(RESPONSES)
    assert {row.status for row in result} == {"passed"}
    assert {row.code for row in result} == {"orders", "products", "returns"}
    assert KEY not in repr(CREDENTIALS)


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "unauthorized"),
        (403, "denied"),
        (429, "rate_limited"),
        (500, "unavailable"),
        (302, "unavailable"),
    ],
)
def test_http_errors_and_redirects_are_safe_and_not_followed(status, expected):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(
            status, json={"message": KEY}, headers={"location": "https://attacker.invalid/steal"}
        )

    result = OzonAccessChecker(httpx.MockTransport(handle)).check(CREDENTIALS)
    assert len(seen) == 3
    assert all(str(r.url).startswith(BASE_URL) for r in seen)
    assert all(row.status == expected for row in result)
    assert KEY not in json.dumps([asdict(r) for r in result])


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"null",
        b"[]",
        b"{}",
        b'{"error":"bad"}',
        b'{"result":{"items":[],"error":"bad"}}',
        b'{"code":7,"result":{"items":[]}}',
        b"x" * 150_000,
    ],
)
def test_http_200_without_valid_data_does_not_confirm_access(body):
    checker = OzonAccessChecker(httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    assert all(r.status == "invalid_response" for r in checker.check(CREDENTIALS))


def test_network_error_does_not_leak_exception_or_credentials():
    def handle(request):
        raise httpx.ReadTimeout("upstream accidentally included " + KEY, request=request)

    result = OzonAccessChecker(httpx.MockTransport(handle)).check(CREDENTIALS)
    assert all(r.status == "unavailable" for r in result)
    assert KEY not in repr(result)


def test_permissions_are_checked_independently():
    def handle(request):
        if request.url.path == "/v4/posting/fbs/list":
            return httpx.Response(200, json={"result": {"postings": []}})
        return httpx.Response(403)

    result = OzonAccessChecker(httpx.MockTransport(handle)).check(CREDENTIALS)
    assert [r.status for r in result] == ["passed", "denied", "denied"]
