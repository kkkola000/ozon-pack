"""Explicit no-extra-fields port for tests of the local product-check engine.

Production composition always injects InstanceData. The end-to-end instance
suite exercises discovery, capture and transmission through that real service.
"""


class ProductVerificationPort:
    def ensure_context(self, *args):
        return "ready", None

    def bind(self, *args):
        return None

    def view(self, *args):
        return {"status": "accepted", "message": "", "can_verify": True, "items": {}, "pending_unit_id": None}
