"""GS1 parsing for supported complete profiles; no legacy imports or print dependencies."""

from __future__ import annotations

import re
from dataclasses import dataclass

GS = "\x1d"
# Some scanners send the separator as printable text, including a backslash
# before the angle bracket. This is scanner notation, not JSON escaping.
TEXT_GS = re.compile(r"(?:\\)?<gs>", re.IGNORECASE)
START = re.compile(r"^01(\d{14})21")
# Supported compact profile: 01 + GTIN(14) + 21 + serial(13) + 93 + check(4).
# Fixed positions only; do not split arbitrary serials at the first "93".
COMPACT_93 = re.compile(r"01\d{14}21[\x20-\x7e]{13}93[\x20-\x7e]{4}")
# Known 83-character profile without separators: serial(13), key(4),
# base64 signature(44). Restore GS only at these fixed field boundaries.
JOINED_91_92 = re.compile(r"01\d{14}21[\x20-\x7e]{13}91[\x20-\x7e]{4}92[A-Za-z0-9+/]{43}=")
MAX_CODE = 512


@dataclass(frozen=True)
class Code:
    raw: str
    gtin: str
    serial: str
    fields: tuple[tuple[str, str], ...]
    compact: bool = False


def looks_like(value: str) -> bool:
    value = TEXT_GS.sub(GS, value)
    return value.startswith(("]d2", "]d8")) or bool(START.match(value.lstrip(GS)))


def parse(value: str) -> Code:
    # CR/LF — терминатор сканера, ]d2/8 — идентификатор символики, не данные.
    raw = value.rstrip("\r\n")
    if raw.startswith(("]d2", "]d8")):
        raw = raw[3:]
    try:
        return _parse(raw)
    except ValueError:
        # Preserve an already valid payload: the literal characters <gs>
        # can themselves be part of a serial number. Only retry scanner
        # notation when parsing the original bytes failed.
        converted = TEXT_GS.sub(GS, raw)
        if converted == raw:
            raise
        return _parse(converted)


def _parse(raw: str) -> Code:
    raw = raw.removeprefix(GS)
    if not raw or len(raw) > MAX_CODE or any(c != GS and not 32 <= ord(c) <= 126 for c in raw):
        raise ValueError("Нужен полный DataMatrix Честный знак без посторонних управляющих символов")
    if JOINED_91_92.fullmatch(raw):
        raw = raw[:31] + GS + raw[31:37] + GS + raw[37:]
    first, *tail = raw.split(GS)
    compact = not tail and COMPACT_93.fullmatch(raw) is not None
    if compact:
        first, tail = raw[:31], [raw[31:]]
    match = START.match(first)
    if not match or not 1 <= len(first[18:]) <= 20:
        raise ValueError("В коде «Честный знак» не найдены корректные GTIN и серийный номер")
    gtin, serial = match.group(1), first[18:]
    check = (10 - sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(gtin[:-1]))) % 10) % 10
    if check != int(gtin[-1]):
        raise ValueError("Некорректная контрольная цифра GTIN в коде «Честный знак»")
    fields = [("01", gtin), ("21", serial)]
    for part in tail:
        ai = "240" if part.startswith("240") else part[:2]
        if len(part) <= len(ai) or ai not in {"240", "91", "92", "93"}:
            raise ValueError(
                "Не удалось разобрать полный код «Честный знак»: проверьте разделители GS сканера"
            )
        fields.append((ai, part[len(ai) :]))
    ais = [ai for ai, _ in fields]
    if len(set(ais)) != len(ais) or not ("93" in ais or "91" in ais and "92" in ais):
        raise ValueError(
            "Не удалось определить крипточасть Честный знак: код неполный или его формат не поддерживается"
        )
    return Code(raw, gtin, serial, tuple(fields), compact)


def comparison_key(value):
    """For comparisons only: never replace the stored or transmitted string."""
    if not isinstance(value, str):
        return None
    try:
        return parse(value).fields
    except ValueError:
        return value


def equivalent(left, right) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return parse(left).fields == parse(right).fields
    except ValueError:
        return False


def short(value: str) -> str:
    code = parse(value)
    return "01" + code.gtin + "21" + code.serial
