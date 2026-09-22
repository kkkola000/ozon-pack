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
class CatalogSource:
    """Откуда площадка наполняет общий каталог товаров и штрихкодов.

    client(account) — клиент с product_list/product_info; save(conn, account_id,
    items) — сохранить карточки; key(item) — ключ карточки в ответе.
    """

    client: Callable[[dict], Any]
    save: Callable[[Any, int, list[dict]], int]
    key: Callable[[dict], str | None]


@dataclass(frozen=True)
class ReturnsSource:
    """Откуда у площадки возвраты и как их показать. Раздел один, источник — у площадки.

    Таблица площадки обязана иметь колонки act_id, mark, note, mark_at,
    mark_by, printed_at, received_at, received_day: по ним ядро ведёт акты и
    отметки, не зная, что за строка перед ним — позиция товара или заказ.
    """

    table: str                                  # где лежат строки возвратов
    label: str                                  # заголовок секции на общем листе: «Ozon», «Avito»
    unit: str                                   # чем считать строки: «поз.» или «заказ(ов)»
    ready: Callable[..., list[dict]]            # ready(account_ids, params={}, limit=N): готовые к выдаче, с account_title
    count_ready: Callable[[list[int]], int]     # сколько готово по кабинетам
    page: Callable[[dict, dict], dict]          # контекст вкладки «К выдаче» кабинета: items, stats и всё для list_template
    act_rows: Callable[[str], list[dict]]       # строки акта из этой таблицы
    quantity: Callable[[dict], int]             # штук в строке — для итогов листа
    list_template: str                          # «ozon/returns_list.html»: фильтры, кнопки, таблица
    sheet_template: str                         # «ozon/returns_sheet.html»: таблица на листе печати
    act_template: str                           # «ozon/returns_act_rows.html»: строки акта
    hint_template: str                          # «ozon/returns_hint.html»: что под знаком «?» у вкладок
    pdf_table: Callable[[Any, list[dict], bool], None]   # таблица секции на листе PDF
    sync: Callable[..., dict]                   # sync(account, full=False) -> {..., "message": str}
    received: Callable[[int, str], list[str]] | None = None   # полученные за день, свободные для акта
    claim: Callable[[Any, str, int, list[str]], int] | None = None   # забрать строки в акт
    giveout: Callable[[dict], bytes] | None = None              # штрихкод на выдачу в ПВЗ (Ozon)


@dataclass(frozen=True)
class Workspace:
    """Рабочее место сборщика: страница одна на все площадки, слова — свои.

    Всё, что на складе одинаково (сканы, замок, история, счётчики, печать),
    делает market_pack.html и market_pack.js. Здесь только то, чем площадки
    отличаются: подписи, счётчики очереди и два куска текста — почему нужна
    выгрузка и каков порядок работы. Как рисовать карточку открытой сборки,
    площадка объявляет в своём markets/<код>/static/pack.js.
    """

    placeholder: str                 # что написано в пустом поле сканирования
    banner: str                      # первая подсказка над полем
    sync_label: str                  # надпись на кнопке обновления очереди
    gate_title: str                  # «Скачайте стикеры» — заголовок замка
    download: str                    # «Скачать стикеры» — надпись на его кнопке
    gate_template: str               # «ozon/pack_gate.html»: почему без выгрузки нельзя
    help_template: str               # «ozon/pack_help.html»: порядок работы под очередью
    # Плитки очереди: (id элемента, ключ в counters, подпись, класс значения).
    counters: tuple[tuple[str, str, str, str], ...]


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
    sync: Callable[..., dict]                # загрузка данных кабинета: sync(account, *, returns_too=True)
    nav: Callable[[dict], list[NavItem]]     # меню и счётчики для кабинета
    stats: Callable[[int], dict[str, int]]   # плитки в «Настройках»
    # Запасные ключи из .env — только там, где они исторически были (Ozon).
    env_credentials: Callable[[], tuple[str, str]] | None = None
    # Свои таблицы: DDL и какие из них хранят ответ площадки целиком (колонка raw).
    schema: str = ""
    raw_tables: tuple[str, ...] = ()
    # Разовые правки своих таблиц в старых базах; вызываются после создания схемы.
    migrate: Callable[[Any], None] | None = None
    # Что ещё не выгружено из стикеров — для замка на «Сборке». None — стикеров нет.
    pending_labels: Callable[[int], list[str]] | None = None
    # Умеет ли площадка наполнять каталог товаров (штрихкоды для сборки).
    catalog: CatalogSource | None = None
    # Дополнительные переменные для страницы «Настройки» текущего кабинета.
    settings_context: Callable[[dict], dict] | None = None
    # И её же куски этой страницы: строки сводки и раздел со своими настройками.
    settings_rows: str | None = None       # «ozon/settings_rows.html»
    settings_panel: str | None = None      # «ozon/settings_panel.html»
    # Возвраты: None — площадка их в панель не отдаёт, раздел ей не показывается.
    returns: ReturnsSource | None = None
    # Заказы кабинетов для общего списка на рабочем месте: feed(account_ids, limit).
    # Строка приводится к общему виду — {account_id, number, goods, quantity,
    # deadline, deadline_local, urgency, status_label, in_work}, — чтобы список
    # показывал рядом отправление Ozon и заказ Avito, не различая их.
    orders_feed: Callable[..., list[dict]] | None = None
    # Рабочее место сборщика: слова и счётчики очереди.
    workspace: Workspace | None = None
    extra: dict[str, Any] = field(default_factory=dict)
