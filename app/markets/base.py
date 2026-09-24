"""Что площадка обязана объявить о себе, чтобы ядро её подхватило.

Ядро не знает площадок по именам. Всё, что зависит от площадки — как
называются ключи, какие таблицы её, куда вести после переключения кабинета,
как проверить ключи, что показать в меню, — площадка объявляет одним объектом
Market в своём пакете, а ядро спрашивает реестр (registry.py).

Необязательные поля равны None у площадки, у которой такой возможности нет:
ядро тогда просто не показывает соответствующую кнопку или раздел.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
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
class CatalogPage:
    """Пачка карточек из обхода каталога: что сохранить и как идут дела.

    total — сколько карточек ожидается всего; ноль значит «площадка не знает»,
    и кнопка тогда просто считает пройденные. skipped — сколько карточек
    пропущено на этом шаге как архивные: в каталог они не попадают, но в итоге
    их показываем, иначе «товаров меньше, чем в кабинете» выглядит как потеря.
    """

    items: list[dict]           # карточки в общем виде: sku, offer_id, name, image, barcodes
    total: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class CatalogSource:
    """Как площадка обходит свой каталог товаров и штрихкодов.

    pages(account) — перебор каталога кабинета: отдаёт CatalogPage, пока они не
    кончатся. Как устроен обход, ядру знать незачем: Ozon сначала берёт список
    артикулов и потом карточки пачками, Маркет отдаёт всё сразу страницами.
    Сохраняет карточки и разводит живое с архивом ядро (core/catalog.py) —
    таблица каталога одна на все площадки.
    """

    pages: Callable[[dict], Iterable[CatalogPage]]


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
    pdf_table: Callable[[Any, list[dict], bool], None]   # таблица секции на листе PDF
    sync: Callable[..., dict]                   # sync(account, full=False) -> {..., "message": str}
    received: Callable[[int, str], list[str]] | None = None   # полученные за день, свободные для акта
    claim: Callable[[Any, str, int, list[str]], int] | None = None   # забрать строки в акт
    giveout: Callable[[dict], bytes] | None = None              # штрихкод на выдачу в ПВЗ (Ozon)


@dataclass(frozen=True)
class LabelsSource:
    """Наклейка заказа: стикер Ozon, этикетка Avito, ярлык Маркета.

    Выгружаются до начала сборки — площадка отдаёт наклейку, пока заказ ждёт
    отгрузки, и упущенную уже не вернуть. Выгрузка идёт одним архивом по всем
    кабинетам сразу: склад один, и бегать за каждым магазином отдельно незачем.

    pending(account_id) — ключи, которые ещё не выгружены; pdf(кабинет,
    человек, ключи) — пачка PDF от площадки; table и key — где стоит отметка
    label_saved_at, по которой запирается сборка.
    """

    word: str                                        # «стикеры»: в именах файлов и сообщениях
    pending: Callable[[int], list[str]]
    pdf: Callable[[dict, dict, list[str]], bytes]
    table: str
    key: str


@dataclass(frozen=True)
class Workspace:
    """Рабочее место сборщика: страница одна на все площадки, слова — свои.

    Всё, что на складе одинаково (сканы, замок, история, счётчики, список
    заказов, печать), делает routes/pack.py вместе с market_pack.html и
    market_pack.js. Здесь только то, чем площадки отличаются: подписи, плитки
    очереди и откуда взять два числа. Как рисовать карточку открытой сборки,
    площадка объявляет в своём markets/<код>/static/pack.js.

    Сканирование идёт через ядро: кабинет в шапке больше не решает, чей заказ
    собирается, — решает фильтр площадок. Ядро само находит кабинет, которому
    принадлежит отсканированный код (`owner`), и уже ему отдаёт скан (`scan`).
    """

    placeholder: str                 # что написано в пустом поле сканирования
    banner: str                      # первая подсказка над полем
    # Плитки очереди: (id элемента, ключ в counters, подпись, класс значения).
    counters: tuple[tuple[str, str, str, str], ...]
    url: str                         # адрес рабочего места: «/pack», «/avito/pack»
    tab: str                         # значение active_tab: по нему подсвечивается меню
    load_state: Callable[[dict, dict], dict]   # состояние сборщика: (кабинет, человек)
    count_queue: Callable[[dict], dict]        # числа для плиток очереди кабинета
    # Чей это код. owner(кабинет, человек, код) -> (вид, номер заказа) либо
    # None, если код этому кабинету не принадлежит. Вид:
    #   «label»   — наклейка заказа этого кабинета;
    #   «product» — товар, и вот какой заказ он откроет прямо сейчас;
    #   «known»   — товар этого кабинета, но открыть нечего (ещё не в том
    #               статусе, собран, собирает другой); номер — о каком заказе
    #               речь, или пусто.
    # Спрашивается до скана и ничего не меняет. Ответ обязан совпадать с тем,
    # что потом сделает scan, — поэтому площадка строит его теми же функциями.
    #
    # Виды различаются не для красоты: наклейка чужого кабинета посреди сборки
    # — это стоп, товар соседнего магазина — обычная позиция, а «known» нужен,
    # чтобы «почему нельзя» объяснял кабинет, где заказ есть, а не тот, что
    # случайно оказался в шапке.
    owner: Callable[[dict, dict, str], tuple[str, str] | None] = lambda _account, _user, _code: None
    # Один скан в этом кабинете: scan(кабинет, человек, код) -> ScanResult.
    scan: Callable[[dict, dict, str], dict] | None = None
    # Отмена начатой сборки: release(кабинет, человек).
    release: Callable[[dict, dict], dict] | None = None
    # «Завершить без скана наклейки» — площадка сама решает, можно ли сейчас:
    # complete(кабинет, человек, причина). None — площадка так не умеет (Avito:
    # заказ там закрывает последняя единица товара).
    complete: Callable[[dict, dict, str], dict] | None = None
    # Наклейка одного заказа на печать: label(кабинет, человек, номер) ->
    # (PDF, имя файла). Берётся с рабочего места, где кабинет заранее неизвестен,
    # поэтому ходит через ядро, а не через ручку площадки.
    label: Callable[[dict, dict, str], tuple[bytes, str]] | None = None


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
    # Наклейки заказов: выгрузка до сборки и замок на неё. None — их нет.
    labels: LabelsSource | None = None
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
