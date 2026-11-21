from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class OfferView:
    id: UUID
    account_id: UUID
    name: str
    article: str | None
    image_url: str | None
    archived: bool | None
    source_present: bool
    revision: int
    observed_at: datetime | None
    references: tuple[tuple[str, str], ...]
    codes: tuple[str, ...]


@dataclass(frozen=True)
class CatalogPage:
    items: tuple[OfferView, ...]
    total: int
    page: int
    pages: int


@dataclass(frozen=True)
class SyncView:
    id: UUID
    account_id: UUID
    status: str
    pages: int
    items: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    last_success_at: datetime | None
    retry_at: datetime | None
