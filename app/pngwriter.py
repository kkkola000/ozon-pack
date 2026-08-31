"""Запись PNG без внешних зависимостей (нужен только zlib из стандартной библиотеки)."""
from __future__ import annotations

import struct
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_png(pixels: bytes, width: int, height: int, *, grayscale: bool = True) -> bytes:
    """pixels — построчный массив без выравнивания: 1 байт на пиксель (серый) или 3 (RGB)."""
    channels = 1 if grayscale else 3
    expected = width * height * channels
    if len(pixels) != expected:
        raise ValueError(f"Ожидалось {expected} байт пикселей, получено {len(pixels)}")

    stride = width * channels
    raw = bytearray()
    for row in range(height):
        raw.append(0)  # фильтр строки: без предсказания
        raw += pixels[row * stride : (row + 1) * stride]

    color_type = 0 if grayscale else 2
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )
