from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pack_v2.infrastructure.database import Base, Record


class MarketplaceAccount(Record, Base):
    __tablename__ = "marketplace_accounts"
    __table_args__ = (
        CheckConstraint("length(marketplace) > 0", name="marketplace"),
        UniqueConstraint("marketplace", "identity_fingerprint"),
        CheckConstraint("credential_revision >= 0", name="credential_revision"),
    )
    marketplace: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(default=True, server_default="true")
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    identity_fingerprint: Mapped[str | None] = mapped_column(String(64))
    credential_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AccountSecret(Base):
    __tablename__ = "account_secrets"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.marketplace_accounts.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)


class AccountCheck(Record, Base):
    __tablename__ = "account_checks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('checking','passed','partial','failed','unavailable','stale')", name="status"
        ),
        Index("ix_account_checks_history", "account_id", "created_at"),
        Index(
            "uq_account_checks_running",
            "account_id",
            unique=True,
            postgresql_where=text("finished_at IS NULL"),
        ),
    )
    account_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.marketplace_accounts.id"))
    credential_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="checking", server_default="checking")
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    results: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
