from pathlib import Path
from typing import Literal, Self

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    # No automatic .env lookup: the legacy root .env must never be consumed.
    model_config = SettingsConfigDict(env_prefix="PACK_V2_", extra="ignore", hide_input_in_errors=True)

    database_url: SecretStr
    secret_key: SecretStr
    encryption_key: SecretStr
    environment: Literal["development", "test", "production"] = "development"
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    secure_cookies: bool = False
    session_hours: int = Field(default=12, ge=1, le=168)
    login_limit: int = Field(default=10, ge=1, le=100)
    login_window_seconds: int = Field(default=300, ge=10, le=3600)
    catalog_sync_minutes: int = Field(default=15, ge=0, le=1440)
    orders_sync_minutes: int = Field(default=5, ge=0, le=1440)
    orders_lookback_days: int = Field(default=30, ge=1, le=90)

    @model_validator(mode="after")
    def validate_separation(self) -> Self:
        url = make_url(self.database_url.get_secret_value())
        if url.drivername != "postgresql+psycopg" or not url.database:
            raise ValueError("Нужна отдельная PostgreSQL: postgresql+psycopg://…/pack_v2")
        if len(self.secret_key.get_secret_value()) < 32:
            raise ValueError("PACK_V2_SECRET_KEY должен содержать не менее 32 символов")
        Fernet(self.encryption_key.get_secret_value().encode("ascii"))
        if not self.allowed_hosts or "*" in self.allowed_hosts:
            raise ValueError("Укажите конкретные PACK_V2_ALLOWED_HOSTS")
        if self.environment == "production" and not self.secure_cookies:
            raise ValueError("Для production нужны HTTPS и PACK_V2_SECURE_COOKIES=true")
        return self


def load_settings(env_file: Path | None = None) -> Settings:
    return Settings(_env_file=env_file)
