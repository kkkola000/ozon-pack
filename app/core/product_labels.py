"""ШК товара перед стикером отправления, в одном задании существующей печати."""
from __future__ import annotations

import io

from . import db, product_links
from .returns_pdf import barcode_svg

MARKET_PREFIXES = ('OZN', 'MRKT')
MAX_COPIES = 500
KV_ENABLED = "print_product_barcodes"


def enabled() -> bool:
    return db.kv_get(KV_ENABLED, "0") == "1"


def _codes(account_id: int, sku: str) -> list[str]:
    row = db.query_one('SELECT barcodes FROM products WHERE account_id = ? AND sku = ?', (account_id, sku))
    values = db.json_list(row['barcodes']) if row else []
    values += [item['barcode'] for item in db.query(
        'SELECT barcode FROM product_barcodes WHERE account_id = ? AND sku = ? ORDER BY barcode', (account_id, sku),
    )]
    # Текущий Code128B поддерживает печатаемый ASCII. Не удаляем символы из
    # неподдерживаемого кода: иначе на бумаге получится другой товарный код.
    return list(dict.fromkeys(str(value).strip() for value in values
                              if value and str(value).strip() and all(32 <= ord(c) <= 126 for c in str(value).strip())))


def choose(account_id: int, sku: str, prefix: str = '') -> str | None:
    own = _codes(account_id, sku)
    if prefix:
        generated = next((code for code in own if code.upper().startswith(prefix.upper())), None)
        if generated:
            return generated
    if own:
        return own[0]
    main = product_links.main_of(account_id, sku)
    if main:
        return next((code for code in _codes(main['account_id'], main['sku'])
                     if not code.upper().startswith(MARKET_PREFIXES)), None)
    return None


def codes_for_order(account: dict, order_id: str) -> list[str]:
    from ..markets import registry

    market = registry.get(account['marketplace'])
    source = market.labels if market else None
    if not source or not source.items:
        return []
    codes = []
    for item in source.items(account['id'], order_id):
        sku = str(item.get('sku') or '')
        if not sku:
            continue
        code = choose(account['id'], sku, source.barcode_prefix)
        if code:
            quantity = max(0, int(item.get('quantity') or 0))
            if len(codes) + quantity > MAX_COPIES:
                raise ValueError('За один раз можно напечатать не больше 500 товарных штрихкодов')
            codes.extend([code] * quantity)
    return codes


def prepend(account: dict, order_id: str, shipment: bytes) -> bytes:
    """Одна страница на единицу товара, затем все оригинальные листы отправления.

    Размер страницы повторяет стикер отправления. Выбор принтера, QZ Tray,
    подгонка и браузерная печать продолжают работать с тем же документом.
    При отсутствии подходящих кодов возвращаем исходный файл без изменений.
    """
    if not enabled():
        return shipment
    codes = codes_for_order(account, order_id)
    if not codes:
        return shipment
    from fpdf import FPDF
    from pypdf import PdfReader, PdfWriter

    original = PdfReader(io.BytesIO(shipment))
    if not original.pages:
        raise ValueError('Площадка прислала PDF без страниц')
    first = original.pages[0]
    width, height = float(first.mediabox.width) * 25.4 / 72, float(first.mediabox.height) * 25.4 / 72
    labels = FPDF(unit='mm', format=(width, height))
    labels.set_auto_page_break(False)
    labels.set_margins(0, 0, 0)
    margin = min(6, width * .1)
    bar_height = min(25, height * .5)
    y = max(2, (height - bar_height - 7) / 2)
    for code in codes:
        labels.add_page()
        labels.image(io.BytesIO(barcode_svg(code)), x=margin, y=y, w=width - margin * 2, h=bar_height)
        labels.set_font('Helvetica', size=10)
        while labels.get_string_width(code) > width - margin * 2 and labels.font_size_pt > 5:
            labels.set_font_size(labels.font_size_pt - 1)
        labels.set_xy(margin, y + bar_height + 1)
        labels.cell(width - margin * 2, 5, code, align='C')
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(bytes(labels.output()))))
    writer.append(original)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def merge(documents: list[bytes]) -> bytes:
    if len(documents) == 1:
        return documents[0]
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for document in documents:
        writer.append(PdfReader(io.BytesIO(document)))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
