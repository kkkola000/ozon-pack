"""Загрузка данных кабинета Ozon: отправления, каталог, возвраты."""
from __future__ import annotations


def sync_account(account: dict, *, returns: bool = True) -> dict:
    """Один проход по кабинету. Возвраты — по отдельному расписанию, поэтому флагом."""
    from ...core import sync as core_sync

    result: dict = {}
    result.update(core_sync.sync_postings(account))
    result.update(core_sync.sync_products(account))
    if returns:
        result.update(core_sync.sync_returns(account))
    return result
