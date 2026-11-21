from datetime import UTC, datetime, timedelta

import httpx

from pack_v2.integrations.contracts import CapabilityCheck, OzonCredentials

BASE_URL = "https://api-seller.ozon.ru"
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
        with httpx.Client(
            base_url=BASE_URL,
            transport=self.transport,
            follow_redirects=False,
            timeout=httpx.Timeout(10, connect=5),
            headers={
                "Client-Id": credentials.client_id,
                "Api-Key": credentials.api_key.get_secret_value(),
                "Accept": "application/json",
            },
        ) as client:
            return [
                self._probe(client, code, title, path, payload, field)
                for code, title, path, payload, field in probes
            ]

    @staticmethod
    def _probe(client, code, title, path, payload, field) -> CapabilityCheck:
        def result(status, message):
            return CapabilityCheck(code, title, status, message)

        try:
            with client.stream("POST", path, json=payload) as response:
                if response.status_code == 401:
                    return result("unauthorized", "Ozon не принял Client-Id или Api-Key (HTTP 401)")
                if response.status_code == 403:
                    return result("denied", "Нет доступа у API-ключа. Проверьте его права в Ozon (HTTP 403)")
                if response.status_code == 429:
                    return result("rate_limited", "Достигнут лимит Ozon. Повторите проверку позже (HTTP 429)")
                if response.status_code != 200:
                    return result("unavailable", "Ozon не подтвердил доступ. Повторите проверку позже")
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        return result("invalid_response", "Ozon вернул неожиданный ответ")
                import json

                data = json.loads(body)
                if isinstance(data, dict):
                    if data.get("error") or data.get("errors") or data.get("code") not in (None, 0, "0"):
                        return result("invalid_response", "Ozon вернул неожиданный ответ")
                    data = data.get("result", data)
                if not isinstance(data, dict) or not isinstance(data.get(field), list):
                    return result("invalid_response", "Ozon вернул неожиданный ответ")
                if data.get("error") or data.get("errors") or data.get("code") not in (None, 0, "0"):
                    return result("invalid_response", "Ozon вернул неожиданный ответ")
                return result("passed", "Доступ подтверждён")
        except (ValueError, UnicodeError):
            return result("invalid_response", "Ozon вернул неожиданный ответ")
        except httpx.HTTPError:
            # Never return exception text, headers or upstream response bodies.
            return result("unavailable", "Не удалось связаться с Ozon. Повторите проверку позже")
