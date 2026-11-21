from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from pack_v2.infrastructure.models import AuditEvent
from pack_v2.shared.errors import NotFound

from .models import AccountCheck, AccountSecret, MarketplaceAccount


def account(tx: Session, account_id: UUID, *, lock: bool = False) -> MarketplaceAccount:
    query = select(MarketplaceAccount).where(MarketplaceAccount.id == account_id)
    saved = tx.scalar(query.with_for_update() if lock else query)
    if saved is None:
        raise NotFound("Кабинет не найден")
    return saved


def save_secret(tx: Session, account_id: UUID, kind: str, ciphertext: bytes) -> None:
    saved = tx.get(AccountSecret, (account_id, kind))
    if saved:
        saved.ciphertext = ciphertext
    else:
        tx.add(AccountSecret(account_id=account_id, kind=kind, ciphertext=ciphertext))


def secrets_for(tx: Session, account_id: UUID) -> dict[str, bytes]:
    return {
        row.kind: row.ciphertext
        for row in tx.scalars(select(AccountSecret).where(AccountSecret.account_id == account_id))
    }


def listing(tx: Session):
    accounts = tx.scalars(
        select(MarketplaceAccount).order_by(MarketplaceAccount.created_at, MarketplaceAccount.id)
    ).all()
    ids = [a.id for a in accounts]
    secrets = {}
    for row in tx.scalars(select(AccountSecret).where(AccountSecret.account_id.in_(ids))):
        secrets.setdefault(row.account_id, {})[row.kind] = row.ciphertext
    checks = {
        row.account_id: row
        for row in tx.scalars(
            select(AccountCheck)
            .where(AccountCheck.account_id.in_(ids))
            .distinct(AccountCheck.account_id)
            .order_by(AccountCheck.account_id, AccountCheck.created_at.desc(), AccountCheck.id.desc())
        )
    }
    return accounts, secrets, checks


def running(tx: Session, account_id: UUID) -> AccountCheck | None:
    return tx.scalar(
        select(AccountCheck).where(AccountCheck.account_id == account_id, AccountCheck.finished_at.is_(None))
    )


def invalidate_running(tx: Session, account_id: UUID) -> None:
    tx.execute(
        update(AccountCheck)
        .where(AccountCheck.account_id == account_id, AccountCheck.finished_at.is_(None))
        .values(status="stale", finished_at=datetime.now(UTC), results=[])
    )


def audit(tx: Session, actor_id: UUID, subject_id: UUID, kind: str) -> None:
    tx.add(AuditEvent(actor_id=actor_id, subject_id=subject_id, kind=kind))
