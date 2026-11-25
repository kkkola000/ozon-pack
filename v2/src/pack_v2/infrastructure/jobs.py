import logging
import threading
from contextlib import contextmanager
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

    @staticmethod
    def owned(tx: Session, job: ClaimedJob) -> Job:
        saved = tx.scalar(
            select(Job)
            .where(
                Job.id == job.id,
                Job.status == "running",
                Job.lease_token == job.lease_token,
                Job.lease_until > datetime.now(UTC),
            )
            .with_for_update()
        )
        if saved is None:
            raise LeaseLost("Исполнитель задания устарел")
        return saved

    def renew(self, job: ClaimedJob, lease_seconds: int = 120) -> bool:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid lease length")
        with self.db.transaction() as tx:
            result = tx.execute(
                update(Job)
                .where(
                    Job.id == job.id,
                    Job.status == "running",
                    Job.lease_token == job.lease_token,
                    Job.lease_until > datetime.now(UTC),
                )
                .values(lease_until=datetime.now(UTC) + timedelta(seconds=lease_seconds))
            )
            return result.rowcount == 1

    @contextmanager
    def keep_alive(self, job: ClaimedJob):
        """Renew long read jobs; every business commit must still fence with owned()."""
        stopped = threading.Event()

        def heartbeat():
            while not stopped.wait(20):
                try:
                    if not self.renew(job):
                        return
                except Exception:
                    logging.warning("job lease renewal stopped; job=%s", job.id)
                    return

        thread = threading.Thread(target=heartbeat, name="pack-v2-job-lease", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)

    @staticmethod
    def finish_in(
        tx: Session,
        job: ClaimedJob,
        *,
        retry: bool = False,
        failed: bool = False,
        retry_at: datetime | None = None,
    ) -> str:
        saved = Queue.owned(tx, job)
        now = datetime.now(UTC)
        if retry and saved.attempts < saved.max_attempts:
            saved.status = "pending"
            saved.available_at = (
                max(now, retry_at) if retry_at else now + timedelta(seconds=min(300, 2**saved.attempts))
            )
        else:
            saved.status = "failed" if retry or failed else "succeeded"
            saved.finished_at = now
        saved.error = "Задание не выполнено; проверьте состояние загрузки" if retry or failed else None
        saved.lease_until, saved.lease_token = None, None
        return saved.status

    def finish(self, job: ClaimedJob, *, retry: bool = False, failed: bool = False) -> bool:
        try:
            with self.db.transaction() as tx:
                self.finish_in(tx, job, retry=retry, failed=failed)
            return True
        except LeaseLost:
            return False


class LeaseLost(Conflict):
    pass
