from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pack_v2.infrastructure.database import Base, Record


class Order(Record, Base):
    __tablename__ = "orders"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.marketplace_accounts.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    __table_args__ = (UniqueConstraint("account_id", "id"),)


class OrderLine(Record, Base):
    __tablename__ = "order_lines"
    account_id: Mapped[UUID]
    order_id: Mapped[UUID] = mapped_column(index=True)
    offer_id: Mapped[UUID | None]
    quantity_source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual")
    external_line_id: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "order_id"], ["pack_v2.orders.account_id", "pack_v2.orders.id"]),
        ForeignKeyConstraint(["account_id", "offer_id"], ["pack_v2.offers.account_id", "pack_v2.offers.id"]),
        CheckConstraint("quantity > 0", name="quantity"),
        CheckConstraint("quantity_source IN ('manual','allocated')", name="quantity_source"),
        CheckConstraint("price IS NULL OR price >= 0", name="price"),
        UniqueConstraint("order_id", "external_line_id"),
        UniqueConstraint("order_id", "id"),
    )


class OrderUnit(Record, Base):
    __tablename__ = "order_units"
    order_id: Mapped[UUID]
    line_id: Mapped[UUID]
    ordinal: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id", "line_id"], ["pack_v2.order_lines.order_id", "pack_v2.order_lines.id"]
        ),
        CheckConstraint("ordinal > 0", name="ordinal"),
        UniqueConstraint("line_id", "ordinal"),
        UniqueConstraint("order_id", "id"),
    )


class Shipment(Record, Base):
    __tablename__ = "shipments"
    account_id: Mapped[UUID]
    order_id: Mapped[UUID]
    parent_id: Mapped[UUID | None]
    source_status: Mapped[str | None] = mapped_column(String(100))
    source_substatus: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(40), default="unknown", server_default="unknown")
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    assembly_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warehouse_id: Mapped[str | None] = mapped_column(String(256))
    warehouse_name: Mapped[str | None] = mapped_column(String(500))
    parent_source_number: Mapped[str | None] = mapped_column(String(256))
    package_count_hint: Mapped[int | None] = mapped_column(Integer)
    needs_review: Mapped[bool] = mapped_column(default=False, server_default="false")
    transition_failed: Mapped[bool] = mapped_column(default=False, server_default="false")
    requirements_complete: Mapped[bool] = mapped_column(default=False, server_default="false")
    last_seen_run_id: Mapped[UUID | None]
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "last_seen_run_id"], ["pack_v2.order_syncs.account_id", "pack_v2.order_syncs.id"]
        ),
        CheckConstraint("package_count_hint IS NULL OR package_count_hint > 0", name="package_count"),
        Index("ix_shipments_listing", "processing_started_at", "id"),
        Index("ix_shipments_account_listing", "account_id", "processing_started_at", "id"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent_not_self"),
        ForeignKeyConstraint(["account_id", "order_id"], ["pack_v2.orders.account_id", "pack_v2.orders.id"]),
        ForeignKeyConstraint(
            ["order_id", "parent_id"], ["pack_v2.shipments.order_id", "pack_v2.shipments.id"]
        ),
        UniqueConstraint("order_id", "id"),
        UniqueConstraint("account_id", "id"),
        Index("ix_shipments_queue", "account_id", "state", "deadline"),
    )


class ShipmentUnit(Record, Base):
    __tablename__ = "shipment_units"
    order_id: Mapped[UUID]
    shipment_id: Mapped[UUID]
    unit_id: Mapped[UUID]
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id", "shipment_id"], ["pack_v2.shipments.order_id", "pack_v2.shipments.id"]
        ),
        ForeignKeyConstraint(
            ["order_id", "unit_id"], ["pack_v2.order_units.order_id", "pack_v2.order_units.id"]
        ),
        UniqueConstraint("shipment_id", "id"),
        UniqueConstraint("shipment_id", "unit_id", "id"),
        Index(
            "uq_shipment_units_current", "unit_id", unique=True, postgresql_where=text("released_at IS NULL")
        ),
    )


class Package(Record, Base):
    __tablename__ = "packages"
    account_id: Mapped[UUID]
    shipment_id: Mapped[UUID]
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "shipment_id"], ["pack_v2.shipments.account_id", "pack_v2.shipments.id"]
        ),
        UniqueConstraint("shipment_id", "id"),
        UniqueConstraint("account_id", "id"),
    )


class PackageContent(Record, Base):
    __tablename__ = "package_contents"
    shipment_id: Mapped[UUID]
    package_id: Mapped[UUID]
    shipment_unit_id: Mapped[UUID]
    part_index: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    part_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["shipment_id", "package_id"], ["pack_v2.packages.shipment_id", "pack_v2.packages.id"]
        ),
        ForeignKeyConstraint(
            ["shipment_id", "shipment_unit_id"],
            ["pack_v2.shipment_units.shipment_id", "pack_v2.shipment_units.id"],
        ),
        CheckConstraint("part_count > 0 AND part_index BETWEEN 1 AND part_count", name="parts"),
        Index(
            "uq_package_contents_part",
            "shipment_unit_id",
            "part_index",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )


class ReferenceFields:
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), primary_key=True)
    authoritative: Mapped[bool] = mapped_column(default=False, server_default="false")


class OrderRef(ReferenceFields, Base):
    __tablename__ = "order_refs"
    order_id: Mapped[UUID] = mapped_column(primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "order_id"], ["pack_v2.orders.account_id", "pack_v2.orders.id"]),
        Index(
            "uq_order_refs_identity",
            "account_id",
            "kind",
            "value",
            unique=True,
            postgresql_where=text("authoritative"),
        ),
        Index("ix_order_refs_value", "value"),
        Index("ix_order_refs_order", "order_id"),
    )


class ShipmentRef(ReferenceFields, Base):
    __tablename__ = "shipment_refs"
    origins: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    shipment_id: Mapped[UUID] = mapped_column(primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "shipment_id"], ["pack_v2.shipments.account_id", "pack_v2.shipments.id"]
        ),
        Index(
            "uq_shipment_refs_identity",
            "account_id",
            "kind",
            "value",
            unique=True,
            postgresql_where=text("authoritative"),
        ),
        Index("ix_shipment_refs_value", "value"),
        Index("ix_shipment_refs_shipment", "shipment_id"),
    )


class PackageRef(ReferenceFields, Base):
    __tablename__ = "package_refs"
    package_id: Mapped[UUID] = mapped_column(primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "package_id"], ["pack_v2.packages.account_id", "pack_v2.packages.id"]
        ),
        Index(
            "uq_package_refs_identity",
            "account_id",
            "kind",
            "value",
            unique=True,
            postgresql_where=text("authoritative"),
        ),
        Index("ix_package_refs_value", "value"),
    )


class OrderSync(Record, Base):
    __tablename__ = "order_syncs"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.marketplace_accounts.id"))
    credential_revision: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.jobs.id"), unique=True)
    requested_by: Mapped[UUID | None]
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    target_shipment_id: Mapped[UUID | None]
    expected_shipment_revision: Mapped[int | None] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pages: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    items: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "(mode='point') = (target_shipment_id IS NOT NULL) AND (mode='point') = (expected_shipment_revision IS NOT NULL)",
            name="target",
        ),
        Index(
            "uq_order_syncs_point",
            "account_id",
            "target_shipment_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running','retrying') AND mode='point'"),
        ),
        UniqueConstraint("account_id", "id"),
        CheckConstraint("since <= until", name="period"),
        CheckConstraint(
            "expected_shipment_revision IS NULL OR expected_shipment_revision >= 0", name="target_revision"
        ),
        CheckConstraint("mode IN ('full','working','point')", name="mode"),
        CheckConstraint(
            "status IN ('queued','running','retrying','succeeded','failed','stale')", name="status"
        ),
        CheckConstraint("pages >= 0 AND items >= 0 AND credential_revision >= 0", name="progress"),
        Index("ix_order_syncs_history", "account_id", "created_at"),
        Index(
            "uq_order_syncs_active",
            "account_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running','retrying') AND mode <> 'point'"),
        ),
    )


class ShipmentItem(Record, Base):
    """Observed marketplace composition; not a physical OrderUnit or a package allocation."""

    __tablename__ = "shipment_items"
    account_id: Mapped[UUID]
    shipment_id: Mapped[UUID]
    source_line_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(500))
    article: Mapped[str | None] = mapped_column(String(256))
    references: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    offer_id: Mapped[UUID | None]
    match_state: Mapped[str] = mapped_column(String(20), default="unmatched", server_default="unmatched")
    required_fields: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    optional_fields: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    source_present: Mapped[bool] = mapped_column(default=True, server_default="true")
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "shipment_id"], ["pack_v2.shipments.account_id", "pack_v2.shipments.id"]
        ),
        ForeignKeyConstraint(["account_id", "offer_id"], ["pack_v2.offers.account_id", "pack_v2.offers.id"]),
        UniqueConstraint("shipment_id", "source_line_id"),
        UniqueConstraint("shipment_id", "id"),
        CheckConstraint("quantity > 0", name="quantity"),
        CheckConstraint("price IS NULL OR price >= 0", name="price"),
        CheckConstraint("match_state IN ('matched','unmatched','conflict')", name="match_state"),
        CheckConstraint("(match_state='matched') = (offer_id IS NOT NULL)", name="match_target"),
        Index("ix_shipment_items_shipment", "shipment_id"),
        Index("ix_shipment_items_offer", "offer_id"),
        Index("ix_shipment_items_references", "references", postgresql_using="gin"),
    )


class ShipmentRelation(Base):
    __tablename__ = "shipment_relations"
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    shipment_id: Mapped[UUID] = mapped_column(primary_key=True)
    related_number: Mapped[str] = mapped_column(String(256), primary_key=True)
    __table_args__ = (
        Index("ix_shipment_relations_shipment", "shipment_id"),
        ForeignKeyConstraint(
            ["account_id", "shipment_id"], ["pack_v2.shipments.account_id", "pack_v2.shipments.id"]
        ),
    )
