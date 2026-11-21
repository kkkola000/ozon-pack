from uuid import UUID

from sqlalchemy import select

from pack_v2.infrastructure.database import Database
from pack_v2.infrastructure.jobs import ClaimedJob
from pack_v2.integrations.public import AccountSync, Connections, ReadError, SyncView
from pack_v2.modules.identity.public import Principal
from pack_v2.shared.errors import DomainError

from . import repository as store
from .contracts import CatalogPage
from .models import CatalogSync, Offer, OfferCode


class Catalog:
    def __init__(self, db: Database, connections: Connections):
        self.db, self.connections = db, connections
        self.sync = AccountSync(
            db,
            connections,
            CatalogSync,
            kind="catalog.sync",
            dataset="catalog",
            section="products",
            complete=lambda tx, run: store.complete(tx, run.account_id, run.id),
        )

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

    @staticmethod
    def match_offers(tx, account_id, groups):
        """Backend port for other modules; immutable marketplace identities take precedence."""
        return store.matching(tx, account_id, groups)

    @staticmethod
    def cards(tx, account_id, ids):
        query = select(Offer).where(Offer.id.in_(ids))
        if account_id:
            query = query.where(Offer.account_id == account_id)
        rows = tx.scalars(query).all()
        return {r.id: r for r in store.views(tx, rows)}

    @staticmethod
    def code_offers(tx, code, account_id=None):
        query = (
            select(OfferCode.offer_id)
            .join(Offer, Offer.id == OfferCode.offer_id)
            .where(OfferCode.value == code)
        )
        if account_id:
            query = query.where(Offer.account_id == account_id)
        return set(tx.scalars(query))

    def sync_status(self, actor: Principal) -> list[SyncView]:
        return self.sync.sync_status(actor)

    def request_sync(self, actor: Principal, account_id: UUID | None = None) -> list[UUID]:
        return self.sync.request_sync(actor, account_id)

    def schedule(self, minutes: int) -> None:
        self.sync.schedule(minutes)

    def perform(self, job: ClaimedJob) -> None:
        self.sync.perform(job, self._read)

    @staticmethod
    def _read(context, reader):
        final = False
        for page in reader.iter_catalog():
            if final:
                raise ReadError("invalid_response")
            context.page(page, lambda tx, run, items: store.apply_page(tx, run.account_id, run.id, items))
            final = page.is_last
        if not final:
            raise ReadError("incomplete")


__all__ = ["Catalog", "CatalogPage", "SyncView"]
