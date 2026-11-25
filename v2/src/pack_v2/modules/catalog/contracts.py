from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pack_v2.integrations.public import SyncView as SyncView


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
class OfferMatch:
    offer_id: UUID | None
    state: str
