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

    def ship(self, payload):
        # This stage deliberately supports a single package, never an implicit split.
        if (
            set(payload) != {"posting_number", "packages"}
            or not isinstance(payload["posting_number"], str)
            or not payload["posting_number"]
            or not isinstance(payload["packages"], list)
            or len(payload["packages"]) != 1
        ):
            raise ValueError("Invalid shipping request")
        products = payload["packages"][0].get("products")
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
        try:
            data = self.http._once(SHIP, payload)
            if data.get("result") != [payload["posting_number"]]:
                raise OutcomeUnknown()
        except httpx.HTTPError:
            raise OutcomeUnknown() from None
        except ReadError as error:
            if error.status is None or error.status >= 500:
                raise OutcomeUnknown() from None
            raise
