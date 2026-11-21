from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class ItemView:
    id: UUID
    source_line_id: str
    name: str
    article: str | None
    quantity: int
    price: str | None
    currency: str | None
    references: tuple[tuple[str, str], ...]
    offer_id: UUID | None
    match_state: str
    image_url: str | None
    codes: tuple[str, ...]
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    source_present: bool


@dataclass(frozen=True)
class ShipmentView:
    id: UUID
    order_id: UUID
    account_id: UUID
    posting_number: str
    order_number: str | None
    references: tuple[tuple[str, str], ...]
    state: str
    source_status: str | None
    source_substatus: str | None
    needs_review: bool
    transition_failed: bool
    deadline: datetime | None
    processing_started_at: datetime | None
    warehouse_name: str | None
    observed_at: datetime | None
    checked_at: datetime | None
    parent_id: UUID | None
    parent_number: str | None
    related_numbers: tuple[str, ...]
    package_count_hint: int | None
    requirements_complete: bool
    items: tuple[ItemView, ...]


@dataclass(frozen=True)
class ShipmentPage:
    items: tuple[ShipmentView, ...]
    total: int
    page: int
    pages: int


@dataclass(frozen=True)
class ShipmentLink:
    id: UUID
    posting_number: str
    state: str
    needs_review: bool


@dataclass(frozen=True)
class ShipmentDetail:
    shipment: ShipmentView
    siblings: tuple[ShipmentLink, ...]


@dataclass(frozen=True)
class PackingLine:
    item_id: UUID
    source_line_id: str
    offer_id: UUID | None
    name: str
    article: str | None
    quantity: int
    references: tuple[tuple[str, str], ...]
    codes: tuple[str, ...]
    image_url: str | None
    marking_hint: bool
    has_components: bool


@dataclass(frozen=True)
class PackingManifest:
    shipment_id: UUID
    order_id: UUID
    account_id: UUID
    posting_number: str
    assembly_revision: int
    basis: str
    state: str
    eligible: bool
    deadline: datetime | None
    warehouse_name: str | None
    items: tuple[PackingLine, ...]


@dataclass(frozen=True)
class PlannedUnit:
    unit_id: UUID
    allocation_id: UUID
    item_id: UUID
    ordinal: int


@dataclass(frozen=True)
class PackingCandidate:
    id: UUID
    account_id: UUID
    state: str
    assembly_revision: int
    posting_number: str
    quantity: int
    deadline: datetime | None
    warehouse_name: str | None
