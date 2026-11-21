import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from pack_v2.infrastructure.database import Database
from pack_v2.infrastructure.jobs import ClaimedJob, LeaseLost, Queue, enqueue
from pack_v2.infrastructure.models import AuditEvent, Job
from pack_v2.integrations.public import AccountSource, Connections, ReadError, SourceChanged
from pack_v2.modules.identity.public import Principal
from pack_v2.shared.errors import DomainError, Forbidden

from . import repository as store
from .contracts import CatalogPage, SyncView
from .models import CatalogSync

ACTIVE = ("queued", "running", "retrying")


class Catalog:
    def __init__(self, db: Database, connections: Connections):
        self.db, self.connections = db, connections

    def list_offers(
        self,
        actor: Principal,
        *,
        account_id: UUID | None = None,
        query: str = "",
        state: str = "all",
        page: int = 1,
    ) -> CatalogPage:
        actor.require("products")
        if (
            state not in {"all", "active", "archived", "missing"}
            or not 1 <= page <= 10000
            or len(query) > 512
        ):
            raise DomainError("Проверьте фильтры каталога")
        with self.db.transaction() as tx:
            return store.listing(tx, account_id=account_id, query=query.strip(), state=state, page=page)

    def sync_status(self, actor: Principal) -> list[SyncView]:
        if not actor.can("products"):
            actor.require("settings")
        accounts = {a.id: a for a in self.connections.account_labels(actor)}
        now = datetime.now(UTC)
        with self.db.transaction() as tx:
            latest = tx.scalars(
                select(CatalogSync)
                .distinct(CatalogSync.account_id)
                .order_by(CatalogSync.account_id, CatalogSync.created_at.desc(), CatalogSync.id.desc())
            ).all()
            successes = dict(
                tx.execute(
                    select(CatalogSync.account_id, CatalogSync.finished_at)
                    .where(CatalogSync.status == "succeeded")
                    .distinct(CatalogSync.account_id)
                    .order_by(CatalogSync.account_id, CatalogSync.finished_at.desc())
                ).all()
            )
            jobs = {j.id: j for j in tx.scalars(select(Job).where(Job.id.in_([r.job_id for r in latest])))}
            result = []
            for run in latest:
                status, error = run.status, run.error
                account = accounts.get(run.account_id)
                job = jobs.get(run.job_id)
                if not account or run.credential_revision != account.credential_revision:
                    status, error = "stale", "Ключи изменились. Обновите каталог"
                elif not account.active and status in ACTIVE:
                    status, error = "stale", "Кабинет отключён"
                elif status in ACTIVE and (
                    job.status in {"failed", "succeeded"}
                    or job.status == "running"
                    and job.lease_until <= now
                    and job.attempts >= job.max_attempts
                ):
                    status, error = "failed", "Загрузка прервана. Запустите её повторно"
                result.append(
                    SyncView(
                        run.id,
                        run.account_id,
                        status,
                        run.pages,
                        run.items,
                        error,
                        run.created_at,
                        run.finished_at,
                        successes.get(run.account_id),
                        run.retry_at,
                    )
                )
            return result

    def request_sync(self, actor: Principal, account_id: UUID | None = None) -> list[UUID]:
        if actor.role not in {"owner", "admin"}:
            raise Forbidden("Обновление каталога доступно администратору и владельцу")
        sources = [account_id] if account_id else [s.account_id for s in self.connections.sync_accounts()]
        result = []
        for current in sources:
            try:
                result.append(self._enqueue(current, actor.id))
            except DomainError:
                if account_id:
                    raise
        return result

    def schedule(self, minutes: int) -> None:
        if minutes <= 0:
            return
        for source in self.connections.sync_accounts():
            try:
                self._enqueue(source.account_id, None, interval=timedelta(minutes=minutes))
            except DomainError:
                continue

    def _enqueue(self, account_id: UUID, actor_id: UUID | None, *, interval: timedelta | None = None) -> UUID:
        now = datetime.now(UTC)
        with self.db.transaction() as tx:
            source = self.connections.source_for_sync(tx, account_id)
            running = tx.scalar(
                select(CatalogSync)
                .where(CatalogSync.account_id == account_id, CatalogSync.status.in_(ACTIVE))
                .with_for_update()
            )
            if running:
                job = tx.get(Job, running.job_id)
                if running.credential_revision != source.credential_revision:
                    running.status, running.finished_at = "stale", now
                    running.error = "Ключи кабинета изменились"
                elif job.status in {"failed", "succeeded"} or (
                    job.status == "running" and job.lease_until <= now and job.attempts >= job.max_attempts
                ):
                    running.status, running.finished_at = "failed", now
                    running.error = "Загрузка прервана. Запустите её повторно"
                else:
                    return running.id
                tx.flush()
            last = tx.scalar(
                select(CatalogSync)
                .where(CatalogSync.account_id == account_id)
                .order_by(CatalogSync.created_at.desc(), CatalogSync.id.desc())
                .limit(1)
            )
            if last and last.credential_revision == source.credential_revision:
                if last.retry_at and last.retry_at > now:
                    raise DomainError("Ozon попросил повторить загрузку позже")
                if interval and last.created_at + interval > now:
                    return last.id
            run_id = uuid4()
            job_id = enqueue(tx, "catalog.sync", "catalog.sync:" + str(run_id), {"run_id": str(run_id)})
            tx.add(
                CatalogSync(
                    id=run_id,
                    account_id=account_id,
                    credential_revision=source.credential_revision,
                    job_id=job_id,
                    requested_by=actor_id,
                )
            )
            tx.add(
                AuditEvent(
                    actor_id=actor_id,
                    kind="catalog_sync_requested",
                    subject_id=run_id,
                    details={"account_id": str(account_id)},
                )
            )
            return run_id

    def _locked(self, tx, job: ClaimedJob) -> tuple[CatalogSync, AccountSource]:
        run_id = UUID(job.payload["run_id"])
        run = tx.get(CatalogSync, run_id)
        if run is None or run.job_id != job.id:
            raise DomainError("Задание загрузки не найдено")
        source = AccountSource(run.account_id, "ozon", run.credential_revision)
        self.connections.lock_source(tx, source)
        Queue.owned(tx, job)
        run = tx.scalar(
            select(CatalogSync)
            .where(CatalogSync.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run.status not in ACTIVE:
            raise SourceChanged("Загрузка уже завершена или устарела")
        return run, source

    def perform(self, job: ClaimedJob) -> None:
        """Called only by the worker. All commits fence the account, job lease and run."""
        try:
            with self.db.transaction() as tx:
                run, source = self._locked(tx, job)
                run.status, run.started_at, run.finished_at = "running", datetime.now(UTC), None
                run.pages, run.items, run.error, run.retry_at = 0, 0, None, None
            final = False
            with self.connections.reader_for_sync(source) as reader:
                for page in reader.iter_catalog():
                    if (
                        final
                        or page.source != source
                        or page.dataset != "catalog"
                        or any(s.source != source for s in page.items)
                    ):
                        raise ReadError("invalid_response")
                    with self.db.transaction() as tx:
                        run, _ = self._locked(tx, job)
                        store.apply_page(tx, run.account_id, run.id, page.items)
                        run.pages += 1
                        run.items += len(page.items)
                    final = page.is_last
            if not final:
                raise ReadError("incomplete")
            with self.db.transaction() as tx:
                run, _ = self._locked(tx, job)
                store.complete(tx, run.account_id, run.id)
                run.status, run.finished_at, run.error = "succeeded", datetime.now(UTC), None
                Queue.finish_in(tx, job)
                tx.add(
                    AuditEvent(
                        kind="catalog_sync_succeeded",
                        subject_id=run.id,
                        details={"account_id": str(run.account_id), "items": run.items},
                    )
                )
        except LeaseLost:
            return
        except ReadError as error:
            self._failed(job, str(error), retry=error.retryable, delay=error.retry_after)
        except SourceChanged as error:
            self._failed(job, str(error), stale=True)
        except DomainError as error:
            self._failed(job, str(error))
        except IntegrityError:
            self._failed(job, "Конфликт идентификаторов карточек. Загрузка остановлена")
        except SQLAlchemyError:
            self._failed(job, "База данных временно недоступна", retry=True)
        except Exception as error:
            logging.error("catalog.sync failed; job=%s; type=%s", job.id, type(error).__name__)
            self._failed(job, "Загрузка прервана. Повторите её или проверьте журнал исполнителя")

    def _failed(
        self,
        job: ClaimedJob,
        message: str,
        *,
        retry: bool = False,
        stale: bool = False,
        delay: float | None = None,
    ) -> None:
        now = datetime.now(UTC)
        retry_at = None
        if retry:
            seconds = max(min(300, 2**job.attempts), delay or 0)
            ceiling = datetime.max.replace(tzinfo=UTC) - timedelta(days=1)
            retry_at = (
                ceiling if seconds >= (ceiling - now).total_seconds() else now + timedelta(seconds=seconds)
            )
        try:
            with self.db.transaction() as tx:
                Queue.owned(tx, job)
                run = tx.scalar(select(CatalogSync).where(CatalogSync.job_id == job.id).with_for_update())
                if not run or run.status not in ACTIVE:
                    Queue.finish_in(tx, job, failed=True)
                    return
                status = Queue.finish_in(tx, job, retry=retry, failed=not retry, retry_at=retry_at)
                run.status = "stale" if stale else "retrying" if status == "pending" else "failed"
                run.finished_at = None if status == "pending" else now
                run.error, run.retry_at = message, retry_at
        except LeaseLost:
            return


__all__ = ["Catalog", "CatalogPage", "SyncView"]
