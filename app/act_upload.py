"""Разбор акта выдачи, загруженного руками.

Обычно акт приходит сам, методами /v1/return/giveout/*. Но метод включён не у
всех продавцов, у части кабинетов он отвечает отказом, а бывает и так, что акт
есть на бумаге или в личном кабинете, а через API его не видно. Тогда
администратор загружает файл акта, и панель собирает раздел «Ждёт
подтверждения» из него.

Разбираем не по структуре файла, а по содержимому: вытаскиваем из него все
похожие на код строки и ищем среди них штрихкоды возвратов этого кабинета. Так
один и тот же разбор работает и для PDF из личного кабинета, и для выгрузки в
Excel, и для CSV, и для сырого ответа API — подстраиваться под формат каждого
документа Ozon пришлось бы бесконечно.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile

log = logging.getLogger("act_upload")

# Сколько весит самый большой разумный акт. Ограничение нужно до чтения файла:
# иначе панель попробует поднять в память всё, что прислали.
MAX_BYTES = 10 * 1024 * 1024

# Похожее на код: буквы и цифры, длиной от четырёх — короче штрихкодов и
# номеров не бывает, а слова из текста акта в поиск попадать ни к чему.
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/]{3,}")

PDF_HELP = (
    "Для разбора PDF нужна библиотека pypdf, а она не установлена. Поставьте "
    "зависимости панели: sudo -u ozon /opt/ozon-pack/.venv/bin/pip install "
    "-r /opt/ozon-pack/requirements.txt — и перезапустите службу. Либо "
    "загрузите акт в CSV, XLSX или JSON."
)


class UploadRejected(ValueError):
    """Файл не годится: пустой, слишком большой или не читается."""


def _decode(data: bytes) -> str:
    """Текст файла. Выгрузки из Excel часто приходят в CP1251."""
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _from_xlsx(data: bytes) -> str:
    """Текст книги Excel без сторонних библиотек.

    XLSX — это zip с XML внутри: строки лежат в общей таблице sharedStrings,
    числа прямо в ячейках. Ради одного чтения тянуть в установку целую
    библиотеку незачем.
    """
    from xml.etree import ElementTree

    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        names = book.namelist()
        for name in names:
            if not (name.startswith("xl/") and name.endswith(".xml")):
                continue
            if not (name == "xl/sharedStrings.xml" or name.startswith("xl/worksheets/")):
                continue
            try:
                root = ElementTree.fromstring(book.read(name))
            except ElementTree.ParseError:
                continue
            for node in root.iter():
                tag = node.tag.rsplit("}", 1)[-1]
                if tag in ("t", "v") and node.text:
                    parts.append(node.text)
    return "\n".join(parts)


def _from_pdf(data: bytes) -> str:
    """Текст PDF. Библиотека подключается здесь, а не наверху модуля.

    Разбор PDF нужен одной кнопке, и его отсутствие не должно мешать загружать
    акт в других форматах — тем более ронять панель.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise UploadRejected(PDF_HELP) from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - библиотека кидает своё на битом файле
        raise UploadRejected(f"PDF не читается: {exc}") from exc


def _from_json(text: str) -> str:
    """Сырой ответ API: собираем все значения, вложенность не важна."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise UploadRejected(f"JSON не разбирается: {exc}") from exc
    parts: list[str] = []
    stack = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, (str, int, float)) and not isinstance(current, bool):
            parts.append(str(current))
    return "\n".join(parts)


def _from_table(text: str) -> str:
    """CSV или TSV: разделитель определяем сами, он бывает и «;»."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        rows = csv.reader(io.StringIO(text), dialect)
    except csv.Error:
        rows = csv.reader(io.StringIO(text), delimiter=";")
    return "\n".join(" ".join(cell for cell in row if cell) for row in rows)


def extract_text(filename: str, data: bytes) -> str:
    """Текст загруженного файла — из того формата, в котором он пришёл."""
    if not data:
        raise UploadRejected("Файл пустой")
    if len(data) > MAX_BYTES:
        raise UploadRejected(f"Файл больше {MAX_BYTES // (1024 * 1024)} МБ — это не акт выдачи")

    name = (filename or "").lower()
    if data[:4] == b"%PDF" or name.endswith(".pdf"):
        return _from_pdf(data)
    if data[:2] == b"PK" or name.endswith((".xlsx", ".xlsm")):
        return _from_xlsx(data)

    text = _decode(data)
    stripped = text.lstrip()
    if name.endswith(".json") or stripped[:1] in ("{", "["):
        return _from_json(text)
    if name.endswith((".csv", ".tsv")):
        return _from_table(text)
    return text


def codes(text: str) -> list[str]:
    """Похожие на коды строки из текста акта, по одному разу каждая."""
    seen: dict[str, None] = {}
    for match in TOKEN_RE.finditer(text or ""):
        seen.setdefault(match.group(0), None)
    return list(seen)


def parse(filename: str, data: bytes) -> list[str]:
    """Коды из файла акта — то, что дальше ищется среди возвратов кабинета."""
    found = codes(extract_text(filename, data))
    if not found:
        raise UploadRejected(
            "В файле не нашлось ни одного кода. Загрузите акт выдачи Ozon — "
            "PDF из личного кабинета, выгрузку в CSV или XLSX либо ответ API в JSON."
        )
    return found
