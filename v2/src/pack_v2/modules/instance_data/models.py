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


class InstanceContext(Record, Base):
    __tablename__ = "instance_contexts"
    account_id: Mapped[UUID]
    order_id: Mapped[UUID]
    shipment_id: Mapped[UUID]
    credential_revision: Mapped[int] = mapped_column(Integer)
    source_revision: Mapped[int] = mapped_column(Integer)
    basis: Mapped[str] = mapped_column(String(64))
    posting_number: Mapped[str] = mapped_column(String(256))
    job_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.jobs.id"), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued")
    policy_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    requirements: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    encrypted_snapshot: Mapped[bytes | None] = mapped_column(LargeBinary)
    signature: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String(500))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "shipment_id"], ["pack_v2.shipments.account_id", "pack_v2.shipments.id"]
        ),
        ForeignKeyConstraint(
            ["order_id", "shipment_id"], ["pack_v2.shipments.order_id", "pack_v2.shipments.id"]
        ),
        UniqueConstraint("account_id", "shipment_id", "id"),
        Index("ix_instance_context_shipment", "shipment_id", "created_at"),
        Index(
            "uq_instance_discovery_active",
            "shipment_id",
            unique=True,
            postgresql_where=text("status IN ('queued','reading')"),
        ),
        CheckConstraint("status IN ('queued','reading','ready','failed','stale')", name="status"),
        CheckConstraint(
            "credential_revision >= 0 AND source_revision >= 0 AND policy_version > 0", name="revision"
        ),
    )


class UnitDetail(Base):
    __tablename__ = "unit_details"
    unit_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID]
    order_id: Mapped[UUID]
    shipment_id: Mapped[UUID]
    allocation_id: Mapped[UUID]
    product_id: Mapped[str] = mapped_column(String(64))
    exemplar_id: Mapped[str] = mapped_column(String(64))
    item_id: Mapped[UUID]
    encrypted_values: Mapped[bytes] = mapped_column(LargeBinary)
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    input_errors: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
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
        UniqueConstraint("shipment_id", "exemplar_id"),
        Index("ix_unit_details_shipment", "shipment_id"),
        CheckConstraint("revision >= 0", name="revision"),
    )


class Submission(Record, Base):
    __tablename__ = "instance_submissions"
    context_id: Mapped[UUID]
    account_id: Mapped[UUID]
    shipment_id: Mapped[UUID]
    fingerprint: Mapped[str] = mapped_column(String(64))
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary)
    # Immutable content; only this execution/proof state changes.
    status: Mapped[str] = mapped_column(String(20), default="prepared", server_default="prepared")
    job_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.jobs.id"), unique=True)
    set_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    update_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(500))
    field_errors: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    field_results: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "shipment_id", "context_id"],
            [
                "pack_v2.instance_contexts.account_id",
                "pack_v2.instance_contexts.shipment_id",
                "pack_v2.instance_contexts.id",
            ],
        ),
        UniqueConstraint("context_id", "fingerprint"),
        Index(
            "uq_instance_submission_active",
            "shipment_id",
            unique=True,
            postgresql_where=text("status IN ('prepared','checking')"),
        ),
        Index("ix_instance_submission_context", "context_id", "created_at"),
        CheckConstraint(
            "status IN ('prepared','checking','accepted','rejected','uncertain','stale')", name="status"
        ),
    )
