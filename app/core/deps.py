"""Общие зависимости FastAPI: текущий пользователь, CSRF, шаблоны."""
from __future__ import annotations

import hmac
import re
from datetime import datetime, timezone

import jinja2
from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from . import access
from .config import BASE_DIR, settings
from .version import build_label


def _template_env() -> jinja2.Environment:
    """Шаблоны ядра плюс шаблоны площадок под своим префиксом.

    «ozon/returns_list.html» — это app/markets/ozon/templates/returns_list.html.
    Так кусок страницы лежит рядом с кодом, который его наполняет, а общие
    страницы не превращаются в список из трёх веток «если это Ozon».

    Каталоги ищем на диске, а не через реестр площадок: шаблоны нужны раньше,
    чем площадки успевают объявиться, и ядро по именам их не знает.
    """
    markets = BASE_DIR / "app" / "markets"
    by_market = {
        path.parent.name: jinja2.FileSystemLoader(str(path))
        for path in sorted(markets.glob("*/templates")) if path.is_dir()
    }
    loader = jinja2.ChoiceLoader([
        jinja2.FileSystemLoader(str(BASE_DIR / "app" / "templates")),
        jinja2.PrefixLoader(by_market),
    ])
    return jinja2.Environment(loader=loader, autoescape=jinja2.select_autoescape())


templates = Jinja2Templates(env=_template_env())

# Выбранный кабинет держим в отдельной куке: менять его может любой вошедший,
# поэтому переподписывать сессию (и сбрасывать CSRF) на каждое переключение незачем.
ACCOUNT_COOKIE = "ozp_account"


def current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


def current_account(request: Request) -> dict | None:
    """Кабинет текущей вкладки: из куки, иначе первый включённый."""
    from . import accounts

    cached = getattr(request.state, "account", None)
    if cached is not None:
        return cached
    account = accounts.resolve(request.cookies.get(ACCOUNT_COOKIE))
    request.state.account = account
    return account


def require_account(request: Request) -> dict:
    account = current_account(request)
    if not account:
        raise HTTPException(status_code=503, detail="Не добавлен ни один кабинет — откройте «Настройки»")
    return account


def require_market(code: str):
    """Зависимость «текущий кабинет — этой площадки».

    Разделы площадки открываются только в её кабинете: заказы Avito в
    кабинете Ozon не существуют, и молча показать пустоту хуже, чем отказать.
    """

    def dependency(request: Request) -> dict:
        account = require_account(request)
        if account["marketplace"] != code:
            from ..markets import registry

            market = registry.get(code)
            title = market.title if market else code
            raise HTTPException(
                status_code=409, detail=f"Этот раздел работает только с кабинетами площадки «{title}»"
            )
        return account

    dependency.__name__ = f"require_market_{code}"
    return dependency


def require_manager(request: Request) -> dict:
    """Настройка панели: сотрудники, доступы, ключи, статусы.

    Это про роль, а не про раздел: галочкой такое не выдаётся. Кому нужно
    настраивать панель — тот администратор, и роль у него должна быть честная.
    """
    user = current_user(request)
    if not access.is_manager(user):
        raise HTTPException(status_code=403, detail="Доступно владельцу и администратору")
    return user


def require_owner(request: Request) -> dict:
    user = current_user(request)
    if not access.is_owner(user):
        raise HTTPException(status_code=403, detail="Доступно только владельцу")
    return user


def require_section(section: str):
    """Зависимость «этот раздел человеку выдан».

    Разделы решают, какие экраны человек видит, — это отдельно от роли. Сборщик
    с выданным разделом «Отчёты» их открывает; администратор без него — нет.
    """

    def dependency(request: Request) -> dict:
        user = current_user(request)
        if not access.can(user, section):
            raise HTTPException(
                status_code=403,
                detail=f"Раздел «{access.SECTION_LABELS.get(section, section)}» вам не выдан",
            )
        return user

    dependency.__name__ = f"require_section_{section}"
    return dependency


def check_csrf(request: Request) -> None:
    """Защита от запросов со сторонних сайтов."""
    session = getattr(request.state, "session", None) or {}
    token = request.headers.get("X-CSRF-Token") or ""
    expected = str(session.get("csrf") or "")
    # compare_digest вместо != : сравнение не выдаёт длину совпавшего начала.
    if not token or not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Недействительный CSRF-токен")


# Имя файла приходит из ответа площадки и уходит в заголовок Content-Disposition.
# Кавычка разорвала бы заголовок, перевод строки — весь ответ, а слэш увёл бы
# файл в чужой каталог при сохранении. Поэтому оставляем только простые символы.
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(raw: object, default: str = "label.pdf") -> str:
    """Имя файла площадки, пригодное для заголовка ответа."""
    name = _UNSAFE_IN_FILENAME.sub("_", str(raw or ""))[:100].strip()
    if not name or not name.strip("._"):
        return default
    return name


def local_dt(value: str | None, fmt: str = "%d.%m %H:%M") -> str:
    """ISO-8601 UTC ->локальное время склада (TZ_OFFSET_HOURS)."""
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    shifted = moment.astimezone(timezone.utc).timestamp() + settings.timezone_offset * 3600
    return datetime.fromtimestamp(shifted, tz=timezone.utc).strftime(fmt)


templates.env.filters["local_dt"] = local_dt
templates.env.globals["settings"] = settings


def panel_has_catalog(request: Request = None) -> bool:  # noqa: ARG001 - зовётся из шаблона
    """Показывать ли «Товары»: есть ли в панели хоть один кабинет с каталогом.

    Раздел общий на все кабинеты, а не на текущий: сборщик Avito сканирует те же
    коробки, и каталог ему нужен ровно так же, хотя своих карточек Avito не
    отдаёт. Прятать раздел от него значило бы прятать склад.
    """
    from ..core import accounts
    from ..markets import registry

    for account in accounts.all_accounts():
        market = registry.get(account["marketplace"])
        if market is not None and market.catalog is not None:
            return True
    return False


def market_nav(request: Request) -> list:
    """Пункты меню: свои у площадки текущего кабинета и общие «Заказы».

    «Заказы» — раздел на все кабинеты сразу, поэтому пункт ставит ядро, и
    значки на нём считаются по всем кабинетам, а не по выбранному в шапке.
    Место — сразу за «Сборкой», как раньше стояли «Заказы» площадок.
    """
    from ..markets import registry
    from ..markets.base import NavItem
    from . import board

    account = current_account(request)
    market = registry.get(account["marketplace"]) if account else None
    items = list(market.nav(account)) if market else []
    if not items:
        return items
    orders = NavItem("/orders", "Заказы", "orders", "orders", board.badges())
    at = next((index + 1 for index, item in enumerate(items) if item.section == "pack"), 0)
    items.insert(at, orders)
    return items


def static_version() -> str:
    """Метка версии статики — по времени изменения файлов в app/static.

    Без неё браузер сборщика может месяцами держать закешированный скрипт и не
    увидеть исправление.
    """
    global _static_version
    if _static_version is None:
        static_dir = BASE_DIR / "app" / "static"
        try:
            newest = max(path.stat().st_mtime for path in static_dir.iterdir() if path.is_file())
        except (OSError, ValueError):
            newest = 0
        _static_version = str(int(newest))
    return _static_version


_static_version: str | None = None


def account_ready(request: Request) -> bool:
    """Есть ли у текущего кабинета ключи — иначе панели нечего показывать."""
    from . import accounts

    return accounts.is_configured(current_account(request))


def account_switcher(request: Request) -> dict:
    """Данные для переключателя кабинетов в шапке."""
    from . import accounts

    return {
        "current": current_account(request),
        "accounts": accounts.all_accounts(active_only=True),
    }


templates.env.globals["build_label"] = build_label
templates.env.globals["market_nav"] = market_nav
templates.env.globals["panel_has_catalog"] = panel_has_catalog
templates.env.globals["account_ready"] = account_ready
templates.env.globals["account_switcher"] = account_switcher
templates.env.globals["static_version"] = static_version


def print_setup() -> dict:
    """Какой документ на какой принтер QZ Tray и лист. Пусто — печать через браузер."""
    from . import printers

    return printers.setup()


templates.env.globals["print_setup"] = print_setup
# Шапка рисуется по разделам, а не по роли: иначе сборщик с выданными
# «Отчётами» просто не увидит на них ссылки.
templates.env.globals["can_see"] = access.can
templates.env.globals["role_label"] = access.role_label


def _marketplace_title(marketplace: str | None) -> str:
    from . import accounts

    return accounts.marketplace_title(marketplace or "ozon")


# Название площадки по её коду — для отчётов, где строки идут из разных кабинетов.
templates.env.globals["marketplace_title"] = _marketplace_title
# «Настраивает панель» — это владелец или администратор. Сравнивать роль со
# строкой в шаблоне нельзя: владелец под такое сравнение не подходит.
templates.env.globals["is_manager"] = access.is_manager
# Снять подтверждение и удалить акт — только владельцу: это единственные
# действия, которые стирают уже принятую работу.
templates.env.globals["is_owner"] = access.is_owner


