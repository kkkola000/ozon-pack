from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pack_v2.shared.errors import Conflict

from .database import Database
from .models import Job


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    kind: str
    payload: dict
    lease_token: UUID
    attempts: int


def enqueue(tx: Session, kind: str, dedup_key: str, payload: dict | None = None) -> UUID:
    """Call inside the business transaction; payload contains IDs, never secrets."""
    tx.execute(
        insert(Job)
        .values(id=uuid4(), kind=kind, dedup_key=dedup_key, payload=payload or {})
        .on_conflict_do_nothing(index_elements=[Job.dedup_key])
    )
    saved = tx.scalar(select(Job).where(Job.dedup_key == dedup_key))
    if saved.kind != kind or saved.payload != (payload or {}):
        raise Conflict("Ключ задания уже использован для другого действия")
    return saved.id


class Queue:
    def __init__(self, db: Database):
        self.db = db

    def claim(self, lease_seconds: int = 60) -> ClaimedJob | None:
        now = datetime.now(UTC)
        with self.db.transaction() as tx:
            tx.execute(
                update(Job)
                .where(Job.status == "running", Job.lease_until < now, Job.attempts >= Job.max_attempts)
                .values(
                    status="failed", finished_at=now, error="Превышено число попыток после потери исполнителя"
                )
            )
            due = or_(
                and_(Job.status == "pending", Job.available_at <= now),
                and_(Job.status == "running", Job.lease_until < now),
            )
            job = tx.scalar(
                select(Job)
                .where(due, Job.attempts < Job.max_attempts)
                .order_by(Job.available_at, Job.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.status, job.lease_token = "running", uuid4()
            job.lease_until, job.attempts = now + timedelta(seconds=lease_seconds), job.attempts + 1
            return ClaimedJob(job.id, job.kind, job.payload, job.lease_token, job.attempts)

    def finish(self, job: ClaimedJob, *, retry: bool = False, failed: bool = False) -> bool:
        now = datetime.now(UTC)
        with self.db.transaction() as tx:
            saved = tx.scalar(
                select(Job)
                .where(
                    Job.id == job.id,
                    Job.status == "running",
                    Job.lease_token == job.lease_token,
                    Job.lease_until > now,
                )
                .with_for_update()
            )
            if saved is None:
                return False
            if retry and saved.attempts < saved.max_attempts:
                saved.status = "pending"
                saved.available_at = now + timedelta(seconds=min(300, 2**saved.attempts))
            else:
                saved.status = "failed" if retry or failed else "succeeded"
                saved.finished_at = now
            saved.error = "Задание не выполнено; проверьте журнал исполнителя" if retry or failed else None
            saved.lease_until, saved.lease_token = None, None
            return True
