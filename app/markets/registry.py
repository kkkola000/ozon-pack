"""Реестр площадок — единственное место, где они перечислены.

Новая площадка — это новый пакет в app/markets и одна строка здесь. Общий код
её не упоминает: он берёт список отсюда.

Порядок в списке — порядок в выпадающих списках панели.
"""
from __future__ import annotations

from .avito import MARKET as avito
from .base import Market
from .ozon import MARKET as ozon
from .yandex import MARKET as yandex

MARKETS: dict[str, Market] = {m.code: m for m in (ozon, avito, yandex)}


def get(code: str | None) -> Market | None:
    return MARKETS.get(str(code or ""))


def require(code: str | None) -> Market:
    market = get(code)
    if market is None:
        raise KeyError(f"Неизвестная площадка: {code!r}")
    return market


def all_markets() -> list[Market]:
    return list(MARKETS.values())


def codes() -> list[str]:
    return list(MARKETS)
