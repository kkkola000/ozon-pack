"""Шифрование ключей площадок в базе.

Зачем: в таблице accounts лежат Api-Key Ozon и client_secret Avito — доступ к
товарам, заказам и отгрузкам магазина. Права 0600 на файл базы защищают от
соседа по серверу, но не от копии базы: бэкап на ноутбуке, снапшот диска,
выгрузка «посмотреть» — и ключи уехали открытым текстом.

Чем: AES-256-GCM. Ключ шифрования выводится из того же секрета, которым
подписываются сессии (SECRET_KEY или data/.secret_key, права 0600), — отдельным
разделом HKDF, чтобы один секрет не использовался в двух ролях напрямую.

Важно: потеря секрета = потеря ключей площадок. Расшифровать их будет нечем,
и ключи придётся ввести заново в «Настройках». Файл data/.secret_key переживает
перезапуск и обновление, но в бэкап должен попадать вместе с базой.
"""
from __future__ import annotations

import base64
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import settings

log = logging.getLogger("crypto")

# Метка в начале значения: по ней видно, что строка зашифрована этой версией.
PREFIX = "enc:v1:"
NONCE_BYTES = 12


def _cipher() -> AESGCM:
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"ozon-pack account credentials v1",
    ).derive(settings.secret_key.encode("utf-8"))
    return AESGCM(key)


def is_encrypted(value: str | None) -> bool:
    return bool(value) and str(value).startswith(PREFIX)


def encrypt(value: str | None) -> str:
    """Зашифровать ключ площадки. Пустое значение остаётся пустым."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if is_encrypted(raw):
        return raw
    nonce = os.urandom(NONCE_BYTES)
    sealed = _cipher().encrypt(nonce, raw.encode("utf-8"), None)
    return PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def decrypt(value: str | None) -> str:
    """Расшифровать ключ. Значение без метки — из старой базы, отдаём как есть.

    Если секрет сменился, расшифровать нечем. Падать здесь нельзя: панель должна
    открыться и сказать «ключи не заданы», чтобы их можно было ввести заново.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    if not is_encrypted(raw):
        return raw
    try:
        blob = base64.urlsafe_b64decode(raw[len(PREFIX):].encode("ascii"))
        return _cipher().decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], None).decode("utf-8")
    except (InvalidTag, ValueError, TypeError):
        log.error(
            "Ключ площадки не расшифровывается — похоже, сменился SECRET_KEY "
            "или файл data/.secret_key. Введите ключи заново в «Настройках»."
        )
        return ""
