"""Лист возвратов файлом PDF.

Тот же лист, что открывается кнопкой «Печать листа», только готовым файлом:
его можно сохранить, отправить водителю в мессенджер и напечатать там, где
браузер печатать отказывается или печатает не то. Состав колонок тот же, что
у HTML-листа, — два листа одного дня не должны расходиться.

Лист состоит из секций по площадкам. Как выглядит таблица каждой — знает
площадка (ReturnsSource.pdf_table); здесь только рамка: заголовок, итоги,
подписи, предупреждение об усечённом списке.

Шрифт нужен свой: встроенные в PDF шрифты кириллицу не показывают. Берём
DejaVu (в Debian и Ubuntu это пакет fonts-dejavu-core), при его отсутствии —
Liberation или FreeFont. Если не нашлось ничего, честно говорим об этом, а не
отдаём лист с пустыми клетками вместо названий товаров.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

# fpdf2 подключается не здесь, а внутри сборки листа. Библиотека нужна одной
# кнопке, и её отсутствие не должно ронять панель целиком: без неё сборщик
# по-прежнему должен входить, сканировать и печатать лист из браузера.
# Однажды это уже уронило вход на сервере, где обновились без pip install.

# Code128B: ширины штрихов по значению символа. Таблица та же, что в
# static/code128.js, — лист на бумаге и лист на экране должны кодировать
# штрихкод одинаково, иначе в пункте выдачи не отсканируется один из них.
CODE128_PATTERNS = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 "
    "221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 "
    "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 "
    "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 "
    "231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 "
    "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 "
    "114131 311141 411131 211412 211214 211232 2331112"
).split()

# Пары «обычное начертание, полужирное». Первая найденная и берётся.
FONT_CANDIDATES = (
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf"),
    ("FreeSans.ttf", "FreeSansBold.ttf"),
)
FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/freefont",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/liberation",
    "/usr/local/share/fonts",
    "/usr/share/fonts",
)

FONT_HELP = (
    "Для PDF нужен шрифт с кириллицей, а в системе его нет. "
    "Поставьте его одной командой: sudo apt install -y fonts-dejavu-core "
    "— и попробуйте снова. Лист по-прежнему можно напечатать из браузера "
    "кнопкой «Печать листа»."
)


LIBRARY_HELP = (
    "Для листа в PDF нужна библиотека fpdf2, а она не установлена. "
    "Поставьте зависимости панели: sudo -u ozon /opt/ozon-pack/.venv/bin/pip install "
    "-r /opt/ozon-pack/requirements.txt — и перезапустите службу. Лист по-прежнему "
    "печатается из браузера кнопкой «Печать листа»."
)


class PdfUnavailable(RuntimeError):
    """Лист в PDF собрать нечем: нет библиотеки или шрифта."""


class FontMissing(PdfUnavailable):
    """В системе нет шрифта, которым можно набрать русский текст."""


def _find_fonts() -> tuple[Path, Path]:
    """Обычное и полужирное начертания одного шрифта.

    Путь можно задать и вручную, переменной RETURNS_PDF_FONT: на непривычной
    системе проще указать файл, чем угадывать, куда её сборка кладёт шрифты.
    """
    forced = os.getenv("RETURNS_PDF_FONT", "").strip()
    if forced:
        regular = Path(forced)
        if not regular.is_file():
            raise FontMissing(f"RETURNS_PDF_FONT указывает на {forced}, а такого файла нет. {FONT_HELP}")
        bold = regular.with_name(regular.name.replace("Regular", "Bold").replace(".ttf", "-Bold.ttf"))
        return regular, (bold if bold.is_file() else regular)

    for directory in FONT_DIRS:
        base = Path(directory)
        if not base.is_dir():
            continue
        for regular_name, bold_name in FONT_CANDIDATES:
            regular = base / regular_name
            if regular.is_file():
                bold = base / bold_name
                return regular, (bold if bold.is_file() else regular)
    raise FontMissing(FONT_HELP)


def barcode_svg(data: str, height: float = 22, module: float = 0.62) -> bytes:
    """Code128B картинкой: её и вставляем в клетку таблицы."""
    values = [104] + [ord(ch) - 32 for ch in str(data) if 32 <= ord(ch) <= 126]
    checksum = (values[0] + sum(i * v for i, v in enumerate(values) if i)) % 103
    values += [checksum, 106]
    widths = [int(digit) for value in values for digit in CODE128_PATTERNS[value]]

    bars, position, dark = [], 0.0, True
    for width in widths:
        if dark:
            bars.append(
                f'<rect x="{position * module:.2f}" y="0" '
                f'width="{width * module:.2f}" height="{height}" fill="#000"/>'
            )
        position += width
        dark = not dark
    total = position * module
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total:.2f}" height="{height}" '
        f'viewBox="0 0 {total:.2f} {height}">{"".join(bars)}</svg>'
    ).encode()


def cut(value: object, limit: int) -> str:
    """Длинное название не должно расталкивать колонки на пол-листа."""
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def mark_cell(row: dict) -> str:
    """Отметка так, чтобы её было видно и на чёрно-белой печати."""
    sign = row.get("mark_sign") or "☐"
    label = row.get("mark_label") or ""
    return f"{sign} {label}".strip()


_SHEET_CLASS = None


def _sheet_class():
    """Класс листа строится при первом обращении.

    fpdf2 нужен одной кнопке, поэтому импортируем его здесь, а не наверху
    модуля: иначе отсутствие библиотеки роняет всю панель, включая вход и
    сканирование. Однажды так и вышло на сервере, где обновились без
    pip install.
    """
    global _SHEET_CLASS
    if _SHEET_CLASS is not None:
        return _SHEET_CLASS
    try:
        from fpdf import FPDF
    except ImportError as exc:  # noqa: F841
        raise PdfUnavailable(LIBRARY_HELP) from exc

    class _Sheet(FPDF):
        """Лист с колонтитулом: без номеров страниц пачку легко перепутать."""

        def __init__(self, title: str, subtitle: str) -> None:
            super().__init__(orientation="L", unit="mm", format="A4")
            self.title_text = title
            self.subtitle = subtitle
            regular, bold = _find_fonts()
            self.add_font("sheet", "", str(regular))
            self.add_font("sheet", "B", str(bold))
            self.set_auto_page_break(auto=True, margin=12)
            self.set_margins(8, 8, 8)

        def header(self) -> None:
            self.set_font("sheet", "B", 13)
            self.cell(0, 6, self.title_text, new_x="LMARGIN", new_y="NEXT")
            self.set_font("sheet", "", 8)
            self.cell(0, 5, self.subtitle, new_x="LMARGIN", new_y="NEXT")
            self.ln(2)

        def footer(self) -> None:
            self.set_y(-10)
            self.set_font("sheet", "", 7)
            self.cell(0, 5, f"Страница {self.page_no()} из {{nb}}", align="R")

        def section(self, name: str) -> None:
            self.ln(3)
            self.set_font("sheet", "B", 10)
            self.cell(0, 5, name, new_x="LMARGIN", new_y="NEXT")
            self.ln(1)

    _SHEET_CLASS = _Sheet
    return _SHEET_CLASS


def build_sheet(
    sections: list[dict],
    *,
    user: dict,
    printed_at: datetime,
    everywhere: bool = False,
    account: dict | None = None,
    subtitle: str = "",
    truncated: bool = False,
    act: dict | None = None,
) -> bytes:
    """Собрать лист возвратов. Возвращает готовый PDF байтами.

    sections — секции по площадкам: {label, unit, rows, pieces, pdf_table}.
    act — печатаем акт получения, а не лист к выдаче: те же строки, но уже с
    отметками, и в заголовке видно, за какую поездку этот акт.
    """
    if act:
        title = act["title"]
    else:
        title = "Возвраты к выдаче"
        if everywhere:
            title += " · все кабинеты"
        elif account:
            title += f" · {account['title']}"
        if subtitle:
            title += f" · {subtitle}"

    parts = [f"{s['label']}: {len(s['rows'])} {s['unit']}, {s['pieces']} шт." for s in sections if s["rows"]]
    if not parts:
        parts.append("Ничего не готово к выдаче")
    if act:
        parts.append(f"принято {act['marked_ok']}, не принято {act['marked_bad']}")
        if act["unmarked"]:
            parts.append(f"без отметки {act['unmarked']}")
        # Откуда акт: собрался по статусу «Получен» или загружен за число.
        # На бумаге это единственный след происхождения документа.
        if act.get("source_label"):
            parts.append(act["source_label"])
    parts.append(f"Сформировал: {user.get('login', '—')}")
    parts.append(f"{printed_at.strftime('%d.%m.%Y %H:%M')} UTC")

    pdf = _sheet_class()(title, " · ".join(parts))
    pdf.set_title(title)
    pdf.alias_nb_pages()
    pdf.add_page()

    filled = [s for s in sections if s["rows"]]
    for index, section in enumerate(filled):
        # Заголовок секции нужен, когда площадок на листе больше одной или лист общий.
        if everywhere or len(filled) > 1 or index > 0:
            pdf.section(section["label"])
        section["pdf_table"](pdf, section["rows"], everywhere)
    if not filled:
        pdf.set_font("sheet", "", 10)
        pdf.ln(6)
        pdf.cell(0, 6, "Ни одного возврата, готового к выдаче.", new_x="LMARGIN", new_y="NEXT")

    if truncated:
        pdf.ln(4)
        pdf.set_font("sheet", "B", 9)
        pdf.multi_cell(
            0, 5,
            "Внимание: в лист поместилась только часть возвратов. Остальные придётся "
            "распечатать отдельно по кабинетам — иначе они останутся в пункте выдачи.",
            border=1, new_x="LMARGIN", new_y="NEXT",
        )

    pdf.ln(6)
    pdf.set_font("sheet", "", 9)
    half = (pdf.w - pdf.l_margin - pdf.r_margin) / 2 - 4
    pdf.cell(half, 6, "Выдал (ПВЗ): ______________________ / подпись")
    pdf.cell(8, 6, "")
    pdf.cell(half, 6, "Принял (сборщик): __________________ / подпись",
             new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


def filename(printed_at: datetime, *, everywhere: bool, account: dict | None) -> str:
    """Имя файла: по нему в папке загрузок видно, чей лист и за какой день.

    Только латиница и цифры: название кабинета русское, а русское имя файла
    часть браузеров и почтовых клиентов превращает в кашу.
    """
    stamp = printed_at.strftime("%Y-%m-%d-%H%M")
    if everywhere:
        return f"vozvraty-vse-kabinety-{stamp}.pdf"
    if account:
        return f"vozvraty-kabinet-{account['id']}-{stamp}.pdf"
    return f"vozvraty-{stamp}.pdf"
