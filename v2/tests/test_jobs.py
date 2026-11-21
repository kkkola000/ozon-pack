from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from pack_v2.cli import worker
from pack_v2.infrastructure.jobs import Queue, enqueue
from pack_v2.infrastructure.models import Job
from pack_v2.shared.errors import Conflict


def test_job_is_atomic_with_business_transaction(db):
    with pytest.raises(RuntimeError), db.transaction() as tx:
        enqueue(tx, "test", "rolled-back")
        raise RuntimeError("rollback")
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(Job)) == 0


def test_enqueue_is_idempotent_and_conflicting_payload_rejected(db):
    with db.transaction() as tx:
        first = enqueue(tx, "test", "same", {"revision": 1})
        assert enqueue(tx, "test", "same", {"revision": 1}) == first
    with pytest.raises(Conflict), db.transaction() as tx:
        enqueue(tx, "test", "same", {"revision": 2})


def test_concurrent_workers_claim_once(db):
    with db.transaction() as tx:
        enqueue(tx, "test", "one-job")
    queue = Queue(db)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: queue.claim(), range(4)))
    assert len([job for job in claims if job]) == 1


def test_expired_owner_cannot_complete_new_lease(db):
    queue = Queue(db)
    with db.transaction() as tx:
        enqueue(tx, "test", "reclaim")
    first = queue.claim()
    with db.transaction() as tx:
        tx.get(Job, first.id).lease_until = datetime.now(UTC) - timedelta(seconds=1)
    second = queue.claim()
    assert first.lease_token != second.lease_token and second.attempts == 2
    assert not queue.finish(first)
    assert queue.finish(second)
    assert not queue.finish(second)


def test_retry_is_delayed_and_exhaustion_is_terminal(db):
    queue = Queue(db)
    with db.transaction() as tx:
        job_id = enqueue(tx, "test", "retry")
        tx.get(Job, job_id).max_attempts = 2
    first = queue.claim()
    assert queue.finish(first, retry=True)
    assert queue.claim() is None
    with db.transaction() as tx:
        tx.get(Job, job_id).available_at = datetime.now(UTC) - timedelta(seconds=1)
    second = queue.claim()
    assert queue.finish(second, retry=True)
    with db.transaction() as tx:
        assert tx.get(Job, job_id).status == "failed"


def test_last_crashed_attempt_is_not_left_running_forever(db):
    queue = Queue(db)
    with db.transaction() as tx:
        job_id = enqueue(tx, "test", "last-lease")
        tx.get(Job, job_id).max_attempts = 1
    queue.claim()
    with db.transaction() as tx:
        tx.get(Job, job_id).lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assert queue.claim() is None
    with db.transaction() as tx:
        assert tx.get(Job, job_id).status == "failed"


def test_foundation_worker_runs_cleanup_and_rejects_unknown_handler(db, identity):
    with db.transaction() as tx:
        job_id = enqueue(tx, "not.registered", "unknown")
    worker(db, identity, once=True)
    with db.transaction() as tx:
        assert tx.get(Job, job_id).status == "failed"
    worker(db, identity, once=True)
    with db.transaction() as tx:
        assert tx.scalar(select(Job.status).where(Job.kind == "identity.cleanup")) == "succeeded"


def test_lease_renewal_requires_current_unexpired_token(db):
    with db.transaction() as tx:
        enqueue(tx, "test", "renew")
    queue = Queue(db)
    first = queue.claim(lease_seconds=30)
    assert queue.renew(first, lease_seconds=120)
    with db.transaction() as tx:
        saved = tx.get(Job, first.id)
        assert saved.lease_until > datetime.now(UTC) + timedelta(seconds=110)
        saved.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assert not queue.renew(first)
    second = queue.claim()
    assert second.lease_token != first.lease_token
    assert not queue.renew(first) and queue.renew(second)


def test_job_finish_in_same_transaction_rolls_back_with_business_changes(db):
    from pack_v2.modules.catalog.models import Product

    with db.transaction() as tx:
        enqueue(tx, "test", "atomic-finish")
    queue = Queue(db)
    job = queue.claim()
    with pytest.raises(RuntimeError), db.transaction() as tx:
        tx.add(Product(name="must roll back"))
        Queue.finish_in(tx, job)
        raise RuntimeError("rollback")
    with db.transaction() as tx:
        assert tx.get(Job, job.id).status == "running"
        assert tx.scalar(select(func.count()).select_from(Product)) == 0
