"""Разбор пачки стикеров на файлы: общая механика выгрузки.

Площадка отдаёт стикеры пачкой, панель раскладывает их по одному файлу на
отправление. Что делать, когда страниц больше номеров или площадка отказала на
середине, — решается здесь, одинаково для всех площадок.
"""
import io
import zipfile

from app.core import labels


def names_in(archive: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        return sorted(zf.namelist())


# ------------------------------------------------------------- разбор пачки
def test_a_batch_with_extra_pages_stays_one_file():
    """Страниц больше, чем номеров, — раскладывать нечем, кладём как есть.

    У отправления бывает два места, и какая страница чья, площадка не говорит.
    Один файл с верным содержимым лучше десяти с чужими стикерами.
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=200, height=300)
    buffer = io.BytesIO()
    writer.write(buffer)

    assert labels._split(buffer.getvalue(), ["A-1", "B-2"]) is None
    archive, saved = labels.build_archive(
        ["A-1", "B-2"], lambda batch: buffer.getvalue(), prefix="стикеры")
    assert saved == ["A-1", "B-2"]
    assert names_in(archive) == ["стикеры-A-1-B-2.pdf"]


def test_a_failed_batch_does_not_lose_the_rest(monkeypatch):
    """Площадка отказала на пачке — остальные стикеры всё равно выгружаются.

    Из-за одного отказа сборщик остался бы вообще без архива. Что не вышло,
    останется без отметки и попадёт в следующую выгрузку.
    """
    from pypdf import PdfWriter

    monkeypatch.setattr(labels, "BATCH", 2)
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=300)
    writer.add_blank_page(width=200, height=300)
    buffer = io.BytesIO()
    writer.write(buffer)

    def fetch(batch):
        if batch[0] == "C-3":
            raise RuntimeError("Ozon отказал")
        return buffer.getvalue()

    archive, saved = labels.build_archive(
        ["A-1", "B-2", "C-3", "D-4"], fetch, prefix="стикеры")
    assert saved == ["A-1", "B-2"], "выгруженным отмечено то, чего не было"
    assert names_in(archive) == ["A-1.pdf", "B-2.pdf"]
