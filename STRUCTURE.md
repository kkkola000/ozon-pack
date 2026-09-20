# Структура проекта

Дерево репозитория с пояснением, за что отвечает каждый файл. Обновляйте этот
файл, когда добавляете новый модуль, — иначе он быстро разойдётся с кодом.

> Это текущая раскладка. Куда она переезжает перед добавлением Wildberries —
> в `ARCHITECTURE.md`: целевое дерево, карта переезда каждого файла и этапы.

```
ozon-pack/
├── README.md                       — полная документация: кабинеты, разделы, API площадок, установка, безопасность
├── STRUCTURE.md                    — этот файл
├── VERSION                         — номер версии сборки (читается в «Настройках» и /healthz)
├── COMMIT                          — git-хэш коммита, подставляется автоматически при `git archive`
├── requirements.txt                — зависимости для продакшена (fastapi, uvicorn, httpx, cryptography, fpdf2, pypdf…)
├── requirements-dev.txt            — requirements.txt + pytest, только для запуска тестов
├── pytest.ini                      — где лежат тесты, тихий вывод (-q)
├── ruff.toml                       — правила линтера и обоснование, почему часть выключена
├── .env.example                    — шаблон .env: ключи площадок, порт, IP-ограничения, логика сборки
├── .gitignore                      — что не попадает в репозиторий (.env, data/, *.db, кэши)
├── .gitattributes                  — COMMIT export-subst: хэш коммита подставляется при экспорте
│
├── app/                            — само приложение (FastAPI)
│   ├── __init__.py                 — пустой, делает app пакетом
│   ├── main.py                     — сборка приложения: middleware проверки IP/сессии, /healthz, редирект «/» по кабинету, подключение всех роутеров
│   ├── config.py                   — Settings: чтение .env, ключи Ozon/Avito/Маркета по умолчанию, лимиты сборки, часовой пояс
│   ├── db.py                       — SQLite: схема таблиц, автомиграции (добавление колонок), запись событий в журнал
│   ├── security.py                 — пароли (хэш), подпись сессии в cookie, защита от подбора пароля, CSRF-токен
│   ├── crypto.py                   — шифрование ключей площадок (Api-Key Ozon, client_secret Avito, токен Маркета) в базе
│   ├── access.py                   — роли (владелец/админ/сборщик) и разделы, которые роль видит в панели
│   ├── accounts.py                 — кабинеты: список площадок (MARKETPLACES), CRUD кабинетов, чьи ключи сейчас активны
│   ├── deps.py                     — общие зависимости FastAPI: текущий пользователь/кабинет, require_*_account, CSRF, счётчики в шапке
│   ├── store.py                    — разбор ответов площадок в строки БД (upsert_posting / upsert_avito_order / upsert_yandex_order) и вычисляемые поля для шаблонов (posting_view, avito_view, yandex_view)
│   ├── sync.py                     — фоновая синхронизация: тянет заказы/товары/возвраты у всех кабинетов по таймеру и по кнопке «Обновить»
│   ├── ozon.py                     — клиент Ozon Seller API: отправления, товары, возвраты, стикеры
│   ├── avito.py                    — клиент Avito API: заказы, статусы, этикетки
│   ├── yandex.py                   — клиент Partner API Яндекс Маркета: заказы, ярлыки
│   ├── packing.py                  — рабочее место сборщика Ozon: сканирование, подбор отправления, наборы, отчёт
│   ├── avito_pack.py               — рабочее место сборщика Avito: стикер открывает заказ, штрихкод товара пишется в отчёт
│   ├── yandex_pack.py              — рабочее место сборщика Маркета: по образцу Ozon, штрихкоды берутся из каталога Ozon по артикулу
│   ├── labels.py                   — выгрузка стикеров/этикеток/ярлыков архивом на компьютер и замок на сборку, пока не выгружены
│   ├── catalog.py                  — полная загрузка каталога кабинета (не только то, что встретилось в заказах) — для раздела «Товары»
│   ├── product_sets.py             — наборы: из каких частей физически собирается товар площадки
│   ├── report.py                   — отчёт об отгруженных товарах: когда появляется строка, выгрузка в CSV
│   ├── return_acts.py              — акты получения возвратов (по поездкам в пункт выдачи)
│   ├── returns_pdf.py              — лист возвратов файлом PDF
│   ├── options.py                  — настройки, задаваемые из интерфейса и хранимые в БД (переживают обновление кода)
│   ├── version.py                  — версия сборки и git-коммит для /healthz
│   │
│   ├── routes/                     — HTTP-маршруты, по одному файлу на раздел
│   │   ├── __init__.py
│   │   ├── auth.py                 — вход, выход, переключение кабинета
│   │   ├── pack.py                 — рабочее место сборщика Ozon (/pack, /api/scan, /api/labels/archive.zip…)
│   │   ├── orders.py               — заказы FBS (/orders): сборка в Ozon, печать стикеров
│   │   ├── avito.py                — заказы Avito, сборка Avito, возвраты Avito
│   │   ├── yandex.py               — заказы Маркета, сборка Маркета
│   │   ├── returns.py              — возвраты FBO/FBS Ozon, акты, печать листа
│   │   ├── products.py             — раздел «Товары»: каталог и наборы
│   │   ├── reports.py              — отчёты об отгрузке, CSV
│   │   └── admin.py                — журнал событий, настройки, пользователи, кабинеты и ключи площадок
│   │
│   ├── templates/                  — HTML-страницы (Jinja2)
│   │   ├── base.html               — общий каркас: шапка, навигация по разделам, переключатель кабинетов
│   │   ├── login.html              — форма входа
│   │   ├── error.html              — страница ошибки (403/404/500…)
│   │   ├── pack.html               — рабочее место сборщика Ozon
│   │   ├── orders.html             — список заказов FBS
│   │   ├── avito.html              — заказы Avito (вкладки «Подтвердите» / «Отправьте» / «Собранные»)
│   │   ├── avito_pack.html         — рабочее место сборщика Avito
│   │   ├── avito_returns.html      — возвраты Avito
│   │   ├── avito_returns_print.html— лист возвратов Avito для печати
│   │   ├── yandex.html             — заказы Маркета (вкладки «Ожидает сборки» / «Ожидает отгрузки» / «Собранные»)
│   │   ├── yandex_pack.html        — рабочее место сборщика Маркета
│   │   ├── returns.html            — возвраты FBO/FBS Ozon
│   │   ├── returns_print.html      — лист возвратов Ozon для печати
│   │   ├── products.html           — каталог и наборы
│   │   ├── logs.html               — журнал событий
│   │   ├── reports.html            — список дней отгрузки
│   │   ├── report_day.html         — отчёт за один день + ссылка на CSV
│   │   ├── report_return_acts.html — список актов возвратов
│   │   ├── report_return_act.html  — один акт целиком
│   │   ├── settings.html           — кабинеты, пользователи, параметры сборки
│   │   ├── _return_acts.html       — фрагмент: вкладка «Ждёт подтверждения» (акты по датам)
│   │   └── _return_mark.html       — фрагмент: окно отметки о возврате (общее для Ozon и Avito)
│   │
│   └── static/                     — CSS и JS без сборки, отдаются как есть
│       ├── app.css                 — общее оформление панели
│       ├── app.js                  — общие утилиты: запросы к API, тосты, звук, печать PDF
│       ├── pack.js                 — рабочее место сборщика Ozon
│       ├── avito.js                — список заказов Avito
│       ├── avito_pack.js           — рабочее место сборщика Avito
│       ├── avito_returns.js        — возвраты Avito
│       ├── yandex.js               — список заказов Маркета
│       ├── yandex_pack.js          — рабочее место сборщика Маркета
│       ├── orders.js               — список заказов FBS
│       ├── products.js             — наборы товаров
│       ├── returns.js              — составление актов возвратов
│       ├── return_mark.js          — окно отметки о возврате
│       ├── act_admin.js            — вернуть акт в работу / удалить (только владелец)
│       └── code128.js              — рисование штрихкода Code128 для листа возвратов
│
├── deploy/                         — установка и обслуживание на сервере
│   ├── install.sh                  — автоустановка и обновление одной командой прямо с GitHub
│   ├── setup.sh                    — установка сразу с HTTPS (install.sh + ssl.sh за один вызов)
│   ├── ssl.sh                      — nginx + сертификат (Let's Encrypt, IP или самоподписанный)
│   ├── vpn-only.sh                 — ограничение входа только из подсети VPN
│   └── ozon-pack.service           — systemd-юнит для запуска без Docker
│
└── tests/                          — pytest, тестами покрыта вся логика сборки
    ├── __init__.py
    ├── conftest.py                 — общие фикстуры: изолированная БД на каждый тест, демо-кабинеты
    ├── fakes.py                    — подделки клиентов Ozon/Avito/Маркета: детерминированные данные без сети
    ├── pdfstub.py                  — минимальный генератор PDF для подделок (стикеры/этикетки/ярлыки)
    ├── code128.py                  — кодирование Code128 для подделок и проверок штрихкода
    ├── test_access.py              — роли и разделы
    ├── test_accounts.py            — кабинеты, ключи, разделение данных между магазинами
    ├── test_api.py                 — HTTP-слой: доступ, CSRF, основные страницы
    ├── test_avito.py               — заказы Avito
    ├── test_avito_pack.py          — сборка заказов Avito
    ├── test_barcode.py             — Code128 и разбор вариантов штрихкода
    ├── test_catalog.py             — полная загрузка каталога кабинета
    ├── test_deploy.py              — синтаксис и логика deploy-скриптов
    ├── test_errors.py              — как панель сообщает об ошибках (кодировка, формат ответа)
    ├── test_labels.py              — выгрузка стикеров/этикеток и замок на сборку
    ├── test_migration.py           — обновление старой базы (без account_id) до текущей схемы
    ├── test_packing.py             — сценарии рабочего места сборщика Ozon
    ├── test_product_sets.py        — наборы товаров
    ├── test_report.py              — отчёт об отгруженных товарах
    ├── test_return_acts.py         — акты получения возвратов
    ├── test_returns_mark.py        — отметка о возврате и PDF-лист
    ├── test_store.py               — разбор ответов площадок и вычисляемые поля
    ├── test_version.py             — версия сборки в /healthz
    ├── test_yandex.py              — заказы Яндекс Маркета
    └── test_yandex_pack.py         — сборка заказов Маркета
```

## Как это соотносится с разделами панели

| Раздел в панели | Роут | Логика | Шаблон | JS |
|---|---|---|---|---|
| Сборка (Ozon) | `app/routes/pack.py` | `app/packing.py` | `pack.html` | `pack.js` |
| Заказы FBS | `app/routes/orders.py` | `app/packing.py` (сборка в Ozon) | `orders.html` | `orders.js` |
| Заказы Avito + Сборка Avito | `app/routes/avito.py` | `app/avito_pack.py`, `app/avito.py` | `avito.html`, `avito_pack.html` | `avito.js`, `avito_pack.js` |
| Заказы Маркета + Сборка Маркета | `app/routes/yandex.py` | `app/yandex_pack.py`, `app/yandex.py` | `yandex.html`, `yandex_pack.html` | `yandex.js`, `yandex_pack.js` |
| Возвраты FBO/FBS | `app/routes/returns.py` | `app/return_acts.py`, `app/returns_pdf.py` | `returns.html` | `returns.js` |
| Товары | `app/routes/products.py` | `app/catalog.py`, `app/product_sets.py` | `products.html` | `products.js` |
| Отчёты | `app/routes/reports.py` | `app/report.py` | `reports.html`, `report_day.html` | — |
| Журнал / Настройки | `app/routes/admin.py` | `app/access.py`, `app/accounts.py`, `app/options.py` | `logs.html`, `settings.html` | `act_admin.js` |

Общее для всех разделов: `app/main.py` (сборка приложения), `app/deps.py`
(доступ и текущий кабинет), `app/db.py` + `app/store.py` (данные), `app/sync.py`
(фоновая загрузка), `app/security.py` + `app/crypto.py` (вход и шифрование
ключей), `app/templates/base.html` + `app/static/app.js` (общий каркас).
