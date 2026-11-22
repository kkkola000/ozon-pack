"""Single-attempt /ship. Acknowledgement never proves readiness."""

import httpx

from pack_v2.integrations.instances import OutcomeUnknown
from pack_v2.integrations.reading import ReadError

from .transport import OzonTransport

SHIP = "/v4/posting/fbs/ship"


class OzonShippingAdapter:
    def __init__(self, source, credentials, *, transport=None):
        if source.marketplace != "ozon":
            raise ValueError("Wrong marketplace")
        self.http = OzonTransport(credentials, transport=transport, attempts=1)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    @staticmethod
    def _validate(payload, split):
        if (
            set(payload) != {"posting_number", "packages"}
            or not isinstance(payload["posting_number"], str)
            or not payload["posting_number"]
            or not isinstance(payload["packages"], list)
            or not (2 <= len(payload["packages"]) <= 100 if split else len(payload["packages"]) == 1)
        ):
            raise ValueError("Invalid shipping request")
        for package in payload["packages"]:
            if not isinstance(package, dict) or set(package) != {"products"}:
                raise ValueError("Invalid shipping package")
            products = package["products"]
            if (
                not isinstance(products, list)
                or not products
                or any(
                    set(p) != {"product_id", "quantity"}
                    or type(p["product_id"]) is not int
                    or not 0 < p["product_id"] < 2**63
                    or type(p["quantity"]) is not int
                    or not 1 <= p["quantity"] <= 1000
                    for p in products
                )
                or len({p["product_id"] for p in products}) != len(products)
            ):
                raise ValueError("Invalid shipping products")

    def ship(self, payload):
        self._validate(payload, False)
        self._send(payload, split=False)

    def split(self, payload):
        self._validate(payload, True)
        return self._send(payload, split=True)

    def _send(self, payload, *, split):
        try:
            data = self.http._once(SHIP, payload)
            result = data.get("result")
            if (
                not isinstance(result, list)
                or len(result) != len(payload["packages"])
                or any(
                    not isinstance(v, str)
                    or not 1 <= len(v) <= 256
                    or not v.isascii()
                    or not all(c.isalnum() or c in "-_" for c in v)
                    for v in result
                )
                or len(set(result)) != len(result)
                or not split
                and result != [payload["posting_number"]]
            ):
                raise OutcomeUnknown()
            return tuple(result)
        except httpx.HTTPError:
            raise OutcomeUnknown() from None
        except ReadError as error:
            if error.status is None or error.status >= 500:
                raise OutcomeUnknown() from None
            raise
