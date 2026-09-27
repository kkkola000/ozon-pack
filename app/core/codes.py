"""Варианты одного отсканированного кода.

Сканер читает одну и ту же наклейку по-разному: EAN-13 с ведущим нулём и без,
UPC-12, GTIN из DataMatrix «Честного знака», номер с дефисами и одними цифрами.
Поиск по каталогу, заказам и возвратам сравнивает со всеми вариантами сразу —
правило одно на всю панель, поэтому живёт в ядре.
"""
from __future__ import annotations

import re


def barcode_variants(code: str) -> list[str]:
    """Варианты одного и того же кода: EAN с ведущим нулём, GTIN из «Честного знака»."""
    code = (code or "").strip()
    if not code:
        return []
    variants = [code]
    digits = re.sub(r"\D", "", code)

    # DataMatrix маркировки: 01<GTIN-14>21<серийный номер>...
    if len(code) >= 16 and code[:2] == "01" and code[2:16].isdigit():
        gtin = code[2:16]
        variants += [gtin, gtin.lstrip("0"), gtin[1:] if gtin.startswith("0") else gtin]

    if digits and digits != code:
        variants.append(digits)
    if len(digits) == 13 and digits.startswith("0"):
        variants.append(digits[1:])
    if len(digits) == 12:
        variants.append("0" + digits)
    if len(digits) == 14 and digits.startswith("0"):
        variants.append(digits[1:])

    seen: list[str] = []
    for variant in variants:
        if variant and variant not in seen:
            seen.append(variant)
    return seen
