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
    UniqueConstraint,
    text,
)
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
    external_line_id: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "order_id"], ["pack_v2.orders.account_id", "pack_v2.orders.id"]),
        ForeignKeyConstraint(["account_id", "offer_id"], ["pack_v2.offers.account_id", "pack_v2.offers.id"]),
        CheckConstraint("quantity > 0", name="quantity"),
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
    __table_args__ = (
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
    )


class ShipmentRef(ReferenceFields, Base):
    __tablename__ = "shipment_refs"
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
