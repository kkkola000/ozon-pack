"""Конфигурация приложения (читается из окружения / .env)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Минимальный парсер .env — без внешних зависимостей."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


def _list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    # --- Ozon Seller API ---
    ozon_client_id: str = field(default_factory=lambda: os.getenv("OZON_CLIENT_ID", "").strip())
    ozon_api_key: str = field(default_factory=lambda: os.getenv("OZON_API_KEY", "").strip())
    ozon_base_url: str = field(default_factory=lambda: os.getenv("OZON_API_URL", "https://api-seller.ozon.ru").rstrip("/"))
    ozon_timeout: int = field(default_factory=lambda: _int("OZON_TIMEOUT", 60))

    # Демо-режим включён принудительно через OZON_DEMO=1. Если ключей нет,
    # панель уходит в демо и без этого флага — см. credentials.is_demo().
    demo_forced: bool = field(default_factory=lambda: _bool("OZON_DEMO", False))

    # --- Приложение ---
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "").strip())
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", str(BASE_DIR / "data" / "ozon-pack.db")))
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 8080))
    admin_login: str = field(default_factory=lambda: os.getenv("ADMIN_LOGIN", "admin").strip())
    admin_password: str = field(default_factory=lambda: os.getenv("ADMIN_PASSWORD", "").strip())
    session_ttl_hours: int = field(default_factory=lambda: _int("SESSION_TTL_HOURS", 24))
    ip_allowlist: list[str] = field(default_factory=lambda: _list("IP_ALLOWLIST", ""))

    # --- Синхронизация ---
    sync_interval: int = field(default_factory=lambda: _int("SYNC_INTERVAL", 60))
    sync_returns_interval: int = field(default_factory=lambda: _int("SYNC_RETURNS_INTERVAL", 300))
    sync_days_back: int = field(default_factory=lambda: _int("SYNC_DAYS_BACK", 30))
    sync_days_forward: int = field(default_factory=lambda: _int("SYNC_DAYS_FORWARD", 30))
    sync_enabled: bool = field(default_factory=lambda: _bool("SYNC_ENABLED", True))

    # --- Логика сборки ---
    # Требовать скан каждого товара в отправлении перед закрытием.
    require_all_items: bool = field(default_factory=lambda: _bool("REQUIRE_ALL_ITEMS", True))
    # Печатать стикер автоматически при выборе отправления.
    autoprint: bool = field(default_factory=lambda: _bool("AUTOPRINT", True))
    # Собирать отправление (v4/posting/fbs/ship) прямо при скане товара.
    auto_ship_on_scan: bool = field(default_factory=lambda: _bool("AUTO_SHIP_ON_SCAN", False))
    # Сколько минут отправление держится за сборщиком.
    claim_ttl_minutes: int = field(default_factory=lambda: _int("CLAIM_TTL_MINUTES", 30))
    # Статусы возвратов, считающиеся «готов к выдаче» (sys_name из API, через запятую).
    returns_ready_statuses: list[str] = field(
        default_factory=lambda: _list(
            # sys_name из /v1/returns/list. «В пункте выдачи» = ArrivedAtReturnPlace:
            # именно эти возвраты сборщик может забрать. Эти же статусы
            # запрашиваются у Ozon при синхронизации.
            "RETURNS_READY_STATUSES",
            "ArrivedAtReturnPlace",
        )
    )
    timezone_offset: int = field(default_factory=lambda: _int("TZ_OFFSET_HOURS", 3))

    def __post_init__(self) -> None:
        if not self.secret_key:
            # Ключ переживает рестарт: иначе сессии сбрасываются при каждом деплое.
            key_file = Path(self.db_path).parent / ".secret_key"
            key_file.parent.mkdir(parents=True, exist_ok=True)
            if key_file.exists():
                self.secret_key = key_file.read_text(encoding="utf-8").strip()
            else:
                self.secret_key = secrets.token_urlsafe(48)
                key_file.write_text(self.secret_key, encoding="utf-8")
                key_file.chmod(0o600)
    @property
    def configured(self) -> bool:
        return bool(self.ozon_client_id and self.ozon_api_key)


settings = Settings()
