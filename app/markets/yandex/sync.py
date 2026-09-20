"""Загрузка данных кабинета Яндекс Маркета: заказы в работе."""
from __future__ import annotations


def sync_account(account: dict, *, returns: bool = True) -> dict:  # noqa: ARG001 - возвратов у Маркета в панели нет
    from ...core import sync as core_sync

    return core_sync.sync_yandex(account)
