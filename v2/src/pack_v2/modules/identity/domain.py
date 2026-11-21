import re
import unicodedata
from dataclasses import dataclass
from uuid import UUID

from pack_v2.shared.errors import DomainError, Forbidden

SECTIONS = frozenset({"pack", "orders", "returns", "products", "reports", "logs", "stickers"})
ROLES = {"owner": "Владелец", "admin": "Администратор", "packer": "Сборщик"}


def normalize_login(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not re.fullmatch(r"[\w.@+-]{1,64}", normalized):
        raise DomainError("Логин: от 1 до 64 букв, цифр или символов . @ + - _")
    return normalized


@dataclass(frozen=True)
class Principal:
    id: UUID
    login: str
    role: str
    sections: frozenset[str]

    def can(self, section: str) -> bool:
        if self.role == "owner":
            return True
        if section == "settings":
            return self.role == "admin"
        if section == "printing":
            return True
        return section in self.sections

    def require(self, section: str) -> None:
        if not self.can(section):
            raise Forbidden("Нет доступа к этому разделу")


@dataclass(frozen=True)
class Authenticated:
    user: Principal
    session_id: UUID
    csrf_token: str
