from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pack_v2.infrastructure.models import AuditEvent

from .domain import Authenticated, Principal
from .models import LoginBudget, LoginSession, User, UserSection


def principal(session: Session, user: User) -> Principal:
    sections = session.scalars(select(UserSection.section).where(UserSection.user_id == user.id))
    return Principal(user.id, user.login, user.role, frozenset(sections))


def find_user(session: Session, normalized: str) -> User | None:
    return session.scalar(select(User).where(User.normalized_login == normalized))


def lock_bootstrap(session: Session) -> bool:
    session.execute(text("SELECT pg_advisory_xact_lock(721047226)"))
    return session.scalar(select(User.id).where(User.role == "owner")) is not None


def add_user(
    session: Session, login: str, normalized: str, password_hash: str, role: str, sections: frozenset[str]
) -> Principal:
    user = User(login=login, normalized_login=normalized, password_hash=password_hash, role=role)
    session.add(user)
    session.flush()
    session.add_all(UserSection(user_id=user.id, section=s) for s in sorted(sections))
    session.flush()
    return principal(session, user)


def audit(session: Session, kind: str, actor_id: UUID | None = None, subject_id: UUID | None = None) -> None:
    session.add(AuditEvent(kind=kind, actor_id=actor_id, subject_id=subject_id))


def take_budget(session: Session, keys: list[tuple[str, int]], window: int) -> bool:
    now = datetime.now(UTC)
    exceeded = False
    for key, limit in sorted(keys):
        query = insert(LoginBudget).values(key=key, started_at=now, attempts=1)
        from sqlalchemy import case

        fresh = LoginBudget.started_at <= now - timedelta(seconds=window)
        query = query.on_conflict_do_update(
            index_elements=[LoginBudget.key],
            set_={
                "started_at": case((fresh, now), else_=LoginBudget.started_at),
                "attempts": case((fresh, 1), else_=LoginBudget.attempts + 1),
            },
        ).returning(LoginBudget.attempts)
        exceeded |= session.scalar(query) > limit
    return exceeded


def revoke(session: Session, digest: str) -> None:
    session.execute(
        update(LoginSession)
        .where(LoginSession.token_digest == digest, LoginSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


def read_session(session: Session, digest: str) -> Authenticated | None:
    result = session.execute(
        select(LoginSession, User)
        .join(User, User.id == LoginSession.user_id)
        .where(
            LoginSession.token_digest == digest,
            LoginSession.revoked_at.is_(None),
            LoginSession.expires_at > datetime.now(UTC),
            User.active.is_(True),
            LoginSession.auth_revision == User.auth_revision,
        )
    ).one_or_none()
    if not result:
        return None
    saved, user = result
    return Authenticated(principal(session, user), saved.id, saved.csrf_token)


def add_session(session: Session, user: User, digest: str, csrf: str, hours: int) -> None:
    session.add(
        LoginSession(
            user_id=user.id,
            token_digest=digest,
            csrf_token=csrf,
            auth_revision=user.auth_revision,
            expires_at=datetime.now(UTC) + timedelta(hours=hours),
        )
    )


def prune(session: Session) -> None:
    now = datetime.now(UTC)
    session.execute(delete(LoginSession).where(LoginSession.expires_at < now - timedelta(days=7)))
    session.execute(delete(LoginBudget).where(LoginBudget.started_at < now - timedelta(days=1)))
