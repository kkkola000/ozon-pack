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

KV_ROWS = "printers"              # JSON: [{"kind", "size", "printer"}, …]
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
    75×120, лист возвратов и акт — A4. Пока человек не выбрал своё, ничего не
    меняется.
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
    out.append({"kind": "returns:sheet", "section": "Возвраты",
                "title": "Лист возвратов для пункта выдачи", "size": "a4", "hint": ""})
    out.append({"kind": "returns:act", "section": "Возвраты",
                "title": "Акт возврата", "size": "a4", "hint": ""})
    return out


def _clean_row(row, known: set[str]) -> dict | None:
    if not isinstance(row, dict):
        return None
    kind, size = str(row.get("kind") or ""), str(row.get("size") or "")
    if kind not in known or size not in PAPER:
        return None
    return {"kind": kind, "size": size, "printer": " ".join(str(row.get("printer") or "").split())}


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
        printer = sheet if doc["kind"].startswith("returns:") else label
        if printer:
            out.append({"kind": doc["kind"], "size": doc["size"], "printer": printer})
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
        out.extend(mine or [{"kind": doc["kind"], "size": doc["size"], "printer": ""}])
    return out


def save(wanted) -> list[dict]:
    """Сохранить строки. Ошибка — ValueError с текстом для человека."""
    if not isinstance(wanted, list):
        raise ValueError("Нужен список: документ, размер листа, принтер")
    docs = {doc["kind"]: doc for doc in documents()}
    clean: list[dict] = []
    seen: set[tuple[str, str]] = set()
    per_kind: dict[str, int] = {}
    for item in wanted:
        if not isinstance(item, dict):
            raise ValueError("Непонятная строка настройки")
        kind, size = str(item.get("kind") or ""), str(item.get("size") or "")
        if kind not in docs:
            raise ValueError(f"Неизвестный документ: {kind or '—'}")
        title = docs[kind]["title"]
        if size not in PAPER:
            raise ValueError(f"Неизвестный размер листа у «{title}»")
        printer = " ".join(str(item.get("printer") or "").split())
        if len(printer) > NAME_LIMIT:
            raise ValueError(f"Слишком длинное имя принтера у «{title}»")
        if (kind, size) in seen:
            raise ValueError(f"У «{title}» размер {PAPER[size][0]} указан дважды")
        seen.add((kind, size))
        per_kind[kind] = per_kind.get(kind, 0) + 1
        if per_kind[kind] > ROWS_PER_KIND:
            raise ValueError(f"У «{title}» слишком много размеров")
        clean.append({"kind": kind, "size": size, "printer": printer})
    db.kv_set(KV_ROWS, json.dumps(clean, ensure_ascii=False))
    return rows()


def size_of(kind: str) -> str | None:
    """Размер листа документа — первой его строки. Нужен площадке, у которой
    размер ярлыка задаётся в запросе (Маркет)."""
    return next((row["size"] for row in rows() if row["kind"] == kind), None)


def setup() -> dict:
    """Что нужно браузеру для печати: строки и размеры листа в мм."""
    return {"rows": rows(), "paper": {code: [w, h] for code, (_t, w, h) in PAPER.items()},
            "match_mm": MATCH_MM}


def page(docs: list[dict] | None = None) -> list[dict]:
    """Документы для страницы настройки: подписи и их строки."""
    table = rows()
    return [{**doc, "rows": [row for row in table if row["kind"] == doc["kind"]]}
            for doc in (docs or documents())]


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
