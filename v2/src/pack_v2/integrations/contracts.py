from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import SecretStr

CheckStatus = Literal["passed", "denied", "unauthorized", "unavailable", "rate_limited", "invalid_response"]


@dataclass(frozen=True)
class OzonCredentials:
    client_id: str
    api_key: SecretStr


@dataclass(frozen=True)
class CapabilityCheck:
    code: str
    title: str
    status: CheckStatus
    message: str


class AccessChecker(Protocol):
    def check(self, credentials: OzonCredentials) -> list[CapabilityCheck]: ...


@dataclass(frozen=True)
class AccountView:
    id: UUID
    marketplace: str
    title: str
    active: bool
    revision: int
    client_id: str | None
    key_saved: bool
    status: str
    checked_at: datetime | None
    results: list[dict]
    configuration_error: str | None = None


@dataclass(frozen=True)
class AccountLabel:
    id: UUID
    title: str
    marketplace: str
    active: bool
    credential_revision: int


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
