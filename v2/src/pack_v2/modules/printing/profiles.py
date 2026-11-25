from math import isfinite

from pack_v2.shared.errors import DomainError

PAPER = {"58x40": (58, 40), "75x120": (75, 120), "100x150": (100, 150), "a4": (210, 297)}
KINDS = {
    "ozon:label": "Ozon: стикеры",
    "marking:duplicate": "Дубликаты «Честный знак» — все площадки",
    "yandex:label": "Яндекс Маркет: ярлыки",
}


def default_profiles():
    return [profile(kind, size="75x120") if kind == "yandex:label" else profile(kind) for kind in KINDS]


def number(value, low, high):
    if isinstance(value, bool):
        raise DomainError("Проверьте размеры и отступы принтера")
    try:
        result = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        raise DomainError("Размеры и отступы указываются в миллиметрах") from None
    if not isfinite(result) or not low <= result <= high:
        raise DomainError(f"Значение должно быть от {low} до {high} мм")
    return round(result, 1)


def profile(kind, *, size="58x40", printer="", orientation="", width=58, height=40, gap=0, x=0, y=0):
    if (
        kind not in KINDS
        or size not in (*PAPER, "custom")
        or orientation not in ("", "portrait", "landscape")
    ):
        raise DomainError("Проверьте документ, бумагу и ориентацию")
    if not isinstance(printer, str) or len(printer) > 200 or any(ord(c) < 32 for c in printer):
        raise DomainError("Проверьте имя принтера")
    width, height = PAPER[size] if size != "custom" else (number(width, 30, 500), number(height, 30, 500))
    return {
        "kind": kind,
        "size": size,
        "printer": printer.strip(),
        "orientation": orientation,
        "width": width,
        "height": height,
        "gap": number(gap, 0, 20),
        "x": number(x, -30, 30),
        "y": number(y, -30, 30),
    }


def validate(values):
    if not isinstance(values, list) or not 2 <= len(values) <= 9:
        raise DomainError("Настройте стикеры Ozon и один общий принтер Честного знака")
    try:
        rows = [profile(**value) for value in values]
    except TypeError:
        raise DomainError("Проверьте строки принтеров") from None
    if sum(r["kind"] == "marking:duplicate" for r in rows) != 1:
        raise DomainError("Для Честного знака используется один общий принтер")
    if not any(r["kind"] == "yandex:label" for r in rows):
        rows.append(profile("yandex:label", size="75x120"))
    for kind in ("ozon:label", "yandex:label"):
        labels = [r for r in rows if r["kind"] == kind]
        if not 1 <= len(labels) <= 4 or len({(r["width"], r["height"]) for r in labels}) != len(labels):
            raise DomainError("Укажите от одного до четырёх разных размеров ярлыков каждой площадки")
    return rows


def pick(rows, kind, measured):
    choices = [r for r in rows if r["kind"] == kind]
    width, height = measured
    for row in choices:
        w, h = row["width"], row["height"]
        if (abs(w - width) <= 3 and abs(h - height) <= 3) or (abs(w - height) <= 3 and abs(h - width) <= 3):
            return row
    return choices[0]
