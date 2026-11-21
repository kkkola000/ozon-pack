from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pack_v2.infrastructure.database import Base, Record


class User(Record, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('owner','admin','packer')", name="role"),)
    login: Mapped[str] = mapped_column(String(64))
    normalized_login: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(default=True, server_default="true")
    auth_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class UserSection(Base):
    __tablename__ = "user_sections"
    __table_args__ = (
        CheckConstraint(
            "section IN ('pack','orders','returns','products','reports','logs','stickers')", name="section"
        ),
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("pack_v2.users.id", ondelete="CASCADE"), primary_key=True
    )
    section: Mapped[str] = mapped_column(String(24), primary_key=True)


class LoginSession(Record, Base):
    __tablename__ = "sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.users.id"), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    auth_revision: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginBudget(Base):
    __tablename__ = "login_budgets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)
