from uuid import UUID

from sqlalchemy import select

from pack_v2.infrastructure.database import Database
from pack_v2.infrastructure.jobs import ClaimedJob
from pack_v2.integrations.public import AccountSync, Connections, ReadError, SyncView
from pack_v2.modules.identity.public import Principal
from pack_v2.shared.errors import DomainError

from . import repository as store
from .contracts import CatalogPage
from .models import CatalogSync, Offer, OfferCode, ProductCode, ProductComponent


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

    @staticmethod
    def packing_cards(tx, ids):
        """Current scan codes from the offer and explicitly linked warehouse product."""
        rows = list(tx.scalars(select(Offer).where(Offer.id.in_(ids))))
        cards = {r.id: r for r in store.views(tx, rows)}
        product_ids = {r.product_id for r in rows if r.product_id}
        codes = {}
        gtins = {}
        own_gtins = {}
        for code in tx.scalars(
            select(OfferCode).where(OfferCode.offer_id.in_(ids), OfferCode.kind == "gtin")
        ):
            own_gtins.setdefault(code.offer_id, set()).add(code.value)
        for code in tx.scalars(select(ProductCode).where(ProductCode.product_id.in_(product_ids))):
            codes.setdefault(code.product_id, set()).add(code.value)
            if code.kind == "gtin":
                gtins.setdefault(code.product_id, set()).add(code.value)
        siblings = {
            r.id: r.product_id for r in tx.scalars(select(Offer).where(Offer.product_id.in_(product_ids)))
        }
        for code in tx.scalars(select(OfferCode).where(OfferCode.offer_id.in_(siblings))):
            codes.setdefault(siblings[code.offer_id], set()).add(code.value)
            if code.kind == "gtin":
                gtins.setdefault(siblings[code.offer_id], set()).add(code.value)
        components = set(
            tx.scalars(
                select(ProductComponent.product_id).where(ProductComponent.product_id.in_(product_ids))
            )
        )
        return {
            r.id: {
                "card": cards[r.id],
                "codes": tuple(sorted(set(cards[r.id].codes) | codes.get(r.product_id, set()))),
                "has_components": r.product_id in components,
                "gtins": tuple(sorted(own_gtins.get(r.id, set()) | gtins.get(r.product_id, set()))),
            }
            for r in rows
        }

    @staticmethod
    def packing_code_offers(tx, code, account_id=None):
        direct = Catalog.code_offers(tx, code, account_id)
        product_ids = (
            select(ProductCode.product_id)
            .where(ProductCode.value == code)
            .union(
                select(Offer.product_id)
                .join(OfferCode, OfferCode.offer_id == Offer.id)
                .where(OfferCode.value == code, Offer.product_id.is_not(None))
            )
        )
        query = select(Offer.id).where(Offer.product_id.in_(product_ids))
        if account_id:
            query = query.where(Offer.account_id == account_id)
        return direct | set(tx.scalars(query))

    @staticmethod
    def print_codes(tx, offer_ids, prefix):
        """Prefer this offer's marketplace code; warehouse fallback excludes OZN/MRKT."""
        offers = list(tx.scalars(select(Offer).where(Offer.id.in_(offer_ids))))
        own, fallback = {}, {}
        products = {o.product_id for o in offers if o.product_id}

        def printable(value):
            return bool(value) and len(value) <= 200 and all(32 <= ord(c) <= 126 for c in value)

        for code in tx.scalars(
            select(OfferCode).where(OfferCode.offer_id.in_(offer_ids)).order_by(OfferCode.value)
        ):
            if printable(code.value):
                own.setdefault(code.offer_id, []).append(code.value)
        for code in tx.scalars(
            select(ProductCode).where(ProductCode.product_id.in_(products)).order_by(ProductCode.value)
        ):
            if printable(code.value) and not code.value.upper().startswith(("OZN", "MRKT")):
                fallback.setdefault(code.product_id, []).append(code.value)
        for code, product_id in tx.execute(
            select(OfferCode, Offer.product_id)
            .join(Offer, Offer.id == OfferCode.offer_id)
            .where(Offer.product_id.in_(products))
            .order_by(OfferCode.value)
        ):
            if printable(code.value) and not code.value.upper().startswith(("OZN", "MRKT")):
                fallback.setdefault(product_id, []).append(code.value)
        result = {}
        for offer in offers:
            codes = own.get(offer.id, [])
            selected = next((c for c in codes if c.upper().startswith(prefix)), None)
            result[offer.id] = selected or next(iter(codes or fallback.get(offer.product_id, [])), None)
        return result

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
