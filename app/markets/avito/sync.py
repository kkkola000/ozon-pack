"""Загрузка данных кабинета Avito: заказы в рабочих статусах и возвраты к выдаче."""
from __future__ import annotations


def sync_account(account: dict, *, returns: bool = True) -> dict:  # noqa: ARG001 - возвраты приходят с заказами
    from ...core import sync as core_sync

    return core_sync.sync_avito(account)
