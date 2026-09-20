# Целевая структура проекта

Что здесь: как должен быть устроен код, чтобы четвёртая площадка (Wildberries)
добавлялась одним пакетом, а не правкой двенадцати общих файлов. Текущая
раскладка описана в `STRUCTURE.md`; этот документ — куда она переезжает.

Каждый пункт сверен с кодом версии 1.32.0: списки функций, импорты, какие
таблицы читает каждый модуль. Где раньше в обсуждении я ошибался — помечено
**[исправлено]**.

## Три принципа

1. **Ядро не знает ни одной площадки.** В `core/` нет ни импорта `ozon`, ни
   строки `if marketplace == ...`. Сейчас таких развилок двадцать.
2. **Площадка — один пакет.** Клиент, свои таблицы, разбор ответов,
   синхронизация, сборка, роуты, шаблоны, скрипты — всё внутри `markets/<код>/`.
3. **Реестр связывает.** Площадка объявляет себя один раз в `__init__.py`;
   ядро спрашивает реестр, а не перебирает площадки по именам.

## Дерево

```
ozon-pack/
├── README.md  STRUCTURE.md  ARCHITECTURE.md
├── VERSION  COMMIT  requirements*.txt  pytest.ini  ruff.toml  .env.example
│
├── app/
│   ├── main.py                     — собирает FastAPI: middleware, /healthz, «/» → домашняя страница площадки.
│   │                                 Роутеры, статику и шаблоны площадок подключает циклом по реестру
│   │
│   ├── core/                       — ЯДРО: ни одной площадки по имени
│   │   ├── config.py               — Settings из .env: таймауты, часовой пояс, логика сборки
│   │   ├── db.py                   — соединение, журнал событий, kv, общие таблицы (users, accounts, kv, events,
│   │   │                             pack_state, shipped_items, return_acts) и общие миграции (владелец,
│   │   │                             шифрование ключей, первый кабинет из .env). Схемы и миграции площадок — из реестра
│   │   ├── store.py                — общие помощники разбора: _text/_dt/_num, hours_left, local_time, отметка о возврате
│   │   ├── scan.py                 — общая механика сборки: ScanResult, barcode_variants, чтение/запись pack_state,
│   │   │                             снятие брони во всех таблицах заказов
│   │   ├── sync.py                 — фоновый воркер: расписание, обход кабинетов, статус. Что грузить — у площадки
│   │   ├── labels.py               — ZIP-архив стикеров, разрезание PDF по заказам, отметка label_saved_at.
│   │   │                             Список «что ещё не выгружено» — у площадки
│   │   ├── report.py               — отчёт об отгруженных товарах: запись строк, дни, CSV
│   │   ├── catalog.py              — каталог товаров и штрихкоды как общий ресурс: таблицы, поиск по артикулу,
│   │   │                             фоновая перезагрузка. Откуда брать карточки — у площадки (сегодня умеет только Ozon)
│   │   ├── product_sets.py         — наборы: из каких частей собирается товар (импортирует только db — проверено)
│   │   ├── return_acts.py          — жизненный цикл акта: создать за день, подтвердить, вернуть в работу, удалить,
│   │   │                             очистить пустой. Строки акта берёт из таблиц, объявленных площадками
│   │   ├── returns_pdf.py          — лист возвратов файлом PDF
│   │   ├── accounts.py             — кабинеты: создание, ключи, удаление. Список площадок и их таблиц — из реестра
│   │   ├── access.py               — роли и разделы
│   │   ├── security.py             — пароли, сессии, защита от подбора, CSRF
│   │   ├── crypto.py               — шифрование ключей площадок в базе
│   │   ├── deps.py                 — зависимости FastAPI: текущий пользователь и кабинет, require_market(код),
│   │   │                             CSRF, счётчики шапки (из реестра), safe_filename
│   │   └── version.py              — версия и коммит для /healthz
│   │
│   ├── routes/                     — HTTP общих разделов: один адрес на всех
│   │   ├── auth.py                 — вход, выход, переключение кабинета (домашняя страница — из реестра)
│   │   ├── returns.py              — ОДИН раздел «Возвраты»: список текущего кабинета, лист по всем кабинетам,
│   │   │                             печать, PDF, отметки, вкладка актов. Без require_ozon_account
│   │   ├── products.py             — «Товары»: каталог и наборы. Кнопка «Обновить каталог» — только у площадок,
│   │   │                             которые объявили источник
│   │   ├── reports.py              — отчёты об отгрузке и акты в отчётах
│   │   └── admin.py                — журнал, настройки, сотрудники, кабинеты. Проверка ключей, плитки и
│   │                                 фрагмент настроек площадки — из реестра
│   │
│   ├── markets/                    — ПЛОЩАДКИ
│   │   ├── base.py                 — dataclass Market и вложенные ReturnsSource, CatalogSource
│   │   ├── registry.py             — MARKETS = {...}. Единственный общий файл, который правится при новой площадке
│   │   │
│   │   ├── ozon/
│   │   │   ├── __init__.py         — MARKET = Market(...): объявление
│   │   │   ├── client.py           — Seller API: отправления, товары, возвраты, стикеры, штрихкод выдачи
│   │   │   ├── store.py            — DDL postings, posting_items, products, product_barcodes + разбор + posting_view
│   │   │   ├── sync.py             — загрузка отправлений и карточек товаров
│   │   │   ├── pack.py             — сборка: подбор отправления по товару, наборы, закрытие стикером, ship, label_pdf
│   │   │   ├── returns.py          — возвраты Ozon целиком: DDL returns, разбор, загрузка из /v1/returns/list,
│   │   │   │                         перестройка «К выдаче», настройки статусов (sys_name), акт из «полученных»,
│   │   │   │                         штрихкод на выдачу в ПВЗ
│   │   │   ├── routes.py           — «Сборка», «Заказы FBS», сброс отметки, настройки статусов возвратов
│   │   │   ├── templates/
│   │   │   │   ├── orders.html     — список «Заказы FBS»
│   │   │   │   └── settings.html   — фрагмент в «Настройках»: печать стикеров, какие возвраты загружать
│   │   │   └── static/
│   │   │       ├── orders.js       — список заказов: сборка в Ozon, печать стикеров
│   │   │       └── pack.js         — как рисовать позицию на рабочем месте (фото, наборы, маркировка)
│   │   │
│   │   ├── avito/
│   │   │   ├── __init__.py  client.py  store.py  sync.py  pack.py  routes.py
│   │   │   ├── templates/orders.html
│   │   │   └── static/orders.js  pack.js
│   │   │   (возвраты — состояние заказа, отдельного файла не нужно: return_status в avito_orders)
│   │   │
│   │   ├── yandex/
│   │   │   ├── __init__.py  client.py  store.py  sync.py  pack.py  routes.py
│   │   │   ├── templates/orders.html
│   │   │   └── static/orders.js  pack.js
│   │   │   (returns.py появится, когда будут возвраты Маркета)
│   │   │
│   │   └── wildberries/            — тот же состав. Снаружи пакета — одна строка в registry.py
│   │
│   ├── templates/                  — общие страницы
│   │   ├── base.html               — каркас; меню строится циклом по реестру
│   │   ├── login.html  error.html
│   │   ├── market_pack.html        — рабочее место сборщика, одно на все площадки
│   │   ├── returns.html            — один раздел возвратов (вместо returns.html + avito_returns.html)
│   │   ├── returns_print.html      — один лист печати (вместо двух)
│   │   ├── _return_acts.html  _return_mark.html
│   │   ├── products.html  logs.html  settings.html
│   │   └── reports.html  report_day.html  report_return_acts.html  report_return_act.html
│   │
│   └── static/
│       ├── app.css  app.js
│       ├── market_pack.js          — рабочее место: сканы, замок, история, счётчики, печать.
│       │                             Отрисовку позиции берёт из pack.js площадки
│       ├── returns.js              — один скрипт возвратов (вместо returns.js + avito_returns.js)
│       └── return_mark.js  act_admin.js  products.js  code128.js
│
├── deploy/                         — без изменений
│
└── tests/
    ├── conftest.py  pdfstub.py  code128.py
    ├── fakes/                      — подделки клиентов: __init__.py (install), ozon.py, avito.py, yandex.py
    ├── core/                       — test_access, test_accounts, test_api, test_barcode, test_errors,
    │                                 test_migration, test_report, test_return_acts, test_returns_mark,
    │                                 test_store, test_version, test_deploy
    └── markets/
        ├── ozon/                   — test_pack, test_catalog, test_product_sets, test_labels (озоновская часть)
        ├── avito/                  — test_orders, test_pack, test_labels (авитовская часть)
        └── yandex/                 — test_orders, test_pack
```

Состав пакета площадки: **пять обязательных файлов** (`__init__`, `client`,
`store`, `sync`, `pack`, `routes`) **плюс `returns.py`, если у площадки есть
возвраты со своей логикой.** У Ozon это 660 строк — отдельный файл честнее, чем
раздувать `store` и `sync`. У Avito возврат — это статус заказа, тридцать строк
в `store.py`. Разница в числе файлов между площадками — ровно этот один.

## Что объявляет площадка

```python
# app/markets/ozon/__init__.py
MARKET = Market(
    code="ozon", title="Ozon",
    id_label="Client-Id", key_label="Api-Key",
    hint="Настройки → Seller API → Сгенерировать ключ",
    env_keys=("OZON_CLIENT_ID", "OZON_API_KEY"),   # запасные ключи из .env; у остальных None
    home="/pack",
    tables=("postings", "posting_items", "products", "product_barcodes", "returns"),
    schema=store.SCHEMA + returns.SCHEMA,
    migrate=returns.migrate,          # свои разовые миграции (arrived_at, received_day…)
    router=routes.router,
    get_client=client.get_client, ping=client.ping,
    sync=sync.sync_account,           # отправления + товары + возвраты
    nav=routes.nav_items,             # пункты меню и счётчики на них
    stats=routes.settings_stats,      # плитки в «Настройках»
    settings_panel="ozon/settings.html",
    pending_labels=pack.pending_labels,
    catalog=CatalogSource(fetch_offers=client.product_list, fetch_cards=client.product_info),
    returns=ReturnsSource(
        table="returns", ready_sql=returns.pickup_sql, view=returns.return_view,
        giveout=client.giveout_pdf,       # штрихкод на выдачу — есть только у Ozon
        from_received=returns.from_received,   # акт из «полученных» — тоже
    ),
)
```

Необязательные поля — `env_keys`, `migrate`, `settings_panel`, `pending_labels`,
`catalog`, `returns` — у площадки без такой возможности равны `None`, и ядро
просто не показывает соответствующую кнопку или раздел.

## Карта переезда: каждый файл

| Сейчас | Строк | Куда | Примечание |
|---|---|---|---|
| `app/main.py` | 188 | `app/main.py` | роутеры/статика/шаблоны — циклом по реестру |
| `app/config.py` | 124 | `core/config.py` | |
| `app/db.py` | 1242 | `core/db.py` (~430) + DDL и миграции по площадкам | **[исправлено]** миграции `_fill_arrived`, `_fill_status_changed`, `_repair_received_days`, `_release_unreceived`, `_drop_auto_return_acts` — озоновские; `_drop_buyer_contacts` — авитовская |
| `app/store.py` | 922 | `core/store.py` (~120) + `markets/*/store.py`, возвраты Ozon → `ozon/returns.py` | |
| `app/sync.py` | 675 | `core/sync.py` (~130) + `ozon/sync.py`, `ozon/returns.py`, `avito/sync.py`, `yandex/sync.py` | `sync_returns` и `_rebuild_pickup` — 240 строк озоновских возвратов |
| `app/packing.py` | 1071 | `core/scan.py` (~80) + `ozon/pack.py` | общее: `ScanResult`, `barcode_variants`, `clear_state`, `_save_state` |
| `app/labels.py` | 179 | `core/labels.py` (~115) + `pending_*` в объявления площадок | |
| `app/options.py` | 137 | `ozon/returns.py` | **[исправлено]** раньше относил к ядру. Весь файл — статусы возвратов Ozon (`sys_name`), `pickup_sql`, «полученные». Общего в нём нет: kv уже в `db.py` |
| `app/return_acts.py` | 522 | `core/return_acts.py` (~380) + `ozon/returns.py` | **[исправлено]** не целиком в ядро: `received_returns`, `from_received`, `_free_received`, `FROM_GIVEOUT` смотрят в таблицу `returns` Ozon. `rows_of` перебирает `("returns", "avito_orders")` руками — станет циклом по реестру |
| `app/returns_pdf.py` | 389 | `core/returns_pdf.py` | |
| `app/catalog.py` | 217 | `core/catalog.py` | вызов `ozon.get_client` уходит в `CatalogSource` площадки |
| `app/product_sets.py` | 270 | `core/product_sets.py` | |
| `app/report.py` | 320 | `core/report.py` | |
| `app/accounts.py` | 251 | `core/accounts.py` | `MARKETPLACES`, список таблиц в `delete`, `_reset_clients`, озоновский `.env` в `credentials` — всё из реестра |
| `app/deps.py` | 295 | `core/deps.py` | три `require_*_account` → один `require_market(код)`; `nav_counters` → реестр |
| `app/access.py` `security.py` `crypto.py` `version.py` | | `core/…` | без изменений |
| `app/ozon.py` | 349 | `ozon/client.py` | |
| `app/avito.py` | 430 | `avito/client.py` | |
| `app/yandex.py` | 297 | `yandex/client.py` | |
| `app/avito_pack.py` | 365 | `avito/pack.py` | |
| `app/yandex_pack.py` | 619 | `yandex/pack.py` | |
| `app/routes/auth.py` | 113 | `routes/auth.py` | |
| `app/routes/pack.py` + `orders.py` | 189 + 109 | `ozon/routes.py` | |
| `app/routes/avito.py` | 541 | `avito/routes.py` (~430) | страница `/avito/returns`, её печать и raw — в общий раздел |
| `app/routes/yandex.py` | 305 | `yandex/routes.py` | |
| `app/routes/returns.py` | 520 | `routes/returns.py` (~430) + `ozon/returns.py` | `api_giveout`, `_places` — озоновские; `MARK_TABLES`, `_avito_returns`, `_accounts_by_marketplace` — заменяются реестром |
| `app/routes/products.py` | 184 | `routes/products.py` | **[исправлено]** сейчас весь на `require_ozon_account` |
| `app/routes/reports.py` | 146 | `routes/reports.py` | |
| `app/routes/admin.py` | 594 | `routes/admin.py` (~480) + `ozon/routes.py` | **[исправлено]** три озоновских маршрута: `/api/postings/{n}/reset`, `/api/returns/statuses`, `/api/returns/received-statuses` |
| `templates/pack.html` `avito_pack.html` `yandex_pack.html` | | `templates/market_pack.html` | один шаблон |
| `templates/orders.html` `avito.html` `yandex.html` | | `markets/*/templates/orders.html` | списки разные — остаются свои |
| `templates/returns.html` + `avito_returns.html` | | `templates/returns.html` | один раздел |
| `templates/returns_print.html` + `avito_returns_print.html` | | `templates/returns_print.html` | один лист |
| `templates/settings.html` | | `templates/settings.html` + `ozon/templates/settings.html` | блоки «Печать стикеров», «Какие возвраты загружать/считать полученными» — озоновские, уходят во фрагмент |
| остальные шаблоны | | `templates/…` | без изменений |
| `static/pack.js` `avito_pack.js` `yandex_pack.js` | 332+183+250 | `static/market_pack.js` + `markets/*/static/pack.js` | общий скелет; у площадки только отрисовка позиции |
| `static/orders.js` `avito.js` `yandex.js` | | `markets/*/static/orders.js` | |
| `static/returns.js` + `avito_returns.js` | | `static/returns.js` | |
| остальная статика | | `static/…` | без изменений |
| `tests/fakes.py` | | `tests/fakes/{__init__,ozon,avito,yandex}.py` | |
| `tests/test_packing.py` `test_catalog.py` `test_product_sets.py` | | `tests/markets/ozon/` | |
| `tests/test_avito.py` `test_avito_pack.py` | | `tests/markets/avito/` | |
| `tests/test_yandex.py` `test_yandex_pack.py` | | `tests/markets/yandex/` | |
| `tests/test_labels.py` | | делится между `ozon/` и `avito/` | |
| остальные тесты | | `tests/core/` | |

Шаблоны и статика площадки подключаются из её пакета: Jinja2 `PrefixLoader`
(`"ozon/orders.html"` → `markets/ozon/templates/orders.html`) и
`app.mount("/static/ozon", …)` — оба из реестра в `main.py`.

## Куда деваются двадцать развилок

| Сейчас | Станет |
|---|---|
| `accounts.MARKETPLACES` | `registry.MARKETS` |
| `accounts.delete` — перечень таблиц | `market.tables` |
| `accounts.credentials` — `.env` только для Ozon | `market.env_keys` |
| `sync.sync_account` — if avito / if yandex | `market.sync(account)` |
| `deps.nav_counters` — три ветки с SQL | `market.nav(account)` |
| `deps.require_ozon/avito/yandex_account` | `require_market(код)` |
| `main.index`, `auth.switch` — домашние страницы | `market.home` |
| `main` — перечисление роутеров | цикл по реестру |
| `admin._probe`, `api_test_account` | `market.ping()` |
| `admin.settings_page` — плитки | `market.stats(account)` |
| `settings.html` — блоки только для Ozon | `market.settings_panel` |
| `labels.pending_ozon/avito/yandex` | `market.pending_labels` |
| `db.SCHEMA` — DDL всех площадок | конкатенация `market.schema` |
| `db._migrate` — миграции площадок | `market.migrate(conn)` |
| `returns.MARK_TABLES`, `_accounts_by_marketplace`, `return_acts.rows_of` | цикл по `market.returns` |
| `report.to_csv`, шаблоны отчётов — название площадки | `registry.get(code).title` |
| `base.html` — три ветки меню | цикл по реестру |

## Ошибки в текущем коде, которые переезд закрывает

Найдены при проверке; исправлять по одной сейчас смысла нет — они исчезают
сами, когда общий код перестаёт знать площадки по имени.

1. **«Товары» → 409 в кабинетах Avito и Яндекса.** Ссылка в шапке показывается
   всем (`base.html:70`), а раздел требует кабинет Ozon
   (`routes/products.py`, все маршруты). В новой структуре ссылка есть у
   площадки с объявленным `catalog`, у остальных её нет.
2. **Акты недоступны из кабинета Avito.** Строки Avito в акты попадают
   (`return_acts.rows_of`), но вкладка «Ждёт подтверждения» живёт на `/returns`,
   а он требует Ozon (`routes/returns.py:134`). У кого только Avito — актов не
   видит. В новой структуре раздел один и кабинета не требует.
3. **Два адреса за одним разделом.** `/returns` и `/avito/returns` показывали
   одно и то же разными шаблонами; лист печати был продублирован.
4. **Возврат Avito не доживал до акта.** Пропав из выдачи, заказ просто
   удалялся, и акта по нему не выходило вовсе. Теперь пропажа из пункта выдачи
   и есть факт получения: строка помечается полученной и ждёт акта — как у Ozon
   по статусу «Получен».

## Этапы перехода

Каждый заканчивается зелёным прогоном и отдельным коммитом. После любого можно
остановиться — код рабочий.

1. ✅ **Переезд по пакетам.** `markets/<код>/` — клиент, сборка, роуты как есть.
   `core/` — общие модули как есть. Логика не менялась, только пути и импорты.
2. ✅ **Реестр.** `Market`, `registry.py`, объявления трёх площадок. Убраны
   двадцать развилок.
3. ✅ **Расшивка общих файлов.** `store`, `sync`, `labels`, DDL, миграции,
   `options`, озоновская часть `return_acts` и `routes/returns` разъехались по
   площадкам.
4. ✅ **Общие страницы.** Один раздел возвратов (`/returns` для всех площадок,
   `ReturnsSource` у каждой), одно рабочее место (`market_pack.html` +
   `market_pack.js`), шаблоны и статика площадок — в их пакетах.
5. ✅ **Тесты по папкам** зеркально коду: `tests/core/`, `tests/markets/<код>/`,
   подделки клиентов — по файлу на площадку.

Затем — Wildberries, сразу в новой структуре.

## Что даёт Wildberries

Сейчас новая площадка — это 12 правок в общих файлах и 5 новых. После — один
пакет `markets/wildberries/` (пять–шесть файлов, шаблон списка заказов, два
скрипта) и одна строка в `registry.py`. Общий код не трогается, значит, сломать
Ozon, Avito или Яндекс новой площадкой невозможно.
