"""One current original label per shipment/format. No PDF version archive."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pack_v2.infrastructure.database import Base, Record


class ShipmentLabel(Record, Base):
    __tablename__ = "shipment_labels"
    account_id: Mapped[UUID]
    shipment_id: Mapped[UUID]
    size: Mapped[str] = mapped_column(String(8))
    basis: Mapped[str] = mapped_column(String(64))
    credential_revision: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(20))
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("pack_v2.jobs.id"), unique=True)
    task_id: Mapped[str | None] = mapped_column(String(32))
    encrypted_pdf: Mapped[bytes | None] = mapped_column(LargeBinary)
    checksum: Mapped[str | None] = mapped_column(String(64))
    byte_count: Mapped[int | None] = mapped_column(Integer)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archive_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    error: Mapped[str | None] = mapped_column(String(500))
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "shipment_id"], ["pack_v2.shipments.account_id", "pack_v2.shipments.id"]
        ),
        UniqueConstraint("shipment_id", "size"),
        Index("ix_shipment_labels_archive", "received_at", "id"),
        Index("ix_shipment_labels_archive_activity", text("coalesce(received_at, created_at)"), "id"),
        Index(
            "ix_shipment_labels_archive_account",
            "account_id",
            text("coalesce(received_at, created_at)"),
            "id",
        ),
        CheckConstraint("size IN ('small','big')", name="size"),
        CheckConstraint(
            "status IN ('waiting_ready','queued','processing','ready','failed','stale','expired')",
            name="status",
        ),
        CheckConstraint("generation > 0 AND credential_revision > 0", name="revision"),
        CheckConstraint(
            "(encrypted_pdf IS NULL) = (checksum IS NULL) AND (encrypted_pdf IS NULL) = (byte_count IS NULL)",
            name="file",
        ),
        CheckConstraint(
            "status <> 'ready' OR (encrypted_pdf IS NOT NULL AND received_at IS NOT NULL)", name="ready_file"
        ),
        CheckConstraint("byte_count IS NULL OR byte_count BETWEEN 1 AND 8388608", name="byte_count"),
    )


class StickerSearch(Record, Base):
    __tablename__ = "sticker_searches"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.users.id"))
    session_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.sessions.id", ondelete="CASCADE"))
    encrypted_filters: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
