from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from pack_v2.infrastructure.database import Base, Record


class MarketplaceAccount(Record, Base):
    __tablename__ = "marketplace_accounts"
    __table_args__ = (CheckConstraint("length(marketplace) > 0", name="marketplace"),)
    marketplace: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(default=True, server_default="true")
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class AccountSecret(Base):
    __tablename__ = "account_secrets"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("pack_v2.marketplace_accounts.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
