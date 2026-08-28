"""Аутентификация: пароли, сессии, защита от подбора и CSRF."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings
from . import db

SESSION_COOKIE = "ozp_session"
PBKDF_ROUNDS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF_ROUNDS)
    return f"pbkdf2_sha256${PBKDF_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def random_password(length: int = 12) -> str:
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


_serializer = URLSafeTimedSerializer(settings.secret_key, salt="ozon-pack-session")


def make_session(user_id: int, login: str, role: str) -> str:
    return _serializer.dumps(
        {"uid": user_id, "login": login, "role": role, "csrf": secrets.token_urlsafe(16)}
    )


def read_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        return _serializer.loads(token, max_age=settings.session_ttl_hours * 3600)
    except (BadSignature, SignatureExpired):
        return None


# --- Защита от подбора пароля ---
_attempts: dict[str, list[float]] = {}
MAX_ATTEMPTS = 10
ATTEMPT_WINDOW = 300


def too_many_attempts(key: str) -> bool:
    now = time.time()
    hits = [t for t in _attempts.get(key, []) if now - t < ATTEMPT_WINDOW]
    _attempts[key] = hits
    return len(hits) >= MAX_ATTEMPTS


def register_attempt(key: str) -> None:
    _attempts.setdefault(key, []).append(time.time())


def clear_attempts(key: str) -> None:
    _attempts.pop(key, None)


def ip_allowed(client_ip: str | None) -> bool:
    """Опциональный белый список IP/подсетей (IP_ALLOWLIST)."""
    if not settings.ip_allowlist:
        return True
    if not client_ip:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in settings.ip_allowlist:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def get_user(user_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT id, login, role, active FROM users WHERE id = ?", (user_id,))
    if not row or not row["active"]:
        return None
    return dict(row)
