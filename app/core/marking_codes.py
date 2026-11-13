"""Полный GS1 DataMatrix ЧЗ: не теряем GS/криптохвост и проверяем дубликат."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

GS = '\x1d'
START = re.compile(r'^01(\d{14})21')
MAX_CODE = 512


@dataclass(frozen=True)
class Code:
    raw: str
    gtin: str
    serial: str
    fields: tuple[tuple[str, str], ...]


def looks_like(value: str) -> bool:
    return value.startswith((']d2', ']d8')) or bool(START.match(value.lstrip(GS)))


def parse(value: str) -> Code:
    # CR/LF — терминатор сканера, ]d2/8 — идентификатор символики, не данные.
    raw = value.rstrip('\r\n')
    if raw.startswith((']d2', ']d8')):
        raw = raw[3:]
    raw = raw.removeprefix(GS)
    if not raw or len(raw) > MAX_CODE or any(c != GS and not 32 <= ord(c) <= 126 for c in raw):
        raise ValueError('Нужен полный DataMatrix ЧЗ без посторонних управляющих символов')
    first, *tail = raw.split(GS)
    match = START.match(first)
    if not match or not 1 <= len(first[18:]) <= 20:
        raise ValueError('В ЧЗ не найдены корректные GTIN и серийный номер')
    gtin, serial = match.group(1), first[18:]
    check = (10 - sum(int(c) * (3 if i % 2 == 0 else 1)
                      for i, c in enumerate(reversed(gtin[:-1]))) % 10) % 10
    if check != int(gtin[-1]):
        raise ValueError('Некорректная контрольная цифра GTIN в ЧЗ')
    fields = [('01', gtin), ('21', serial)]
    for part in tail:
        ai = '240' if part.startswith('240') else part[:2]
        if len(part) <= len(ai) or ai not in {'240', '91', '92', '93'}:
            raise ValueError('Не удалось разобрать полный код ЧЗ: проверьте разделители GS сканера')
        fields.append((ai, part[len(ai):]))
    ais = [ai for ai, _ in fields]
    if len(set(ais)) != len(ais) or not ('93' in ais or '91' in ais and '92' in ais):
        raise ValueError('В ЧЗ отсутствует криптохвост. Настройте сканер на передачу полного кода и GS')
    return Code(raw, gtin, serial, tuple(fields))


def image(code: Code):
    """GS1 с FNC1 и quiet zone. Декодирование обязано вернуть те же байты."""
    import zxingcpp
    from PIL import Image

    # Разделители представления выбираем так, чтобы данные нельзя было принять за AI.
    values = ''.join(value for _, value in code.fields)
    if '[' not in values and ']' not in values:
        text = ''.join(f'[{ai}]{value}' for ai, value in code.fields)
    elif '(' not in values and ')' not in values:
        text = ''.join(f'({ai}){value}' for ai, value in code.fields)
    else:
        raise ValueError('Не удалось безопасно подготовить дубликат этого ЧЗ; исходный код сохранён')
    symbol = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.DataMatrix, gs1=True, force_square=True)
    raster = symbol.to_image(scale=8, add_quiet_zones=True)
    result = Image.frombytes('L', (raster.shape[1], raster.shape[0]), bytes(raster))
    decoded = zxingcpp.read_barcode(result)
    if not decoded or decoded.content_type != zxingcpp.ContentType.GS1 or decoded.bytes != code.raw.encode('ascii'):
        raise ValueError('Контроль дубликата ЧЗ не пройден: печать остановлена, исходный код сохранён')
    return result


def pdf(codes: list[Code], width: float = 58, height: float = 40) -> bytes:
    from fpdf import FPDF

    if not codes or len(codes) > 100:
        raise ValueError('Выберите от 1 до 100 кодов ЧЗ')
    if width < 30 or height < 30:
        raise ValueError('Для ЧЗ нужна этикетка не меньше 30×30 мм')
    out = FPDF(unit='mm', format=(width, height))
    out.set_auto_page_break(False)
    for code in codes:
        png = io.BytesIO()
        image(code).save(png, format='PNG')
        side = min(30, width - 6, height - 11)
        out.add_page()
        out.image(png, x=(width - side) / 2, y=2, w=side, h=side)
        out.set_font('Helvetica', size=7)
        out.set_xy(2, side + 3)
        out.cell(width - 4, 3, code.gtin, align='C')
        out.set_xy(2, side + 6)
        out.cell(width - 4, 3, code.serial, align='C')
    return bytes(out.output())
