from decimal import Decimal
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, BeforeValidator, ConfigDict

AccountFilter = Annotated[UUID | None, BeforeValidator(lambda value: None if value == "" else value)]
SYNC_STATUSES = {
    "queued": ("В очереди", ""),
    "running": ("Загружается", "accent"),
    "retrying": ("Ожидает повтора", "warn"),
    "succeeded": ("Загружено", "ok"),
    "failed": ("Не завершено", "err"),
    "stale": ("Требует обновления", "warn"),
}


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    account_id: AccountFilter = None


def msk(value):
    return value.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M") if value else "—"


def money(value):
    if value is None:
        return "—"
    number = Decimal(value)
    places = max(2, -number.normalize().as_tuple().exponent)
    return format(number, f",.{places}f").replace(",", " ").replace(".", ",")
