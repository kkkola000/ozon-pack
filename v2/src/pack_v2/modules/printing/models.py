"""Print intentions and delivery acknowledgements; never a claim of physical printing."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pack_v2.infrastructure.database import Base, Record


class PrintSettings(Base):
    __tablename__ = "print_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    product_barcodes: Mapped[bool] = mapped_column(default=False, server_default="false")
    marking_duplicates: Mapped[bool] = mapped_column(default=False, server_default="false")
    profiles: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    encrypted_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    certificate: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("id = 1 AND revision >= 0", name="singleton"),
        CheckConstraint("(encrypted_key IS NULL) = (certificate IS NULL)", name="signing_pair"),
    )


class PrintJob(Record, Base):
    __tablename__ = "print_jobs"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.users.id"))
    request_id: Mapped[UUID]
    fingerprint: Mapped[str] = mapped_column(String(64))
    automatic_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    section: Mapped[str] = mapped_column(String(16))
    session_id: Mapped[UUID | None] = mapped_column(ForeignKey("pack_v2.assembly_sessions.id"))
    kind: Mapped[str] = mapped_column(String(32))
    label_id: Mapped[UUID | None] = mapped_column(ForeignKey("pack_v2.shipment_labels.id"))
    generation: Mapped[int | None] = mapped_column(Integer)
    assignments: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    product_codes: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    profile: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20), default="prepared", server_default="prepared")
    dispatch_token: Mapped[UUID]
    delivery_token: Mapped[UUID | None]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("user_id", "request_id"),
        CheckConstraint("section IN ('pack','stickers','test')", name="section"),
        CheckConstraint("kind IN ('ozon:label','marking:duplicate')", name="kind"),
        CheckConstraint(
            "state IN ('prepared','dispatching','submitted','browser','uncertain','failed')", name="state"
        ),
        CheckConstraint("generation IS NULL OR generation > 0", name="generation"),
        CheckConstraint("(label_id IS NULL) = (generation IS NULL)", name="label_generation"),
    )
