"""Отрисовка PDF в картинки.

Нужна ради Safari: печать PDF во фрейме там даёт пустой лист, а обычную
HTML-страницу с картинкой браузер печатает без нареканий. Библиотека
необязательная — без неё панель просто печатает исходный PDF.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .pngwriter import write_png

log = logging.getLogger("pdfrender")

MM_PER_POINT = 25.4 / 72
DEFAULT_DPI = 203  # типичное разрешение термопринтера этикеток


@dataclass
class RenderedPage:
    png: bytes
    width_mm: float
    height_mm: float


def is_available() -> bool:
    try:
        import pypdfium2  # noqa: F401
    except Exception:  # noqa: BLE001 - библиотека необязательная
        return False
    return True


def render_pdf(pdf_bytes: bytes, *, dpi: int = DEFAULT_DPI, max_pages: int = 50) -> list[RenderedPage]:
    """PDF -> список страниц в PNG. Пустой список, если рендер недоступен."""
    try:
        import pypdfium2 as pdfium
    except Exception as exc:  # noqa: BLE001
        log.info("Рендер PDF недоступен (%s) — печатаем исходный PDF", exc)
        return []

    pages: list[RenderedPage] = []
    document = pdfium.PdfDocument(pdf_bytes)
    try:
        for index, page in enumerate(document):
            if index >= max_pages:
                break
            width_pt, height_pt = page.get_size()
            bitmap = page.render(scale=dpi / 72, rev_byteorder=True, draw_annots=True)
            buffer = bytes(bitmap.buffer)
            width, height, stride = bitmap.width, bitmap.height, bitmap.stride
            channels = bitmap.n_channels

            # Строки в буфере могут быть выровнены — собираем плотный массив.
            rows = [buffer[row * stride : row * stride + width * channels] for row in range(height)]
            dense = b"".join(rows)
            # Этикетка чёрно-белая, pdfium сглаживает по серому: каналы совпадают,
            # поэтому берём один и втрое уменьшаем объём картинки.
            gray = dense[::channels] if channels > 1 else dense

            pages.append(
                RenderedPage(
                    png=write_png(gray, width, height, grayscale=True),
                    width_mm=round(width_pt * MM_PER_POINT, 1),
                    height_mm=round(height_pt * MM_PER_POINT, 1),
                )
            )
    finally:
        document.close()
    return pages
