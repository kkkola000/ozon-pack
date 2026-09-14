"""Лист возвратов файлом PDF.

Тот же лист, что открывается кнопкой «Печать листа», только готовым файлом:
его можно сохранить, отправить водителю в мессенджер и напечатать там, где
браузер печатать отказывается или печатает не то. Состав колонок тот же, что
у HTML-листа, — два листа одного дня не должны расходиться.

Шрифт нужен свой: встроенные в PDF шрифты кириллицу не показывают. Берём
DejaVu (в Debian и Ubuntu это пакет fonts-dejavu-core), при его отсутствии —
Liberation или FreeFont. Если не нашлось ничего, честно говорим об этом, а не
отдаём лист с пустыми клетками вместо названий товаров.
"""
from __future__ import annotations

import io
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


def _cut(value: object, limit: int) -> str:
    """Длинное название не должно расталкивать колонки на пол-листа."""
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mark_cell(row: dict) -> str:
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


def _ozon_table(pdf, items: list[dict], *, everywhere: bool) -> None:
    headings = ["№"]
    widths = [8.0]
    if everywhere:
        headings.append("Кабинет")
        widths.append(22.0)
    headings += ["Возврат", "Схема", "Штрихкод", "Товар", "Артикул / SKU", "Кол-во",
                 "Пункт выдачи", "Отметка", "Комментарий"]
    widths += [30.0, 12.0, 32.0, 48.0, 24.0, 14.0, 36.0, 20.0, 33.0]
    # Раскладываем по ширине листа: иначе на A4 таблица уезжает за поля.
    free = pdf.w - pdf.l_margin - pdf.r_margin
    scale = free / sum(widths)
    widths = [width * scale for width in widths]

    pdf.set_font("sheet", "", 7)
    with pdf.table(col_widths=tuple(widths), first_row_as_headings=True,
                   line_height=4, padding=1.2, repeat_headings=1) as table:
        head = table.row()
        for name in headings:
            head.cell(name)
        for index, item in enumerate(items, start=1):
            row = table.row()
            row.cell(str(index))
            if everywhere:
                row.cell(_cut(item.get("account_title") or "—", 22))
            barcode = str(item.get("barcode") or "").strip()
            # Номер штрихкода печатаем рядом с самим кодом: не считался сканером —
            # в пункте выдачи набьют руками, а не поедут за листом заново.
            row.cell("\n".join(x for x in (str(item.get("id")), item.get("order_number"), barcode) if x))
            row.cell(item.get("type") or item.get("scheme") or "—")
            if barcode:
                row.cell(img=io.BytesIO(barcode_svg(barcode)), img_fill_width=True)
            else:
                row.cell("—")
            row.cell(_cut(item.get("product_name") or "Без названия", 70))
            row.cell(f"{item.get('offer_id') or '—'}\n{item.get('sku') or ''}".strip())
            row.cell(str(item.get("quantity") or ""))
            row.cell(_cut(
                " · ".join(x for x in (item.get("place_name"), item.get("place_address")) if x) or "—", 60
            ))
            row.cell(_mark_cell(item))
            row.cell(_cut(item.get("note"), 60))


def _avito_table(pdf, orders: list[dict], *, everywhere: bool) -> None:
    headings = ["№"]
    widths = [8.0]
    if everywhere:
        headings.append("Кабинет")
        widths.append(22.0)
    headings += ["Заказ", "Трек возврата", "Товары", "Кол-во", "Куда ехать", "Отметка", "Комментарий"]
    widths += [30.0, 32.0, 62.0, 14.0, 42.0, 20.0, 33.0]
    free = pdf.w - pdf.l_margin - pdf.r_margin
    scale = free / sum(widths)
    widths = [width * scale for width in widths]

    pdf.set_font("sheet", "", 7)
    with pdf.table(col_widths=tuple(widths), first_row_as_headings=True,
                   line_height=4, padding=1.2, repeat_headings=1) as table:
        head = table.row()
        for name in headings:
            head.cell(name)
        for index, order in enumerate(orders, start=1):
            row = table.row()
            row.cell(str(index))
            if everywhere:
                row.cell(_cut(order.get("account_title") or "—", 22))
            tracking = str(order.get("return_tracking") or "").strip()
            row.cell("\n".join(
                x for x in (str(order.get("marketplace_id") or order.get("id")),
                            order.get("buyer_name"), tracking) if x
            ))
            if tracking:
                row.cell(img=io.BytesIO(barcode_svg(tracking)), img_fill_width=True)
            else:
                row.cell("—")
            goods = "\n".join(
                f"{item.get('quantity')} × {_cut(item.get('title') or 'Без названия', 52)}"
                for item in (order.get("items") or [])
            )
            row.cell(goods or "—")
            row.cell(str(order.get("items_count") or ""))
            where = [order.get("service_name") or order.get("service_label"), order.get("terminal_address")]
            if order.get("terminal_code"):
                where.append(f"ПВЗ {order['terminal_code']}")
            row.cell(_cut(" · ".join(x for x in where if x) or "адрес Avito не прислал", 70))
            row.cell(_mark_cell(order))
            row.cell(_cut(order.get("note"), 60))


def build_sheet(
    items: list[dict],
    avito_orders: list[dict],
    *,
    user: dict,
    printed_at: datetime,
    everywhere: bool = False,
    account: dict | None = None,
    scheme: str = "all",
    place: str = "",
    truncated: bool = False,
    act: dict | None = None,
) -> bytes:
    """Собрать лист возвратов. Возвращает готовый PDF байтами.

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
        if scheme != "all":
            title += f" ({scheme})"
        if place:
            title += f" · {place}"

    parts = []
    if items:
        parts.append(f"Ozon: {len(items)} поз., {sum(int(i.get('quantity') or 0) for i in items)} шт.")
    if avito_orders:
        parts.append(
            f"Avito: {len(avito_orders)} заказ(ов), "
            f"{sum(int(o.get('items_count') or 0) for o in avito_orders)} шт."
        )
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

    if items:
        if everywhere:
            pdf.section("Ozon")
        _ozon_table(pdf, items, everywhere=everywhere)
    if avito_orders:
        pdf.section("Avito")
        _avito_table(pdf, avito_orders, everywhere=everywhere)
    if not items and not avito_orders:
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
