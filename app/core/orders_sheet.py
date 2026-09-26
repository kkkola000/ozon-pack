"""«Лист с заказами»: что взять с полки под заказы, которые ждут сборки.

Кнопка на «Сборке», в блоке «Очередь». Лист — PDF: кабинет, фото, название
товара, артикул и количество. С ним сборщик обходит склад один раз и
приносит к столу всё сразу, а не бегает к полке под каждый заказ.

Какие заказы: «К сборке» — те же, что в плитке очереди («Ожидает отгрузки»,
ещё не собраны), и под тем же фильтром кабинетов, что стоит на «Сборке».
Одинаковый товар одного кабинета — одна строка с общим количеством: полке всё
равно, в скольких заказах он нужен. Кабинеты не смешиваются — у каждого
магазина свой товар, даже если он выглядит так же.

Строки берутся из того же объявления площадок, что и раздел «Заказы»
(OrdersBoard): площадок по именам здесь нет. Фото — то, что показывает панель
(своё или сопоставленной карточки); скачивает их core/photos.py.
"""
from __future__ import annotations

import io

from . import board as core_board
from . import db, photos
from .returns_pdf import FontMissing, PdfUnavailable, _find_fonts, cut
from .store import local_time

TITLE = "Лист с заказами"
STATUS = "deliver"        # «Ожидает отгрузки» — то, что в очереди «К сборке»

LIBRARY_HELP = (
    "Для листа в PDF нужна библиотека fpdf2, а она не установлена. "
    "Поставьте зависимости панели: sudo -u ozon /opt/ozon-pack/.venv/bin/pip install "
    "-r /opt/ozon-pack/requirements.txt — и перезапустите службу."
)
FONT_HELP = (
    "Для PDF нужен шрифт с кириллицей, а в системе его нет. "
    "Поставьте его одной командой: sudo apt install -y fonts-dejavu-core — и попробуйте снова."
)


class NothingToPick(ValueError):
    """Заказов к сборке нет — и листа нет."""


# ------------------------------------------------------------------ строки
def collect(where: list[dict]) -> dict:
    """Строки листа по кабинетам под фильтром.

    {rows: [{shop, name, code, quantity, image, orders}], orders, pieces,
    truncated}. Порядок — кабинеты как в «Настройках», внутри по названию.
    """
    cards = core_board.rows(where, STATUS)
    order_of = {account["id"]: index for index, account in enumerate(where)}
    merged: dict[tuple, dict] = {}
    for card in cards:
        for item in card.get("items") or []:
            name = item.get("name") or "Без названия"
            code = str(item.get("code") or "").strip()
            key = (card["account_id"], code or name.lower())
            row = merged.setdefault(key, {
                "account_id": card["account_id"], "shop": card.get("shop") or "—",
                "name": name, "code": code, "quantity": 0, "image": "", "orders": set(),
            })
            row["quantity"] += int(item.get("quantity") or 0)
            row["orders"].add(str(card["number"]))
            row["image"] = row["image"] or item.get("image") or ""
    rows = sorted(merged.values(), key=lambda row: (
        order_of.get(row["account_id"], 10**6), row["name"].lower(), row["code"],
    ))
    for row in rows:
        row["orders"] = len(row["orders"])
    return {
        "rows": rows,
        "orders": len(cards),
        "pieces": sum(row["quantity"] for row in rows),
        "truncated": len(cards) >= core_board.LIMIT,
    }


# ------------------------------------------------------------------ PDF
def _fonts():
    try:
        return _find_fonts()
    except FontMissing as exc:
        raise PdfUnavailable(FONT_HELP) from exc


def _pdf(title: str, subtitle: str):
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise PdfUnavailable(LIBRARY_HELP) from exc

    class Sheet(FPDF):
        """Лист с колонтитулом: без номеров страниц пачку легко перепутать."""

        def header(self) -> None:
            self.set_font("sheet", "B", 13)
            self.cell(0, 6, title, new_x="LMARGIN", new_y="NEXT")
            self.set_font("sheet", "", 8)
            self.cell(0, 5, subtitle, new_x="LMARGIN", new_y="NEXT")
            self.ln(2)

        def footer(self) -> None:
            self.set_y(-10)
            self.set_font("sheet", "", 7)
            self.cell(0, 5, f"Страница {self.page_no()} из {{nb}}", align="R")

    regular, bold = _fonts()
    pdf = Sheet(orientation="P", unit="mm", format="A4")
    pdf.add_font("sheet", "", str(regular))
    pdf.add_font("sheet", "B", str(bold))
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.set_margins(8, 8, 8)
    pdf.set_title(title)
    pdf.alias_nb_pages()
    return pdf


def build(sheet: dict, *, user: dict, scope: str) -> bytes:
    """Собрать лист. scope — «все кабинеты» или название выбранного."""
    rows = sheet["rows"]
    if not rows:
        raise NothingToPick("Заказов к сборке нет — лист пустой. Нажмите «Обновить заказы».")
    title = f"{TITLE} · {scope}"
    subtitle = " · ".join((
        f"К сборке: заказов {sheet['orders']}, товаров {len(rows)}, всего {sheet['pieces']} шт.",
        f"Сформировал: {user.get('login', '—')}",
        local_time(db.now_iso(), "%d.%m.%Y %H:%M"),
    ))
    pdf = _pdf(title, subtitle)
    from fpdf.fonts import FontFace

    bold = FontFace(emphasis="BOLD", size_pt=11)
    pdf.add_page()
    images = photos.thumbnails(row["image"] for row in rows)

    widths = (36.0, 22.0, 88.0, 32.0, 18.0)
    free = pdf.w - pdf.l_margin - pdf.r_margin
    widths = tuple(width * free / sum(widths) for width in widths)
    pdf.set_font("sheet", "", 9)
    with pdf.table(col_widths=widths, first_row_as_headings=True, line_height=5, padding=1.5,
                   repeat_headings=1, text_align=("LEFT", "CENTER", "LEFT", "LEFT", "CENTER")) as table:
        head = table.row()
        for name in ("Кабинет", "Фото", "Название товара", "Артикул", "Кол-во"):
            head.cell(name)
        for row in rows:
            line = table.row()
            line.cell(cut(row["shop"], 40))
            photo = images.get(row["image"])
            if photo:
                line.cell(img=io.BytesIO(photo), img_fill_width=True)
            else:
                line.cell("нет фото")
            line.cell(cut(row["name"], 140))
            line.cell(cut(row["code"] or "—", 40))
            line.cell(str(row["quantity"]), style=bold)

    pdf.ln(3)
    pdf.set_font("sheet", "B", 10)
    pdf.cell(0, 6, f"Итого: {sheet['pieces']} шт.", align="R", new_x="LMARGIN", new_y="NEXT")
    if sheet["truncated"]:
        pdf.set_font("sheet", "B", 9)
        pdf.multi_cell(
            0, 5,
            f"Внимание: в лист попали первые {core_board.LIMIT} заказов по сроку отгрузки. "
            "Остальные — отдельным листом по кабинетам.",
            border=1, new_x="LMARGIN", new_y="NEXT",
        )
    return bytes(pdf.output())


def filename(scope_id: str) -> str:
    """Имя файла латиницей: русское имя часть браузеров превращает в кашу."""
    stamp = local_time(db.now_iso(), "%Y-%m-%d_%H-%M")
    suffix = "vse-kabinety" if scope_id == core_board.ALL else f"kabinet-{scope_id}"
    return f"list-zakazov-{suffix}-{stamp}.pdf"
