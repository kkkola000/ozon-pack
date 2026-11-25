"""FBS readiness is a single status mutation; ambiguous responses are reconciled by reads."""

from .instances import YandexInstanceAdapter
from .transport import error, positive


class YandexShippingAdapter(YandexInstanceAdapter):
    def __init__(self, source, credentials, *, transport=None):
        super().__init__(source, credentials, transport=transport)
        self.http.shipping = True
        self.http.instances = False

    def ship(self, payload):
        number = positive(payload["posting_number"])
        fresh = self.discover(number)
        native = fresh.posting["yandex"]
        if native["campaign_id"] != positive(payload["campaign_id"]) or native["status"] != "PROCESSING":
            raise error("invalid_request", status=409)
        if (
            native["layout_digest"] != payload["layout_digest"]
            or native["data_digest"] != payload["data_digest"]
        ):
            raise error("invalid_request", status=409)
        if native["substatus"] == "READY_TO_SHIP":
            return
        if native["substatus"] != "STARTED":
            raise error("invalid_request", status=409)
        # An order/operation in a 200 response is only acknowledgement, never readiness proof.
        self.http.once(
            "PUT",
            f"/v2/campaigns/{native['campaign_id']}/orders/{number}/status",
            {"order": {"status": "PROCESSING", "substatus": "READY_TO_SHIP"}},
            write=True,
        )
