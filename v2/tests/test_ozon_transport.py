from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime

import httpx
import pytest
from pydantic import SecretStr

from pack_v2.integrations.contracts import OzonCredentials
from pack_v2.integrations.ozon.transport import BASE_URL, OzonTransport, body_of, retry_after
from pack_v2.integrations.public import ReadError

KEY = "synthetic-secret-never-return"
CREDS = OzonCredentials("000123", SecretStr(KEY))
PATH = "/v3/product/list"
NOW = datetime(2026, 11, 21, tzinfo=UTC)


@pytest.mark.parametrize(
    "path",
    [
        "/v4/posting/fbs/ship",
        "/v6/fbs/posting/product/exemplar/set",
        "/v6/fbs/posting/product/exemplar/create-or-get",
        "https://evil.invalid/v3/product/list",
        "//evil.invalid/v3/product/list",
        "/v3/product/list?extra=1",
    ],
)
def test_mutation_endpoints_and_external_urls_cannot_be_called(path):
    def unexpected(request):
        pytest.fail("No request should leave the transport")

    with OzonTransport(CREDS, transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ValueError):
            client.read(path, {})


def test_request_identity_exact_values_and_decimal_response():
    def handle(request):
        assert str(request.url) == BASE_URL + PATH
        assert request.method == "POST"
        assert request.headers["client-id"] == "000123"
        assert request.headers["api-key"] == KEY
        assert request.headers["content-type"] == "application/json"
        return httpx.Response(200, content=b'{"amount":123456789.123456789}')

    with OzonTransport(CREDS, transport=httpx.MockTransport(handle)) as client:
        assert client.read(PATH, {})["amount"] == Decimal("123456789.123456789")
    assert client.client.is_closed


@pytest.mark.parametrize(
    "status,kind",
    [
        (400, "invalid_request"),
        (401, "unauthorized"),
        (403, "denied"),
        (404, "not_found"),
        (302, "invalid_request"),
    ],
)
def test_permanent_errors_not_retried_or_redirected_and_no_secret_leak(status, kind):
    requests, waits = [], []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={"message": KEY}, headers={"location": "https://evil.invalid/"})

    with OzonTransport(CREDS, transport=httpx.MockTransport(handle), wait=waits.append) as client:
        with pytest.raises(ReadError) as error:
            client.read(PATH, {})
    assert error.value.kind == kind and error.value.status == status
    assert KEY not in str(error.value) and KEY not in repr(error.value)
    assert not error.value.retryable and not waits and len(requests) == 1


@pytest.mark.parametrize("failure", [429, 503, "timeout"])
def test_transient_reads_retry_then_recover(failure):
    requests, waits = [], []

    def handle(request):
        requests.append(request)
        if len(requests) < 3:
            if failure == "timeout":
                raise httpx.ReadTimeout(KEY, request=request)
            return httpx.Response(failure, headers={"retry-after": "1"})
        return httpx.Response(200, json={"items": []})

    with OzonTransport(CREDS, transport=httpx.MockTransport(handle), wait=waits.append) as client:
        assert client.read(PATH, {}) == {"items": []}
    assert len(requests) == 3 and len(waits) == 2
    assert all(0 <= delay <= 2 for delay in waits)


def test_repeated_network_failure_bounded_and_sanitized():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ConnectError(KEY, request=request)

    with OzonTransport(CREDS, transport=httpx.MockTransport(handle), wait=lambda _: None) as client:
        with pytest.raises(ReadError) as error:
            client.read(PATH, {})
    assert len(calls) == 3 and error.value.kind == "unavailable"
    assert KEY not in repr(error.value) and error.value.__cause__ is None
    assert error.value.__suppress_context__


def test_long_server_delay_returned_for_future_worker_without_early_retry():
    calls, waits = [], []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "120"})

    with OzonTransport(CREDS, transport=httpx.MockTransport(handle), wait=waits.append) as client:
        with pytest.raises(ReadError) as error:
            client.read(PATH, {})
    assert error.value.retry_after == 120 and error.value.retryable
    assert len(calls) == 1 and not waits


@pytest.mark.parametrize(
    "body",
    [
        b"null",
        b"[]",
        b"not json",
        b'{"amount":NaN}',
        b'{"a":Infinity}',
        b'{"error":"secret"}',
        b'{"result":{"errors":["secret"]}}',
        b'{"code":7}',
        b"\xff",
    ],
)
def test_http_200_does_not_make_invalid_response_successful(body):
    with OzonTransport(
        CREDS, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
    ) as client:
        with pytest.raises(ReadError, match="неожиданный ответ") as error:
            client.read(PATH, {})
    assert error.value.kind == "invalid_response"


def test_response_limit_applies_to_decoded_stream():
    with OzonTransport(
        CREDS,
        response_limit=32,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": "x" * 100})),
    ) as client:
        with pytest.raises(ReadError):
            client.read(PATH, {})


def test_wrapped_and_flat_envelopes_and_wrong_result_type():
    assert body_of({"items": []}) == {"items": []}
    assert body_of({"result": {"items": []}}) == {"items": []}
    with pytest.raises(ReadError):
        body_of({"result": []})


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", 0),
        ("120", 120),
        ("-1", None),
        ("garbage", None),
        (None, None),
        (format_datetime(NOW + timedelta(seconds=20), usegmt=True), 20),
        (format_datetime(NOW - timedelta(seconds=20), usegmt=True), 0),
    ],
)
def test_retry_after_seconds_and_dates(value, expected):
    assert retry_after(value, NOW) == expected
