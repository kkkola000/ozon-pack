"""Подготовка задания для принтера этикеток на языке TSPL.

TSPL понимают Xprinter (XP-420B и родственные), TSC, Godex и другие. Картинка
передаётся командой BITMAP, где бит 1 — белая точка, 0 — чёрная: обычное
изображение приходится инвертировать.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_DPI = 203
DOTS_PER_MM = DEFAULT_DPI / 25.4


@dataclass
class PrinterConfig:
    """Настройки принтера этикеток."""

    host: str = ""
    port: int = 9100
    dpi: int = DEFAULT_DPI
    gap_mm: float = 2.0
    gap_offset_mm: float = 0.0
    direction: int = 1
    copies: int = 1
    invert: bool = False
    threshold: int = 160
    speed: int | None = None
    density: int | None = None

    @property
    def max_width_dots(self) -> int:
        # У четырёхдюймовых принтеров печатное поле — 104..108 мм
        return int(108 * self.dpi / 25.4)


def pack_bitmap(gray: bytes, width: int, height: int, *, threshold: int = 160, invert: bool = False) -> tuple[bytes, int]:
    """8-битные оттенки серого -> упакованные биты TSPL. Возвращает (данные, ширина в байтах)."""
    if width <= 0 or height <= 0:
        raise ValueError("Пустое изображение")
    if len(gray) != width * height:
        raise ValueError(f"Ожидалось {width * height} байт, получено {len(gray)}")

    width_bytes = (width + 7) // 8
    out = bytearray(width_bytes * height)
    for y in range(height):
        row_start = y * width
        out_start = y * width_bytes
        # Незаполненный хвост строки оставляем белым (биты 1)
        for byte_index in range(width_bytes):
            value = 0
            for bit in range(8):
                x = byte_index * 8 + bit
                if x < width:
                    dark = gray[row_start + x] < threshold
                    if invert:
                        dark = not dark
                    # 1 — белая точка, 0 — чёрная
                    pixel = 0 if dark else 1
                else:
                    pixel = 1
                value = (value << 1) | pixel
            out[out_start + byte_index] = value
    return bytes(out), width_bytes


def build_label_job(
    pages: list[tuple[bytes, int, int]],
    config: PrinterConfig,
    *,
    width_mm: float,
    height_mm: float,
) -> bytes:
    """Полное задание печати: по странице на этикетку.

    pages — список (серые пиксели, ширина, высота).
    """
    if not pages:
        raise ValueError("Нет страниц для печати")

    chunks: list[bytes] = []
    for gray, width, height in pages:
        if width > config.max_width_dots:
            raise ValueError(
                f"Ширина этикетки {width} точек больше печатного поля принтера ({config.max_width_dots})"
            )
        data, width_bytes = pack_bitmap(gray, width, height, threshold=config.threshold, invert=config.invert)

        header = [
            f"SIZE {width_mm:.0f} mm,{height_mm:.0f} mm",
            f"GAP {config.gap_mm:g} mm,{config.gap_offset_mm:g} mm",
            f"DIRECTION {config.direction},0",
            "REFERENCE 0,0",
        ]
        if config.speed:
            header.append(f"SPEED {config.speed}")
        if config.density is not None:
            header.append(f"DENSITY {config.density}")
        header.append("CLS")

        chunks.append(("\r\n".join(header) + "\r\n").encode("ascii"))
        chunks.append(f"BITMAP 0,0,{width_bytes},{height},0,".encode("ascii"))
        chunks.append(data)
        chunks.append(f"\r\nPRINT {max(1, config.copies)},1\r\n".encode("ascii"))
    return b"".join(chunks)


def build_test_job(config: PrinterConfig, *, text: str = "OZON PACK") -> bytes:
    """Тестовая этикетка встроенным шрифтом — проверить связь и подачу ленты."""
    lines = [
        "SIZE 75 mm,120 mm",
        f"GAP {config.gap_mm:g} mm,{config.gap_offset_mm:g} mm",
        f"DIRECTION {config.direction},0",
        "REFERENCE 0,0",
        "CLS",
        'TEXT 30,60,"3",0,1,1,"OZON PACK"',
        f'TEXT 30,140,"2",0,1,1,"{text[:32]}"',
        'TEXT 30,200,"2",0,1,1,"TEST PRINT / TESTOVAYA PECHAT"',
        'BARCODE 30,260,"128",80,1,0,2,2,"OZONPACK-TEST"',
        f'TEXT 30,380,"2",0,1,1,"{config.dpi} dpi, {config.host}:{config.port}"',
        "PRINT 1,1",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")
