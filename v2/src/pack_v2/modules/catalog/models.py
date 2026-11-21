from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
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
    archived: Mapped[bool] = mapped_column(default=False, server_default="false")
    __table_args__ = (UniqueConstraint("account_id", "id"),)


class OfferRef(Base):
    __tablename__ = "offer_refs"
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    offer_id: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "offer_id"], ["pack_v2.offers.account_id", "pack_v2.offers.id"]),
        UniqueConstraint("account_id", "kind", "value"),
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
    __table_args__ = (Index("ix_offer_codes_value", "value"),)


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
