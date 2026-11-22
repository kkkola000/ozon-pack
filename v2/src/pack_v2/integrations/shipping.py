"""One-package shipping, separate from read and exemplar clients."""

from typing import Callable, ContextManager, Protocol

from .contracts import OzonCredentials
from .reading import AccountSource


class ShippingClient(Protocol):
    def ship(self, payload: dict) -> None: ...


ShippingFactory = Callable[[AccountSource, OzonCredentials], ContextManager[ShippingClient]]
