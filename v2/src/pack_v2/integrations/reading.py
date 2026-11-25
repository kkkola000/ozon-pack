"""Provider-neutral, immutable read contracts. They contain no credentials or raw responses."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable, ContextManager, Literal, Protocol
from uuid import UUID

from pack_v2.shared.errors import Conflict, DomainError

from .contracts import OzonCredentials


class SourceChanged(Conflict):
    pass


WORKING_STATES = ("awaiting_assembly", "awaiting_dispatch")


class ReadError(DomainError):
    def __init__(
        self, kind: str, *, status: int | None = None, retry_after: float | None = None, marketplace="ozon"
    ):
        messages = {
            "unauthorized": "Ozon не принял Client-Id или Api-Key (HTTP 401)",
            "denied": "Нет доступа у API-ключа. Проверьте его права в Ozon (HTTP 403)",
            "not_found": "Объект не найден в Ozon (HTTP 404)",
            "invalid_request": "Ozon отклонил параметры запроса",
            "rate_limited": "Достигнут лимит Ozon. Повторите запрос позже (HTTP 429)",
            "unavailable": "Не удалось получить данные Ozon. Повторите запрос позже",
            "invalid_response": "Ozon вернул неожиданный ответ. Данные не подтверждены",
            "incomplete": "Ozon вернул неполный набор данных. Повторите загрузку",
            "pagination": "Не удалось полностью прочитать страницы Ozon. Повторите загрузку",
        }
        message = messages.get(kind, messages["invalid_response"])
        if marketplace == "yandex":
            message = message.replace("Ozon", "Яндекс Маркет").replace(
                "Client-Id или Api-Key", "Business ID или Api-Key"
            )
        super().__init__(message)
        self.kind, self.status, self.retry_after = kind, status, retry_after
        self.retryable = kind in {"rate_limited", "unavailable", "incomplete", "pagination"}


@dataclass(frozen=True)
class AccountSource:
    account_id: UUID
    marketplace: str
    credential_revision: int


@dataclass(frozen=True)
class ExternalReference:
    kind: str
    value: str
    authoritative: bool = False
    origin: str | None = None


@dataclass(frozen=True)
class Barcode:
    value: str
    kind: Literal["gtin", "marketplace", "other"]
    origin: str


@dataclass(frozen=True)
class OfferSnapshot:
    source: AccountSource
    source_id: str
    references: tuple[ExternalReference, ...]
    name: str | None
    article: str | None
    barcodes: tuple[Barcode, ...]
    image_url: str | None
    archived: bool | None
    fields_present: frozenset[str]
    observed_at: datetime = field(compare=False, repr=False)
    marking_declared: bool | None = None


@dataclass(frozen=True)
class LineSnapshot:
    source_line_id: str
    product_references: tuple[ExternalReference, ...]
    name: str | None
    quantity: int
    unit_price: Decimal | None
    currency: str | None
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    fields_present: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ShipmentSnapshot:
    source: AccountSource
    source_id: str
    order_references: tuple[ExternalReference, ...]
    references: tuple[ExternalReference, ...]
    source_status: str
    source_substatus: str | None
    state: str
    transition_failed: bool
    items: tuple[LineSnapshot, ...]
    deadline: datetime | None
    processing_started_at: datetime | None
    warehouse_id: str | None
    warehouse_name: str | None
    parent_reference: ExternalReference | None
    related_references: tuple[ExternalReference, ...]
    package_count_hint: int | None
    fields_present: frozenset[str]
    requirements_complete: bool
    observed_at: datetime = field(compare=False, repr=False)


@dataclass(frozen=True)
class ReadPage[T]:
    source: AccountSource
    dataset: Literal["catalog", "shipments"]
    items: tuple[T, ...]
    next_cursor: str | None
    is_last: bool
    total_hint: int | None = None


class MarketplaceReader(Protocol):
    source: AccountSource

    def iter_catalog(self, *, cursor: str = "") -> Iterator[ReadPage[OfferSnapshot]]: ...
    def iter_shipments(
        self,
        since: datetime,
        until: datetime,
        *,
        cursor: str = "",
        states: tuple[str, ...] | None = WORKING_STATES,
    ) -> Iterator[ReadPage[ShipmentSnapshot]]: ...
    def get_shipment(self, posting_number: str) -> ShipmentSnapshot | None: ...
    def find_shipment_by_barcode(self, barcode: str) -> ShipmentSnapshot | None: ...


ReaderFactory = Callable[[AccountSource, OzonCredentials], ContextManager[MarketplaceReader]]
