from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pack_v2.infrastructure.database import Base, Record


class Product(Record, Base):
    __tablename__ = "products"
    name: Mapped[str] = mapped_column(String(500))
    article: Mapped[str | None] = mapped_column(String(200))
    archived: Mapped[bool] = mapped_column(default=False, server_default="false")
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Offer(Record, Base):
    __tablename__ = "offers"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.marketplace_accounts.id"), index=True)
    product_id: Mapped[UUID | None] = mapped_column(ForeignKey("pack_v2.products.id"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    article: Mapped[str | None] = mapped_column(String(256))
    image_url: Mapped[str | None] = mapped_column(String(4096))
    archived: Mapped[bool | None]
    marking_declared: Mapped[bool | None]
    source_present: Mapped[bool] = mapped_column(default=True, server_default="true")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_run_id: Mapped[UUID | None]
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    __table_args__ = (
        UniqueConstraint("account_id", "id"),
        ForeignKeyConstraint(
            ["account_id", "last_seen_run_id"],
            ["pack_v2.catalog_syncs.account_id", "pack_v2.catalog_syncs.id"],
        ),
        Index("ix_offers_listing", "created_at", "id"),
        Index("ix_offers_account_listing", "account_id", "created_at", "id"),
    )


class OfferRef(Base):
    __tablename__ = "offer_refs"
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    offer_id: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "offer_id"], ["pack_v2.offers.account_id", "pack_v2.offers.id"]),
        UniqueConstraint("account_id", "kind", "value"),
        Index("ix_offer_refs_offer", "offer_id"),
        Index("ix_offer_refs_value", "value"),
    )


class CodeFields:
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(String(512), primary_key=True)
    origin: Mapped[str] = mapped_column(String(40))


class ProductCode(CodeFields, Base):
    __tablename__ = "product_codes"
    product_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.products.id"), primary_key=True)
    __table_args__ = (Index("ix_product_codes_value", "value"),)


class OfferCode(CodeFields, Base):
    __tablename__ = "offer_codes"
    offer_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.offers.id"), primary_key=True)
    __table_args__ = (Index("ix_offer_codes_value", "value"), Index("ix_offer_codes_offer", "offer_id"))


class ProductComponent(Base):
    __tablename__ = "product_components"
    product_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.products.id"), primary_key=True)
    part_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    component_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity"),
        CheckConstraint("product_id <> component_id", name="not_self"),
    )


class CatalogSync(Record, Base):
    __tablename__ = "catalog_syncs"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.marketplace_accounts.id"))
    credential_revision: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.jobs.id"), unique=True)
    requested_by: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pages: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    items: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("account_id", "id"),
        CheckConstraint(
            "status IN ('queued','running','retrying','succeeded','failed','stale')", name="status"
        ),
        CheckConstraint("pages >= 0 AND items >= 0 AND credential_revision >= 0", name="progress"),
        Index("ix_catalog_syncs_history", "account_id", "created_at"),
        Index(
            "uq_catalog_syncs_active",
            "account_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running','retrying')"),
        ),
    )
