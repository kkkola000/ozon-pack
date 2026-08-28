"""Минимальный генератор PDF без внешних зависимостей.

Используется только в демо-режиме (стикер отправления и лист выдачи возвратов):
в боевом режиме PDF присылает Ozon. Кириллица транслитерируется — чтобы не
тащить встраивание шрифтов ради демонстрационной этикетки.
"""
from __future__ import annotations

from .barcode import bars, total_modules

MM = 72.0 / 25.4

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def translit(text: str) -> str:
    out = []
    for char in text or "":
        lower = char.lower()
        if lower in _TRANSLIT:
            replacement = _TRANSLIT[lower]
            out.append(replacement.upper() if char.isupper() else replacement)
        elif 32 <= ord(char) <= 126:
            out.append(char)
        else:
            out.append(" ")
    return "".join(out)


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


class _Content:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def text(self, x: float, y: float, size: float, value: str, *, bold: bool = False) -> None:
        font = "/F2" if bold else "/F1"
        self.parts.append(f"BT {font} {size:.1f} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({_escape(translit(value))}) Tj ET")

    def rect(self, x: float, y: float, width: float, height: float, *, fill: bool = True) -> None:
        self.parts.append(f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re {'f' if fill else 'S'}")

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.5) -> None:
        self.parts.append(f"{width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def barcode(self, data: str, x: float, y: float, width: float, height: float) -> None:
        modules = total_modules(data)
        if not modules:
            return
        unit = width / modules
        for offset, module_width, dark in bars(data):
            if dark:
                self.rect(x + offset * unit, y, module_width * unit, height)

    def build(self) -> bytes:
        return "\n".join(self.parts).encode("latin-1", "replace")


def _pdf(pages: list[tuple[float, float, bytes]]) -> bytes:
    """Собрать PDF из страниц (ширина, высота, поток содержимого)."""
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)

    font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    pages_id = add(b"")  # заполним после создания страниц
    page_ids: list[int] = []
    for width, height, content in pages:
        stream_id = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
        page_id = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, width, height, font_regular, font_bold, stream_id)
        )
        page_ids.append(page_id)
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    objects[pages_id - 1] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_ids), kids)
    catalog_id = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % index + obj + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog_id,
        xref_at,
    )
    return bytes(out)


def make_label_pdf(pages: list[dict]) -> bytes:
    """Демо-стикер отправления 75×120 мм."""
    width, height = 75 * MM, 120 * MM
    rendered = []
    for page in pages:
        c = _Content()
        c.rect(4, 4, width - 8, height - 8, fill=False)
        c.text(10, height - 22, 9, "DEMO / ТЕСТОВЫЙ СТИКЕР")
        c.line(8, height - 28, width - 8, height - 28)
        c.text(10, height - 48, 13, page.get("tpl") or "Ozon", bold=True)
        c.text(10, height - 66, 10, page.get("city") or "")
        c.text(10, height - 82, 9, page.get("warehouse") or "")
        c.line(8, height - 92, width - 8, height - 92)
        number = page["posting_number"]
        c.barcode(number, 12, height - 150, width - 24, 46)
        c.text(14, height - 164, 12, number, bold=True)
        c.line(8, height - 176, width - 8, height - 176)
        c.text(10, height - 192, 9, f"Заказ: {page.get('order_number', '')}")
        y = height - 210
        for name, quantity in page.get("products", [])[:6]:
            c.text(10, y, 8, f"- {name[:34]} x{quantity}")
            y -= 12
        rendered.append((width, height, c.build()))
    return _pdf(rendered)


def make_giveout_pdf(code: str) -> bytes:
    """Демо-штрихкод на выдачу возвратов, A4."""
    width, height = 210 * MM, 297 * MM
    c = _Content()
    c.text(40, height - 60, 18, "ВЫДАЧА ВОЗВРАТОВ (DEMO)", bold=True)
    c.text(40, height - 84, 11, "Покажите штрихкод сотруднику пункта выдачи")
    c.barcode(code, 40, height - 190, width - 80, 80)
    c.text(40, height - 208, 14, code, bold=True)
    return _pdf([(width, height, c.build())])
