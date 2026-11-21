from datetime import timedelta
from uuid import UUID

from sqlalchemy import select

from pack_v2.infrastructure.jobs import ClaimedJob
from pack_v2.integrations.public import AccountSync, ReadError, ReadPage
from pack_v2.modules.catalog.public import Catalog
from pack_v2.modules.identity.public import Principal
from pack_v2.shared.errors import DomainError

from . import packing, queries
from . import repository as store
from .models import OrderSync

STATES = {
    "all",
    "awaiting_assembly",
    "awaiting_dispatch",
    "in_delivery",
    "delivered",
    "cancelled",
    "unknown",
    "review",
}


class Orders:
    def __init__(self, db, connections, catalog: Catalog, *, lookback_days=30):
        self.db, self.connections, self.catalog = db, connections, catalog
        self.lookback_days = lookback_days
        self.sync = AccountSync(
            db,
            connections,
            OrderSync,
            kind="orders.sync",
            dataset="shipments",
            section="orders",
            complete=self._complete,
            scope=lambda model: model.mode != "point",
            begin=self._begin,
            prepare_run=self._prepare,
            context_values=lambda run: {
                "since": run.since,
                "until": run.until,
                "mode": run.mode,
                "target": run.target_shipment_id,
                "expected_revision": run.expected_shipment_revision,
            },
        )

    def _prepare(self, tx, source, automatic, now):
        last_full = tx.scalar(
            select(OrderSync.finished_at)
            .where(
                OrderSync.account_id == source.account_id,
                OrderSync.credential_revision == source.credential_revision,
                OrderSync.mode == "full",
                OrderSync.status == "succeeded",
            )
            .order_by(OrderSync.finished_at.desc())
            .limit(1)
        )
        mode = "working" if automatic and last_full and last_full > now - timedelta(days=1) else "full"
        return {"since": now - timedelta(days=self.lookback_days), "until": now, "mode": mode}

    def _begin(self, tx, run):
        # A command may have waited for a stopped/busy worker. Read a current window.
        run.until = run.started_at
        run.since = run.until - timedelta(days=self.lookback_days)
        if run.mode == "point":
            saved = packing.lock_shipment(tx, run.target_shipment_id)
            if saved.account_id != run.account_id:
                raise DomainError("Кабинет отправления не совпадает с заданием")
            run.expected_shipment_revision = saved.revision
        if run.mode == "working":
            recent = tx.scalar(
                select(OrderSync.id)
                .where(
                    OrderSync.account_id == run.account_id,
                    OrderSync.credential_revision == run.credential_revision,
                    OrderSync.mode == "full",
                    OrderSync.status == "succeeded",
                    OrderSync.finished_at > run.until - timedelta(days=1),
                )
                .limit(1)
            )
            if recent is None:
                run.mode = "full"

    @staticmethod
    def _complete(tx, run):
        if run.mode == "point":
            target = packing.lock_shipment(tx, run.target_shipment_id)
            store.complete(tx, run, order_id=target.order_id)
        else:
            store.complete(tx, run)

    def packing_manifest(self, tx, shipment_id):
        return packing.manifest(tx, shipment_id, self.catalog)

    @staticmethod
    def packing_lock(tx, shipment_id):
        packing.lock_shipment(tx, shipment_id)

    @staticmethod
    def packing_plan(tx, manifest):
        return packing.plan_units(tx, manifest)

    def packing_candidates(self, tx, **kwargs):
        return packing.candidates(tx, self.catalog, **kwargs)

    @staticmethod
    def packing_refresh(tx, source, shipment_id, actor_id=None):
        return packing.queue_refresh(tx, source, shipment_id, actor_id)

    @staticmethod
    def packing_refresh_status(tx, run_id, account_id, shipment_id):
        return packing.refresh_status(tx, run_id, account_id, shipment_id)

    def list_shipments(self, actor: Principal, *, account_id=None, query="", state="all", page=1):
        actor.require("orders")
        if state not in STATES or not 1 <= page <= 10000 or len(query) > 512:
            raise DomainError("Проверьте фильтры заказов")
        with self.db.transaction() as tx:
            return queries.listing(
                tx, self.catalog, account_id=account_id, query=query.strip(), state=state, page=page
            )

    def shipment(self, actor: Principal, shipment_id: UUID):
        actor.require("orders")
        with self.db.transaction() as tx:
            return queries.detail(tx, self.catalog, shipment_id)

    def sync_status(self, actor):
        return self.sync.sync_status(actor)

    def request_sync(self, actor, account_id=None):
        return self.sync.request_sync(actor, account_id)

    def schedule(self, minutes):
        self.sync.schedule(minutes)

    def perform(self, job: ClaimedJob):
        self.sync.perform(job, self._read)

    def _read(self, context, reader):
        final = False
        config = context.values
        if config["mode"] == "point":
            target = context.write(lambda tx, run: self.packing_manifest(tx, run.target_shipment_id))
            snapshot = reader.get_shipment(target.posting_number)

            def apply(tx, run):
                saved = packing.lock_shipment(tx, run.target_shipment_id)
                if saved.revision != config["expected_revision"]:
                    raise ReadError("incomplete")
                if snapshot is None:
                    store.missing(tx, run, saved.id)
                else:
                    if snapshot.source != context.source or snapshot.source_id != target.posting_number:
                        raise ReadError("invalid_response")
                    store.apply_page(tx, run, (snapshot,), self.catalog)
                    run.items = 1
                run.pages = 1

            context.write(apply)
            return
        states = (
            None if config["mode"] == "full" else ("awaiting_assembly", "awaiting_dispatch", "in_delivery")
        )
        for page in reader.iter_shipments(config["since"], config["until"], states=states):
            if final:
                raise ReadError("invalid_response")
            context.page(page, lambda tx, run, items: store.apply_page(tx, run, items, self.catalog))
            final = page.is_last
        if not final:
            raise ReadError("incomplete")
        after = None
        while True:
            pending = context.write(
                lambda tx, run, after=after: store.candidates(tx, run.account_id, run.id, after)
            )
            if not pending:
                break
            for shipment_id, number in pending:
                snapshot = reader.get_shipment(number)
                if snapshot is None:
                    context.write(
                        lambda tx, run, shipment_id=shipment_id: store.missing(tx, run, shipment_id)
                    )
                else:
                    if snapshot.source_id != number:
                        raise ReadError("invalid_response")
                    context.page(
                        ReadPage(context.source, "shipments", (snapshot,), None, True),
                        lambda tx, run, items: store.apply_page(tx, run, items, self.catalog),
                    )
            after = pending[-1][0]
        after = None
        while True:
            after = context.write(
                lambda tx, run, after=after: store.relink_batch(tx, run, self.catalog, after)
            )
            if after is None:
                break


__all__ = ["Orders"]
