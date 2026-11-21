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
