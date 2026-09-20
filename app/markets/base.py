"""Что площадка обязана объявить о себе, чтобы ядро её подхватило.

Ядро не знает площадок по именам. Всё, что зависит от площадки — как
называются ключи, какие таблицы её, куда вести после переключения кабинета,
как проверить ключи, что показать в меню, — площадка объявляет одним объектом
Market в своём пакете, а ядро спрашивает реестр (registry.py).

Необязательные поля равны None у площадки, у которой такой возможности нет:
ядро тогда просто не показывает соответствующую кнопку или раздел.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class MarketError(RuntimeError):
    """Общий предок ошибок обращения к API площадок.

    Ядро ловит его, не зная, чья это ошибка: сообщение и код HTTP берутся из
    самого исключения, как и раньше.
    """

    message: str
    status: int | None


class KeyCheckError(ValueError):
    """Проверка ключей до сохранения не прошла; текст — готовое объяснение оператору."""


@dataclass(frozen=True)
class NavItem:
    """Пункт меню площадки в шапке."""

    href: str
    label: str
    tab: str                     # значение active_tab, при котором пункт подсвечен
    section: str                 # раздел из access: pack / orders / returns
    # (число, класс значка, подсказка) — нулевые значки не рисуются
    badges: tuple[tuple[int, str, str], ...] = ()


@dataclass(frozen=True)
class Market:
    code: str                    # "ozon" — так площадка записана в accounts.marketplace
    title: str                   # «Яндекс Маркет» — как показать человеку
    id_label: str                # что спросить в «Настройках» первым полем
    key_label: str               # и вторым
    hint: str                    # где взять ключи
    home: str                    # куда вести после переключения на кабинет
    prefixes: tuple[str, ...]    # адреса разделов площадки — при переключении они не сбрасываются
    tables: tuple[str, ...]      # её таблицы с account_id: удаляются вместе с кабинетом
    router: Any                  # APIRouter разделов площадки
    get_client: Callable[[dict | None], Any]
    reset_client: Callable[[int | None], None]
    probe: Callable[[str, str], None]        # проверить ключи до сохранения; KeyCheckError с текстом
    ping: Callable[[dict], dict]             # проверить ключи кабинета; MarketError при отказе
    sync: Callable[..., dict]                # загрузка данных кабинета: sync(account, *, returns=True)
    nav: Callable[[dict], list[NavItem]]     # меню и счётчики для кабинета
    stats: Callable[[int], dict[str, int]]   # плитки в «Настройках»
    # Запасные ключи из .env — только там, где они исторически были (Ozon).
    env_credentials: Callable[[], tuple[str, str]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
