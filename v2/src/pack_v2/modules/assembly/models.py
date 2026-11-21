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

ACTIVE = ("opening", "scanning", "blocked")


class AssemblySession(Record, Base):
    __tablename__ = "assembly_sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.users.id"))
    account_id: Mapped[UUID]
    order_id: Mapped[UUID]
    shipment_id: Mapped[UUID]
    credential_revision: Mapped[int] = mapped_column(Integer)
    source_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    basis: Mapped[str | None] = mapped_column(String(64))
    posting_number: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(20), default="opening", server_default="opening")
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_token: Mapped[UUID]
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_run_id: Mapped[UUID]
    last_unit_id: Mapped[UUID | None]
    initial_code: Mapped[bytes | None] = mapped_column(LargeBinary)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message: Mapped[str | None] = mapped_column(String(500))
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "shipment_id"], ["pack_v2.shipments.account_id", "pack_v2.shipments.id"]
        ),
        ForeignKeyConstraint(
            ["order_id", "shipment_id"], ["pack_v2.shipments.order_id", "pack_v2.shipments.id"]
        ),
        ForeignKeyConstraint(
            ["account_id", "refresh_run_id"], ["pack_v2.order_syncs.account_id", "pack_v2.order_syncs.id"]
        ),
        UniqueConstraint("id", "shipment_id", "order_id"),
        UniqueConstraint("user_id", "id"),
        ForeignKeyConstraint(
            ["order_id", "last_unit_id"], ["pack_v2.order_units.order_id", "pack_v2.order_units.id"]
        ),
        CheckConstraint(
            "status IN ('opening','scanning','blocked','verified','cancelled','stale','expired')",
            name="status",
        ),
        CheckConstraint(
            "revision >= 0 AND source_revision >= 0 AND credential_revision >= 0", name="revision"
        ),
        Index(
            "uq_assembly_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('opening','scanning','blocked')"),
        ),
        Index(
            "uq_assembly_active_shipment",
            "shipment_id",
            unique=True,
            postgresql_where=text("status IN ('opening','scanning','blocked')"),
        ),
        Index(
            "uq_assembly_verified_basis",
            "shipment_id",
            "source_revision",
            unique=True,
            postgresql_where=text("status='verified'"),
        ),
        Index("ix_assembly_user_history", "user_id", "created_at"),
        Index("ix_assembly_shipment_history", "shipment_id", "created_at"),
    )


class AssemblyLine(Base):
    __tablename__ = "assembly_lines"
    session_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.assembly_sessions.id"), primary_key=True)
    item_id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    article: Mapped[str | None] = mapped_column(String(256))
    source_line_id: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer)
    references: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    marking_hint: Mapped[bool] = mapped_column(default=False, server_default="false")
    __table_args__ = (CheckConstraint("quantity > 0", name="quantity"),)


class AssemblyUnit(Record, Base):
    __tablename__ = "assembly_units"
    session_id: Mapped[UUID]
    account_id: Mapped[UUID]
    order_id: Mapped[UUID]
    shipment_id: Mapped[UUID]
    item_id: Mapped[UUID]
    unit_id: Mapped[UUID]
    allocation_id: Mapped[UUID]
    ordinal: Mapped[int] = mapped_column(Integer)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "shipment_id"], ["pack_v2.shipments.account_id", "pack_v2.shipments.id"]
        ),
        ForeignKeyConstraint(
            ["session_id", "shipment_id", "order_id"],
            [
                "pack_v2.assembly_sessions.id",
                "pack_v2.assembly_sessions.shipment_id",
                "pack_v2.assembly_sessions.order_id",
            ],
        ),
        ForeignKeyConstraint(
            ["session_id", "item_id"], ["pack_v2.assembly_lines.session_id", "pack_v2.assembly_lines.item_id"]
        ),
        ForeignKeyConstraint(
            ["shipment_id", "item_id"], ["pack_v2.shipment_items.shipment_id", "pack_v2.shipment_items.id"]
        ),
        ForeignKeyConstraint(
            ["shipment_id", "unit_id", "allocation_id"],
            [
                "pack_v2.shipment_units.shipment_id",
                "pack_v2.shipment_units.unit_id",
                "pack_v2.shipment_units.id",
            ],
        ),
        ForeignKeyConstraint(
            ["order_id", "unit_id"], ["pack_v2.order_units.order_id", "pack_v2.order_units.id"]
        ),
        UniqueConstraint("session_id", "unit_id"),
        UniqueConstraint("session_id", "item_id", "ordinal"),
        CheckConstraint("ordinal > 0", name="ordinal"),
        Index("ix_assembly_units_session", "session_id"),
    )


class AssemblyCommand(Base):
    __tablename__ = "assembly_commands"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.users.id"), primary_key=True)
    command_id: Mapped[UUID] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[UUID | None]
    action: Mapped[str] = mapped_column(String(24))
    outcome: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "session_id"], ["pack_v2.assembly_sessions.user_id", "pack_v2.assembly_sessions.id"]
        ),
    )
