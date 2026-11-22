"""Sensitive provider contracts used only by trusted instance-data jobs."""

from dataclasses import dataclass, field
from typing import Callable, ContextManager, Protocol

from pack_v2.shared.errors import DomainError

from .contracts import OzonCredentials
from .reading import AccountSource


class OutcomeUnknown(DomainError):
    def __init__(self):
        super().__init__("Результат передачи Ozon не подтверждён. Проверяем сохранённые данные")


@dataclass(frozen=True)
class InstanceSnapshot:
    source: AccountSource
    posting_number: str
    products: tuple[dict, ...] = field(repr=False)
    multi_box_qty: int | None = None
    posting: dict | None = field(default=None, repr=False)


class InstanceClient(Protocol):
    def discover(self, posting_number: str) -> InstanceSnapshot: ...
    def validate(self, payload: dict) -> dict: ...
    def submit(self, payload: dict) -> None: ...
    def status(self, posting_number: str) -> dict: ...
    def update(self, posting_number: str) -> None: ...
    def posting(self, posting_number: str) -> dict: ...


InstanceFactory = Callable[[AccountSource, OzonCredentials], ContextManager[InstanceClient]]
