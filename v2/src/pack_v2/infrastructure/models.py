from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, Record


class AuditEvent(Record, Base):
    __tablename__ = "audit_events"
    actor_id: Mapped[UUID | None]
    kind: Mapped[str] = mapped_column(String(80))
    subject_id: Mapped[UUID | None]
    details: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")


class Job(Record, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("status IN ('pending','running','succeeded','failed')", name="status"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts"),
        Index("ix_jobs_due", "status", "available_at"),
    )
    kind: Mapped[str] = mapped_column(String(80))
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None]
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
