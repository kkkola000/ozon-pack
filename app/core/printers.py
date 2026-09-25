"""Принтеры: какой размер листа на какой принтер уходит через QZ Tray.

Печать в панели устроена двумя путями, и оба живут рядом:

* **через браузер** — как было всегда: PDF открывается во фрейме, дальше окно
  печати браузера и принтер по умолчанию;
* **через QZ Tray** — программа на компьютере склада принимает PDF и сразу
  отправляет его на названный принтер, без окна печати.

Владелец выбирает путь для каждого размера листа: наклейку — на термопринтер
через QZ Tray, лист A4 — через браузер, или наоборот. Пустое значение — «через
браузер». Если QZ Tray на компьютере не отвечает или принтера с таким именем
на нём нет, браузер печатает как раньше: из-за настройки печать не должна
пропасть ни на одном рабочем месте.

Настройка общая на панель, а не на компьютер: владелец настраивает её один раз
за упаковочным столом, где список принтеров QZ Tray и берётся.

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

# Размеры листа, которые печатает панель. Добавить размер — одна строка здесь
# и одна в static/qz.js (параметры бумаги для QZ Tray).
SIZES: tuple[tuple[str, str, str], ...] = (
    ("label", "Наклейка 75×120 мм",
     "стикеры Ozon, ярлыки Маркета, этикетки Avito — на «Сборке» и в «Заказах»"),
    ("a4", "Лист A4",
     "лист возвратов для пункта выдачи и акт возврата"),
)
SIZE_CODES = tuple(code for code, _title, _what in SIZES)

KV_PRINTER = "printer"            # printer:<размер> -> имя принтера QZ Tray или ""
NAME_LIMIT = 200                  # имя принтера длиннее — это уже не имя

# Что присылает QZ Tray на подпись: SHA-256 запроса шестнадцатеричной строкой.
TO_SIGN = re.compile(r"^[0-9a-f]{64}$")

_lock = threading.Lock()


# ------------------------------------------------------------------ настройка
def printers() -> dict[str, str]:
    """Принтер на каждый размер листа. Пусто — печать через браузер."""
    return {code: db.kv_get(f"{KV_PRINTER}:{code}", "") or "" for code in SIZE_CODES}


def sizes() -> list[dict]:
    """Размеры листа с тем, что на них печатается, и выбранным принтером."""
    chosen = printers()
    return [{"code": code, "title": title, "what": what, "printer": chosen[code]}
            for code, title, what in SIZES]


def save(wanted: dict) -> dict[str, str]:
    """Сохранить выбор владельца. Ошибка — ValueError с текстом для человека."""
    if not isinstance(wanted, dict):
        raise ValueError("Нужен список принтеров по размерам листа")
    unknown = [key for key in wanted if key not in SIZE_CODES]
    if unknown:
        raise ValueError(f"Неизвестный размер листа: {', '.join(map(str, unknown))}")
    clean: dict[str, str] = {}
    for code, value in wanted.items():
        name = " ".join(str(value or "").split())
        if len(name) > NAME_LIMIT:
            raise ValueError(f"Слишком длинное имя принтера для «{code}»")
        clean[code] = name
    for code, name in clean.items():
        db.kv_set(f"{KV_PRINTER}:{code}", name)
    return printers()


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
