"""Фильтр кабинетов — один на все разделы.

Переключателя кабинетов в шапке нет: что показывать, решает фильтр на самой
странице. Чипы — «Все» и по одному на каждый кабинет, в том же порядке, что
в «Настройках»; точка на чипе — цвет площадки, чтобы два «Магазина» разных
площадок не путались. Выбор живёт в адресе (`?shop=2`), поэтому страницу
можно обновить, отправить другому и вернуться к ней кнопкой «Назад».

Какие кабинеты у раздела есть, решает сам раздел: у «Возвратов» — те, чья
площадка отдаёт возвраты, у «Журнала» — все.
"""
from __future__ import annotations

from collections.abc import Callable

from . import accounts

# «Все кабинеты»: фильтр не выбран.
ALL = "all"


def _registry():
    from ..markets import registry

    return registry


def live(fits: Callable | None = None) -> list[dict]:
    """Включённые кабинеты с ключами; fits(площадка) — есть ли у неё этот раздел."""
    out = []
    for account in accounts.all_accounts(active_only=True):
        if not accounts.is_configured(account):
            continue
        market = _registry().get(account["marketplace"])
        if market is None or (fits is not None and not fits(market)):
            continue
        out.append(account)
    return out


def picked_of(value, candidates: list[dict]) -> str:
    """Кабинет из адреса. Незнакомый, выключенный или без ключей — «Все».

    Кривая ссылка не должна оставлять человека с пустым экраном.
    """
    wanted = str(value or ALL).strip()
    if wanted != ALL and any(str(account["id"]) == wanted for account in candidates):
        return wanted
    return ALL


def narrow(candidates: list[dict], picked: str) -> list[dict]:
    """Кабинеты под фильтром."""
    if picked == ALL:
        return list(candidates)
    return [account for account in candidates if str(account["id"]) == picked]


def chips(candidates: list[dict], picked: str, counts: dict[int, int] | None,
          href: Callable[[str], str], *, all_title: str = "Все кабинеты") -> list[dict]:
    """Чипы фильтра: «Все» и по кабинету. counts=None — без чисел."""
    def count(ids):
        return None if counts is None else sum(counts.get(account_id, 0) for account_id in ids)

    out = [{
        "id": ALL, "title": all_title, "market": None, "market_title": "",
        "count": count([account["id"] for account in candidates]),
        "href": href(ALL), "active": picked == ALL,
    }]
    for account in candidates:
        market = _registry().get(account["marketplace"])
        out.append({
            "id": str(account["id"]), "title": account["title"],
            "market": market.code if market else "", "market_title": market.title if market else "",
            "count": count([account["id"]]),
            "href": href(str(account["id"])), "active": picked == str(account["id"]),
        })
    return out
