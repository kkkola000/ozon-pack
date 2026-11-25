from pack_v2.integrations.contracts import CapabilityCheck
from pack_v2.integrations.reading import ReadError

from .transport import YandexTransport, body


class YandexAccessChecker:
    def __init__(self, *, transport=None):
        self.transport = transport

    def check(self, credentials):
        client = YandexTransport(credentials, transport=self.transport, attempts=1)
        results = []
        try:
            for code, title, method, path, payload, params, key, envelope in (
                (
                    "orders",
                    "Заказы FBS",
                    "POST",
                    client.orders_path,
                    {"programTypes": ["FBS"]},
                    {"limit": 1},
                    "orders",
                    False,
                ),
                ("products", "Товары", "POST", client.catalog_path, {}, {"limit": 1}, "offerMappings", True),
                ("campaigns", "Магазины", "GET", "/v2/campaigns", None, {"pageSize": 1}, "campaigns", False),
            ):
                try:
                    data = client.read(method, path, payload, params)
                    data = body(data) if envelope else data
                    if not isinstance(data.get(key), list):
                        from .transport import error

                        raise error("invalid_response")
                    status, message = "passed", "Чтение доступно"
                except ReadError as failure:
                    status, message = failure.kind, str(failure)
                    if status not in {
                        "unauthorized",
                        "denied",
                        "rate_limited",
                        "unavailable",
                        "invalid_response",
                    }:
                        status = "invalid_response"
                results.append(CapabilityCheck(code, title, status, message))
        finally:
            client.close()
        return results
