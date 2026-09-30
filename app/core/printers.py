"""Принтеры: какой документ на какой принтер и на какой лист уходит.

Печать в панели устроена двумя путями, и оба живут рядом:

* **через браузер** — как было всегда: PDF открывается во фрейме, дальше окно
  печати браузера и принтер по умолчанию;
* **через QZ Tray** — программа на компьютере склада принимает PDF и сразу
  отправляет его на названный принтер, без окна печати.

Настраивается **по документам**: стикеры Ozon, ярлыки Маркета, этикетки Avito,
лист и акт «Возвратов». У каждого — свой размер листа и свой принтер: на
складе это разные принтеры с разной лентой. Пустой принтер — «через браузер».

У одного документа может быть несколько размеров. Этикетку Avito площадка
отдаёт то 58×40, то 100×150 — смотря чем едет заказ. Поэтому панель меряет
настоящий размер листа в PDF (заголовок X-Page-Size) и по нему выбирает
строку; не совпал ни один — берётся первая строка документа.

Если QZ Tray на компьютере не отвечает или принтера с таким именем на нём нет,
браузер печатает как раньше: из-за настройки печать не должна пропасть.

**Три разные вещи, три настройки.** Какую наклейку просить у площадки (у Ozon
маленький стикер или большой, у Маркета формат ярлыка) — выбор документа,
label_format. На какую бумагу печатать — строка: готовый размер или свой, в мм.
И подгонка строки (⚙): зазор между этикетками и сдвиг печати. Подгонка — только
для QZ Tray: через браузер печать и так идёт через поля драйвера. Её делает
сервер прямо в PDF (fit_pdf): QZ Tray сдвигать умеет только внутрь полей, и то
сжимая этикетку.

Настройка общая на панель и доступна всем, кто в ней работает, — сборщик у
стола сам знает, куда воткнут какой принтер. Кто что поменял, видно в журнале.

**Подпись запросов.** QZ Tray без подписи на каждое подключение спрашивает
«разрешить этому сайту?» — на складе это окно посреди сборки. Поэтому панель
подписывает запросы своим ключом, а её сертификат один раз ставится в QZ Tray.
Ключ и сертификат создаются сами при первом обращении и лежат рядом с базой
(data/qz): ключ — с правами 0600, наружу уходит только сертификат.

На подпись QZ Tray присылает не сам запрос, а его SHA-256 — 64 символа. Что
внутри, сервер не видит, поэтому и подписывает только строки такого вида: это
не делает ключ универсальной подписью для чего угодно. Доверие к сертификату в
QZ Tray — это доверие к панели: кто в ней работает, тот может печатать на
принтерах склада.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from . import db
from .config import settings

log = logging.getLogger(__name__)

# Размеры листа: код -> (подпись, ширина и высота в мм). Добавить размер —
# одна строка здесь; браузер получает их вместе с настройкой.
PAPER: dict[str, tuple[str, int, int]] = {
    "58x40": ("58×40 мм", 58, 40),
    "75x120": ("75×120 мм", 75, 120),
    "100x150": ("100×150 мм", 100, 150),
    "a4": ("A4", 210, 297),
}

# Ориентация печати: код -> подпись. Пусто — как в файле, панель её не задаёт
# (так было всегда). Задаётся принтеру через QZ Tray: при печати через браузер
# PDF печатается как есть, ориентацию там выбирает окно печати или драйвер.
ORIENTATION: dict[str, str] = {
    "": "Как в файле",
    "portrait": "Вертикальная",
    "landscape": "Горизонтальная",
}

# Своя бумага: «Свой размер…» и ширина с высотой в мм — лента бывает любой.
CUSTOM = "custom"
PAPER_MM = (10.0, 500.0)
# Подгонка печати: зазор между этикетками и сдвиг в каждую сторону, мм.
GAP_MAX = 20.0
SHIFT_MAX = 30.0
SIDES = ("top", "right", "bottom", "left")
MM = 72 / 25.4                    # пунктов PDF в миллиметре

KV_ROWS = "printers"              # JSON: [{"kind", "size", "printer", "orientation"}, …]
KV_FORMATS = "label_formats"      # JSON: {"ozon:label": "small", …} — какую наклейку просить
KV_LEGACY = "printer"             # 1.42.0: printer:label и printer:a4
ROWS_PER_KIND = 4                 # больше размеров у одного документа не бывает
NAME_LIMIT = 200                  # имя принтера длиннее — это уже не имя
MATCH_MM = 5                      # «тот же размер»: ±5 мм на неточность PDF

# Что присылает QZ Tray на подпись: SHA-256 запроса шестнадцатеричной строкой.
TO_SIGN = re.compile(r"^[0-9a-f]{64}$")

_lock = threading.Lock()


# ------------------------------------------------------------------ документы
def documents() -> list[dict]:
    """Что печатает панель. Наклейки — у каждой площадки свои, из реестра.

    Размер по умолчанию — то, что панель печатала до настройки: наклейка
    75×120, листы (с заказами, возвратов) и акт — A4. Пока человек не выбрал
    своё, ничего не меняется.
    """
    from ..markets import registry

    out = []
    for market in registry.all_markets():
        if market.labels is None:
            continue
        out.append({
            "kind": f"{market.code}:label",
            "section": "Сборка и заказы",
            "title": f"{market.title}: {market.labels.word}",
            "size": "75x120",
            "hint": market.labels.size_hint,
        })
    out.append({"kind": "pack:sheet", "section": "Сборка и заказы",
                "title": "Лист с заказами", "size": "a4", "hint": ""})
    out.append({"kind": "returns:sheet", "section": "Возвраты",
                "title": "Лист возвратов для пункта выдачи", "size": "a4", "hint": ""})
    out.append({"kind": "returns:act", "section": "Возвраты",
                "title": "Акт возврата", "size": "a4", "hint": ""})
    return out


def parse_mm(value, low: float, high: float, what: str) -> float:
    """Миллиметры из поля: запятая или точка, до десятых. Вне границ — ValueError."""
    raw = str(value if value is not None else "").strip().replace(",", ".") or "0"
    try:
        number = round(float(raw), 1)
    except ValueError:
        raise ValueError(f"{what}: нужно число в миллиметрах") from None
    if not low <= number <= high:
        raise ValueError(f"{what}: от {_num(low)} до {_num(high)} мм")
    return number


def _num(value: float) -> str:
    """58.0 -> «58», 1.5 -> «1,5»: как пишут миллиметры."""
    return (f"{value:.1f}".rstrip("0").rstrip(".")).replace(".", ",")


def _row(kind: str, size: str, printer: str = "", orientation: str = "", *,
         width: float | None = None, height: float | None = None,
         gap: float = 0.0, shift: dict | None = None) -> dict:
    """Строка настройки. Необязательное — только если задано: так настройка из
    прежних версий и новая читаются одинаково."""
    out = {"kind": kind, "size": size, "printer": printer, "orientation": orientation}
    if size == CUSTOM:
        out["width"], out["height"] = width, height
    if gap:
        out["gap"] = gap
    shift = {side: value for side, value in (shift or {}).items() if side in SIDES and value}
    if shift:
        out["shift"] = shift
    return out


def _parse(item: dict, title: str) -> dict:
    """Строка из запроса или из базы — проверенная. Ошибка — ValueError с текстом."""
    kind, size = str(item.get("kind") or ""), str(item.get("size") or "")
    if size not in PAPER and size != CUSTOM:
        raise ValueError(f"Неизвестный размер листа у «{title}»")
    width = height = None
    if size == CUSTOM:
        width = parse_mm(item.get("width"), *PAPER_MM, f"«{title}»: ширина бумаги")
        height = parse_mm(item.get("height"), *PAPER_MM, f"«{title}»: высота бумаги")
    printer = " ".join(str(item.get("printer") or "").split())
    if len(printer) > NAME_LIMIT:
        raise ValueError(f"Слишком длинное имя принтера у «{title}»")
    orientation = str(item.get("orientation") or "")
    if orientation not in ORIENTATION:
        raise ValueError(f"Неизвестная ориентация печати у «{title}»")
    gap = parse_mm(item.get("gap"), 0, GAP_MAX, f"«{title}»: зазор")
    raw_shift = item.get("shift") if isinstance(item.get("shift"), dict) else {}
    shift = {side: parse_mm(raw_shift.get(side), 0, SHIFT_MAX, f"«{title}»: сдвиг") for side in SIDES}
    return _row(kind, size, printer, orientation, width=width, height=height, gap=gap, shift=shift)


def _clean_row(row, known: set[str]) -> dict | None:
    if not isinstance(row, dict) or str(row.get("kind") or "") not in known:
        return None
    try:
        return _parse(row, str(row.get("kind")))
    except ValueError:
        return None


def paper_mm(row: dict) -> tuple[float, float]:
    """Бумага строки в мм: свой размер или готовый."""
    if row["size"] == CUSTOM:
        return float(row["width"]), float(row["height"])
    _title, width, height = PAPER[row["size"]]
    return float(width), float(height)


def paper_title(row: dict) -> str:
    """Бумага словами: «58×40 мм», «A4» или свой размер — «60×40 мм»."""
    if row["size"] == CUSTOM:
        width, height = paper_mm(row)
        return f"{_num(width)}×{_num(height)} мм"
    return PAPER[row["size"]][0]


def fit_title(row: dict) -> str:
    """Подгонка словами для строки и журнала: «зазор 2 · вправо 2 · вниз 1». Пусто — не задана."""
    words = {"top": "вверх", "right": "вправо", "bottom": "вниз", "left": "влево"}
    parts = [f"зазор {_num(row['gap'])}"] if row.get("gap") else []
    parts += [f"{words[side]} {_num(value)}" for side, value in (row.get("shift") or {}).items()]
    return " · ".join(parts)


def _saved(known: set[str]) -> list[dict] | None:
    """Сохранённые строки. None — ещё ничего не сохраняли."""
    raw = db.kv_get(KV_ROWS)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("Настройка принтеров не читается — беру значения по умолчанию")
        return None
    return [row for row in (_clean_row(item, known) for item in data or []) if row]


def _legacy(docs: list[dict]) -> list[dict]:
    """Настройка 1.42.0 была по размерам: наклейка и A4. Переносим, чтобы не пропала."""
    label = db.kv_get(f"{KV_LEGACY}:label", "") or ""
    sheet = db.kv_get(f"{KV_LEGACY}:a4", "") or ""
    out = []
    for doc in docs:
        printer = sheet if doc["size"] == "a4" else label
        if printer:
            out.append(_row(doc["kind"], doc["size"], printer))
    return out


def rows() -> list[dict]:
    """Строки настройки по порядку документов. У каждого документа — хотя бы одна."""
    docs = documents()
    known = {doc["kind"] for doc in docs}
    saved = _saved(known)
    if saved is None:
        saved = _legacy(docs)
    out: list[dict] = []
    for doc in docs:
        mine = [row for row in saved if row["kind"] == doc["kind"]]
        out.extend(mine or [_row(doc["kind"], doc["size"])])
    return out


def save(wanted) -> list[dict]:
    """Сохранить строки. Ошибка — ValueError с текстом для человека."""
    if not isinstance(wanted, list):
        raise ValueError("Нужен список: документ, размер листа, принтер, ориентация")
    docs = {doc["kind"]: doc for doc in documents()}
    clean: list[dict] = []
    seen: set[tuple[str, str]] = set()
    per_kind: dict[str, int] = {}
    for item in wanted:
        if not isinstance(item, dict):
            raise ValueError("Непонятная строка настройки")
        kind = str(item.get("kind") or "")
        if kind not in docs:
            raise ValueError(f"Неизвестный документ: {kind or '—'}")
        title = docs[kind]["title"]
        row = _parse(item, title)
        # Одна и та же бумага дважды — две строки на один файл, какая сработает, не угадать.
        paper = "x".join(_num(mm) for mm in paper_mm(row))
        if (kind, paper) in seen:
            raise ValueError(f"У «{title}» размер {paper_title(row)} указан дважды")
        seen.add((kind, paper))
        per_kind[kind] = per_kind.get(kind, 0) + 1
        if per_kind[kind] > ROWS_PER_KIND:
            raise ValueError(f"У «{title}» слишком много размеров")
        clean.append(row)
    db.kv_set(KV_ROWS, json.dumps(clean, ensure_ascii=False))
    return rows()


# ------------------------------------------------------------------ наклейка от площадки
def _label_sources() -> dict[str, object]:
    """Документы-наклейки, у которых площадка умеет разные размеры: {вид: LabelsSource}."""
    from ..markets import registry

    return {f"{market.code}:label": market.labels for market in registry.all_markets()
            if market.labels is not None and market.labels.formats}


def label_format(kind: str) -> str | None:
    """Какую наклейку просить у площадки: выбранную на «Принтерах», а нет — прежнюю.

    None — у документа выбора нет (Avito: размер решает площадка).
    """
    source = _label_sources().get(kind)
    if source is None:
        return None
    codes = [code for code, _title in source.formats]
    try:
        saved = json.loads(db.kv_get(KV_FORMATS) or "{}")
    except ValueError:
        saved = {}
    chosen = saved.get(kind) if isinstance(saved, dict) else None
    if chosen in codes:
        return chosen
    fallback = source.format_default() if source.format_default else None
    return fallback if fallback in codes else codes[0]


def save_formats(wanted) -> dict[str, str]:
    """Сохранить выбор наклеек: {вид документа: код}. Ошибка — ValueError."""
    if wanted is None:
        return {}
    if not isinstance(wanted, dict):
        raise ValueError("Непонятный выбор наклейки")
    sources = _label_sources()
    clean: dict[str, str] = {}
    for kind, code in wanted.items():
        source = sources.get(str(kind))
        if source is None:
            raise ValueError(f"У документа {kind} размер наклейки не выбирается")
        if str(code) not in {item for item, _title in source.formats}:
            raise ValueError(f"Неизвестный размер наклейки: {code}")
        clean[str(kind)] = str(code)
    db.kv_set(KV_FORMATS, json.dumps(clean, ensure_ascii=False))
    return clean


def size_of(kind: str) -> str | None:
    """Размер листа документа — первой его строки. Нужен площадке, у которой
    размер ярлыка задаётся в запросе (Маркет)."""
    return next((row["size"] for row in rows() if row["kind"] == kind), None)


def setup() -> dict:
    """Что нужно браузеру для печати: строки и размеры листа в мм."""
    return {"rows": rows(), "paper": {code: [w, h] for code, (_t, w, h) in PAPER.items()},
            "match_mm": MATCH_MM}


def page(docs: list[dict] | None = None) -> list[dict]:
    """Документы для страницы настройки: подписи, их строки и выбор наклейки."""
    table = rows()
    sources = _label_sources()
    out = []
    for doc in docs or documents():
        source = sources.get(doc["kind"])
        extra = ({"formats": list(source.formats), "format_title": source.format_title,
                  "format": label_format(doc["kind"])} if source else {})
        mine = [{**row, "paper_title": paper_title(row), "fit": fit_title(row), "mm": _mm_texts(row)}
                for row in table if row["kind"] == doc["kind"]]
        out.append({**doc, **extra, "rows": mine})
    return out


def _mm_texts(row: dict) -> dict[str, str]:
    """Числа строки для полей страницы: «60», «1,5»; пусто — не задано."""
    width, height = paper_mm(row)
    shift = row.get("shift") or {}
    texts = {"width": _num(width), "height": _num(height), "gap": _num(row.get("gap") or 0)}
    texts.update({side: _num(shift.get(side) or 0) for side in SIDES})
    return texts


# ------------------------------------------------------------------ подгонка печати
def fit_pdf(pdf: bytes, *, gap: float = 0, top: float = 0, right: float = 0,
            bottom: float = 0, left: float = 0) -> bytes:
    """Сдвинуть печать и добавить зазор — прямо в PDF, для печати через QZ Tray.

    Сдвиг — перенос содержимого каждой страницы на заданные мм (вверх и вниз,
    влево и вправо гасят друг друга). Зазор — пустая полоса снизу страницы:
    длина листа становится «этикетка + зазор», и принтер, считающий ленту
    сплошной, протягивает её ровно до следующей этикетки. Повёрнутую страницу
    сначала выпрямляем: иначе «вправо» уехало бы вниз.
    """
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf.generic import RectangleObject

    dx, dy = (right - left) * MM, (top - bottom) * MM
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(pdf)).pages:
        if int(page.get("/Rotate") or 0) % 360:
            page.transfer_rotation_to_content()
        if dx or dy:
            page.add_transformation(Transformation().translate(dx, dy))
        if gap:
            box = page.mediabox
            grown = RectangleObject((box.left, float(box.bottom) - gap * MM, box.right, box.top))
            page.mediabox = grown
            page.cropbox = grown
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def sample_pdf(width: float, height: float, title: str) -> bytes:
    """Пробная этикетка для подгонки: рамка в 1 мм от края бумаги, крест по центру и подпись.

    По рамке сразу видно, куда уехала печать: какая сторона обрезана или шире
    других, в ту сторону и сдвиг.
    """
    from fpdf import FPDF

    from .returns_pdf import FontMissing, _find_fonts

    pdf = FPDF(unit="mm", format=(width, height))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)
    pdf.add_page()
    try:
        regular, _bold = _find_fonts()
        pdf.add_font("label", "", str(regular))
        pdf.set_font("label", "", 8)
    except FontMissing:
        pdf.set_font("helvetica", "", 8)
        title = title.encode("ascii", "replace").decode()
    pdf.set_line_width(0.4)
    pdf.rect(1, 1, width - 2, height - 2)
    pdf.line(width / 2 - 4, height / 2, width / 2 + 4, height / 2)
    pdf.line(width / 2, height / 2 - 4, width / 2, height / 2 + 4)
    pdf.set_xy(3, 3)
    pdf.multi_cell(width - 6, 3.6, title)
    return bytes(pdf.output())


# ------------------------------------------------------------------ размер PDF
def page_size(pdf: bytes) -> str | None:
    """Размер первой страницы PDF в мм — «58x40». None — не прочиталось.

    По нему браузер выбирает принтер, когда у документа их несколько: этикетку
    Avito 100×150 — на один, 58×40 — на другой.
    """
    try:
        from pypdf import PdfReader

        first = PdfReader(io.BytesIO(pdf)).pages[0]
        width, height = float(first.mediabox.width), float(first.mediabox.height)
        if int(first.get("/Rotate") or 0) % 180:
            width, height = height, width
    except Exception:  # noqa: BLE001 - без размера печать всё равно пойдёт
        return None
    return f"{round(width * 25.4 / 72)}x{round(height * 25.4 / 72)}"


def size_header(pdf: bytes) -> dict[str, str]:
    """Заголовок с размером листа — в каждый ответ с наклейкой."""
    size = page_size(pdf)
    return {"X-Page-Size": size} if size else {}


# ------------------------------------------------------------------ сертификат
def _folder() -> Path:
    return Path(settings.db_path).parent / "qz"


def _key_path() -> Path:
    return _folder() / "private-key.pem"


def _cert_path() -> Path:
    return _folder() / "certificate.pem"


def _write_private(path: Path, data: bytes) -> None:
    """Записать ключ сразу с правами 0600 — без мгновения, когда его видят все."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o600)


def _create() -> None:
    """Ключ RSA и самоподписанный сертификат для QZ Tray — один раз на панель."""
    _folder().mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Ozon Pack"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Ozon Pack"),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        # Двадцать лет: сертификат ставится в QZ Tray руками на каждом
        # компьютере, и истечь посреди смены ему незачем.
        .not_valid_after(now + timedelta(days=365 * 20))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    _write_private(_key_path(), key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    _cert_path().write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    db.log_event("qz_certificate", message="Создан сертификат панели для QZ Tray")


def certificate() -> str:
    """Сертификат панели (PEM). Создаётся при первом обращении."""
    with _lock:
        if not (_key_path().exists() and _cert_path().exists()):
            _create()
    return _cert_path().read_text(encoding="ascii")


def sign(to_sign: str) -> str:
    """Подписать запрос QZ Tray: RSA, SHA-512, base64 — как ждёт QZ Tray 2.1+."""
    value = (to_sign or "").strip()
    if not TO_SIGN.match(value):
        raise ValueError("Подписываются только запросы QZ Tray")
    certificate()   # ключ мог ещё не быть создан
    key = serialization.load_pem_private_key(_key_path().read_bytes(), password=None)
    signature = key.sign(value.encode("ascii"), padding.PKCS1v15(), hashes.SHA512())
    return base64.b64encode(signature).decode("ascii")
