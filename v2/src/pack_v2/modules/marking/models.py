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
from sqlalchemy.orm import Mapped, mapped_column

from pack_v2.infrastructure.database import Base, Record


class MarkingUnit(Record, Base):
    __tablename__ = "marking_units"
    identity: Mapped[str] = mapped_column(String(64), unique=True)
    full_fingerprint: Mapped[str] = mapped_column(String(64))
    gtin: Mapped[str] = mapped_column(String(14))
    serial: Mapped[str] = mapped_column(String(20))
    encrypted_code: Mapped[bytes] = mapped_column(LargeBinary)


class MarkingAssignment(Record, Base):
    __tablename__ = "marking_assignments"
    mark_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.marking_units.id"))
    account_id: Mapped[UUID]
    order_id: Mapped[UUID]
    shipment_id: Mapped[UUID]
    unit_id: Mapped[UUID]
    allocation_id: Mapped[UUID]
    posting_number: Mapped[str] = mapped_column(String(256))
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("pack_v2.users.id"))
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "shipment_id"], ["pack_v2.shipments.account_id", "pack_v2.shipments.id"]
        ),
        ForeignKeyConstraint(
            ["order_id", "shipment_id"], ["pack_v2.shipments.order_id", "pack_v2.shipments.id"]
        ),
        ForeignKeyConstraint(
            ["order_id", "unit_id"], ["pack_v2.order_units.order_id", "pack_v2.order_units.id"]
        ),
        ForeignKeyConstraint(
            ["shipment_id", "unit_id", "allocation_id"],
            [
                "pack_v2.shipment_units.shipment_id",
                "pack_v2.shipment_units.unit_id",
                "pack_v2.shipment_units.id",
            ],
        ),
        UniqueConstraint("shipment_id", "unit_id", "id"),
        Index(
            "uq_marking_current_identity",
            "mark_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        Index(
            "uq_marking_current_unit", "unit_id", unique=True, postgresql_where=text("released_at IS NULL")
        ),
        Index("ix_marking_assignment_shipment", "shipment_id", "released_at"),
        CheckConstraint("revision >= 0", name="revision"),
        CheckConstraint("(released_at IS NULL) = (release_reason IS NULL)", name="release"),
    )


class MarkingTransmission(Record, Base):
    __tablename__ = "marking_transmissions"
    assignment_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.marking_assignments.id"))
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.instance_submissions.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    __table_args__ = (
        UniqueConstraint("assignment_id", "batch_id"),
        CheckConstraint("status IN ('pending','accepted','rejected','unknown')", name="status"),
    )
