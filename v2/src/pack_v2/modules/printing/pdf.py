"""Transient print copies. Original marketplace files are never modified."""

import io

from fpdf import FPDF
from PIL import Image
from pypdf import PdfReader, PdfWriter, Transformation

from pack_v2.modules.marking.public import parse as parse_mark
from pack_v2.shared.errors import DomainError

MM = 72 / 25.4


def measure(data):
    page = PdfReader(io.BytesIO(data)).pages[0]
    if int(page.get("/Rotate", 0)) % 360:
        page.transfer_rotation_to_content()
    return float(page.mediabox.width) / MM, float(page.mediabox.height) / MM


def page(data, index):
    reader = PdfReader(io.BytesIO(data))
    if type(index) is not int or not 0 <= index < len(reader.pages):
        raise DomainError("Страница ярлыка изменилась. Обновите сборку")
    writer = PdfWriter()
    writer.add_page(reader.pages[index])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def barcode_image(code, *, marking=False):
    import zxingcpp

    raw = code
    if marking:
        parsed = parse_mark(code)
        raw = parsed.raw
        fields = (("01", parsed.gtin), ("21", raw[18:])) if parsed.compact else parsed.fields
        content = "".join(value for _, value in fields)
        brackets = ("[", "]") if "[" not in content and "]" not in content else ("(", ")")
        if any(char in content for char in brackets):
            raise DomainError(
                "Не удалось безопасно подготовить дубликат Честного знака. Исходный код сохранён"
            )
        text = "".join(f"{brackets[0]}{ai}{brackets[1]}{value}" for ai, value in fields)
    else:
        if not code or len(code) > 200 or any(not 32 <= ord(c) <= 126 for c in code):
            raise DomainError("Штрихкод товара не поддерживается для печати")
        text = code
    symbol = zxingcpp.create_barcode(
        text,
        zxingcpp.BarcodeFormat.DataMatrix if marking else zxingcpp.BarcodeFormat.Code128,
        gs1=marking,
        **({"force_square": True} if marking else {}),
    )
    raster = symbol.to_image(scale=8, add_quiet_zones=True)
    result = Image.frombytes("L", (raster.shape[1], raster.shape[0]), bytes(raster))
    decoded = zxingcpp.read_barcode(result)
    if (
        not decoded
        or decoded.bytes != raw.encode("ascii")
        or marking
        and decoded.content_type != zxingcpp.ContentType.GS1
    ):
        raise DomainError("Контроль дубликата не пройден. Печать остановлена; исходный код сохранён")
    return result


def labels(codes, width, height, *, marking=False):
    if not 1 <= len(codes) <= 500 or min(width, height) < 30:
        raise DomainError("Проверьте количество этикеток и размер бумаги")
    out = FPDF(unit="mm", format=(width, height))
    out.set_auto_page_break(False)
    out.set_margins(0, 0, 0)
    for code in codes:
        image = barcode_image(code, marking=marking)
        png = io.BytesIO()
        image.save(png, format="PNG")
        out.add_page()
        if marking:
            parsed = parse_mark(code)
            side = min(30, width - 6, height - 11)
            out.image(png, x=(width - side) / 2, y=2, w=side, h=side)
            text, y = f"{parsed.gtin}\n{parsed.serial}", side + 3
        else:
            bar_height = min(25, height * 0.5)
            top = max(2, (height - bar_height - 7) / 2)
            out.image(png, x=4, y=top, w=width - 8, h=bar_height)
            text, y = code, top + bar_height + 1
        out.set_font("Helvetica", size=7 if marking else 10)
        while (
            any(out.get_string_width(line) > width - 4 for line in text.splitlines()) and out.font_size_pt > 5
        ):
            out.set_font_size(out.font_size_pt - 1)
        out.set_xy(2, y)
        out.multi_cell(width - 4, 3, text, align="C")
    return bytes(out.output())


def prepend(original, codes):
    if not codes:
        return original
    width, height = measure(original)
    out = PdfWriter()
    out.append(PdfReader(io.BytesIO(labels(codes, width, height))))
    out.append(PdfReader(io.BytesIO(original)))
    target = io.BytesIO()
    out.write(target)
    return target.getvalue()


def fit(data, row):
    out = PdfWriter()
    source = PdfWriter(clone_from=io.BytesIO(data))
    for page in source.pages:
        if int(page.get("/Rotate", 0)) % 360:
            page.transfer_rotation_to_content()
        # Scaling and offsets are applied to the print copy for both delivery paths.
        width, height = paper_size(row)
        w, h = width * MM, height * MM
        if row["orientation"] and (w > h) != (float(page.mediabox.width) > float(page.mediabox.height)):
            page.rotate(90)
            page.transfer_rotation_to_content()
        scale = min(w / float(page.mediabox.width), h / float(page.mediabox.height))
        x = (w - float(page.mediabox.width) * scale) / 2 + row["x"] * MM
        y = (h - float(page.mediabox.height) * scale) / 2 + (row["y"] + row["gap"]) * MM
        sheet = out.add_blank_page(width=w, height=h + row["gap"] * MM)
        sheet.merge_transformed_page(
            page,
            Transformation()
            .translate(-float(page.mediabox.left), -float(page.mediabox.bottom))
            .scale(scale)
            .translate(x, y),
        )
    target = io.BytesIO()
    out.write(target)
    return target.getvalue()


def paper_size(row):
    w, h = row["width"], row["height"]
    if row["orientation"] == "portrait" and w > h or row["orientation"] == "landscape" and h > w:
        return h, w
    return w, h


def sample(row):
    out = FPDF(unit="mm", format=(row["width"], row["height"]))
    out.set_auto_page_break(False)
    out.add_page()
    w, h = row["width"], row["height"]
    out.set_line_width(0.2)
    out.rect(1, 1, w - 2, h - 2)
    out.line(w / 2 - 5, h / 2, w / 2 + 5, h / 2)
    out.line(w / 2, h / 2 - 5, w / 2, h / 2 + 5)
    out.set_font("Helvetica", size=8)
    out.text(3, 6, f"Pack v2 / Print test / {w:g} x {h:g} mm")
    return bytes(out.output())
