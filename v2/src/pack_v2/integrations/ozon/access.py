from datetime import UTC, datetime, timedelta

import httpx

from pack_v2.integrations.contracts import CapabilityCheck, OzonCredentials
from pack_v2.integrations.reading import ReadError

from .transport import BASE_URL, OzonTransport, body_of

__all__ = ["BASE_URL", "OzonAccessChecker"]
MAX_RESPONSE = 128 * 1024


class OzonAccessChecker:
    """Only read endpoints, one record each. No redirects, shipping or exemplar creation."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def check(self, credentials: OzonCredentials) -> list[CapabilityCheck]:
        now = datetime.now(UTC)

        def iso(value):
            return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        probes = (
            (
                "orders",
                "Заказы FBS",
                "/v4/posting/fbs/list",
                {
                    "sort_dir": "asc",
                    "filter": {"since": iso(now - timedelta(days=1)), "to": iso(now)},
                    "limit": 1,
                    "cursor": "",
                    "with": {"analytics_data": False, "barcodes": False},
                },
                "postings",
            ),
            (
                "products",
                "Товары",
                "/v3/product/list",
                {"filter": {"visibility": "ALL"}, "limit": 1, "last_id": ""},
                "items",
            ),
            ("returns", "Возвраты", "/v1/returns/list", {"limit": 1, "last_id": 0}, "returns"),
        )
        with OzonTransport(
            credentials, transport=self.transport, attempts=1, response_limit=MAX_RESPONSE
        ) as client:
            return [
                self._probe(client, code, title, path, payload, field)
                for code, title, path, payload, field in probes
            ]

    @staticmethod
    def _probe(client, code, title, path, payload, field) -> CapabilityCheck:
        try:
            data = body_of(client.read(path, payload))
            if not isinstance(data.get(field), list):
                raise ReadError("invalid_response")
            return CapabilityCheck(code, title, "passed", "Доступ подтверждён")
        except ReadError as error:
            status = (
                error.kind
                if error.kind in {"unauthorized", "denied", "rate_limited", "invalid_response"}
                else "unavailable"
            )
            return CapabilityCheck(code, title, status, str(error))
