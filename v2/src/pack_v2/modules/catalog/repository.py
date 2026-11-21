from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import exists, func, or_, select, tuple_, update
from sqlalchemy.orm import Session

from pack_v2.integrations.public import OfferSnapshot
from pack_v2.shared.errors import Conflict, DomainError

from .contracts import CatalogPage, OfferMatch, OfferView
from .models import Offer, OfferCode, OfferRef


def apply_page(tx: Session, account_id: UUID, run_id: UUID, snapshots: tuple[OfferSnapshot, ...]) -> None:
    ids = [s.source_id for s in snapshots]
    if len(set(ids)) != len(ids):
        raise DomainError("Повтор карточки в одной странице каталога")
    existing = dict(
        tx.execute(
            select(OfferRef.value, OfferRef.offer_id).where(
                OfferRef.account_id == account_id, OfferRef.kind == "product_id", OfferRef.value.in_(ids)
            )
        ).all()
    )
    offers = {o.id: o for o in tx.scalars(select(Offer).where(Offer.id.in_(existing.values())))}
    saved_refs, saved_codes = {}, {}
    for row in tx.scalars(select(OfferRef).where(OfferRef.offer_id.in_(offers))):
        saved_refs.setdefault(row.offer_id, []).append(row)
    for row in tx.scalars(select(OfferCode).where(OfferCode.offer_id.in_(offers))):
        saved_codes.setdefault(row.offer_id, []).append(row)
    new_refs, new_codes = [], []
    for snapshot in snapshots:
        if snapshot.source.account_id != account_id or not any(
            r.kind == "product_id" and r.value == snapshot.source_id and r.authoritative
            for r in snapshot.references
        ):
            raise DomainError("Источник карточки не соответствует загрузке")
        saved = offers.get(existing.get(snapshot.source_id))
        fresh = saved is None
        if fresh:
            saved = Offer(
                id=uuid4(),
                account_id=account_id,
                name=snapshot.name or "Товар Ozon " + snapshot.source_id,
                revision=0,
            )
            tx.add(saved)
        changed = fresh or not saved.source_present
        for field in ("name", "article", "image_url", "archived"):
            if field in snapshot.fields_present:
                value = getattr(snapshot, field)
                if field == "name" and not value:
                    continue
                if getattr(saved, field) != value:
                    setattr(saved, field, value)
                    changed = True
        # SKU identities remain aliases. A renamed offer_id is replaced only when observed.
        refs = saved_refs.get(saved.id, [])
        incoming = {(r.kind, r.value) for r in snapshot.references}
        if "article" in snapshot.fields_present:
            incoming = {(k, v) for k, v in incoming if k != "offer_id"}
            if snapshot.article:
                incoming.add(("offer_id", snapshot.article))
            for row in list(refs):
                if row.kind == "offer_id" and (row.kind, row.value) not in incoming:
                    tx.delete(row)
                    refs.remove(row)
                    changed = True
        known = {(r.kind, r.value) for r in refs}
        for kind, value in incoming - known:
            new_refs.append(OfferRef(account_id=account_id, offer_id=saved.id, kind=kind, value=value))
            changed = True
        if "barcodes" in snapshot.fields_present:
            codes = saved_codes.get(saved.id, [])
            incoming_codes = {(c.kind, c.value, c.origin) for c in snapshot.barcodes}
            if any(origin != "ozon" for _, _, origin in incoming_codes):
                raise DomainError("Источник штрихкода не соответствует площадке")
            manual = {(c.kind, c.value) for c in codes if c.origin != "ozon"}
            incoming_codes = {
                (kind, value, origin) for kind, value, origin in incoming_codes if (kind, value) not in manual
            }
            current = {(c.kind, c.value, c.origin) for c in codes if c.origin == "ozon"}
            if current != incoming_codes:
                for row in codes:
                    if row.origin == "ozon" and (row.kind, row.value, row.origin) not in incoming_codes:
                        tx.delete(row)
                for kind, value, origin in incoming_codes - current:
                    new_codes.append(OfferCode(offer_id=saved.id, kind=kind, value=value, origin=origin))
                changed = True
        saved.source_present = True
        saved.observed_at, saved.synced_at = snapshot.observed_at, datetime.now(UTC)
        saved.last_seen_run_id = run_id
        if changed:
            saved.revision += 1
    # Flush removals before insertions, including offer_id swaps between cards.
    tx.flush()
    if new_refs:
        candidates = {(r.kind, r.value) for r in new_refs}
        conflicts = tx.execute(
            select(OfferRef.kind, OfferRef.value).where(
                OfferRef.account_id == account_id, tuple_(OfferRef.kind, OfferRef.value).in_(candidates)
            )
        ).first()
        if conflicts:
            raise Conflict("Один идентификатор Ozon указан у разных карточек. Загрузка остановлена")
    tx.add_all([*new_refs, *new_codes])
    tx.flush()


def complete(tx: Session, account_id: UUID, run_id: UUID) -> None:
    tx.execute(
        update(Offer)
        .where(
            Offer.account_id == account_id,
            Offer.observed_at.is_not(None),
            Offer.source_present.is_(True),
            or_(Offer.last_seen_run_id.is_(None), Offer.last_seen_run_id != run_id),
        )
        .values(source_present=False, revision=Offer.revision + 1)
    )


def listing(tx: Session, *, account_id: UUID | None, query: str, state: str, page: int) -> CatalogPage:
    conditions = []
    if account_id:
        conditions.append(Offer.account_id == account_id)
    if state == "active":
        conditions += [Offer.archived.is_(False), Offer.source_present.is_(True)]
    elif state == "archived":
        conditions.append(Offer.archived.is_(True))
    elif state == "missing":
        conditions.append(Offer.source_present.is_(False))
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(
            or_(
                Offer.name.ilike("%" + escaped + "%", escape="\\"),
                Offer.article.ilike("%" + escaped + "%", escape="\\"),
                exists(
                    select(OfferRef.offer_id).where(OfferRef.offer_id == Offer.id, OfferRef.value == query)
                ),
                exists(
                    select(OfferCode.offer_id).where(OfferCode.offer_id == Offer.id, OfferCode.value == query)
                ),
            )
        )
    total = tx.scalar(select(func.count()).select_from(Offer).where(*conditions))
    rows = tx.scalars(
        select(Offer)
        .where(*conditions)
        .order_by(Offer.created_at.desc(), Offer.id.desc())
        .offset((page - 1) * 30)
        .limit(30)
    ).all()
    return CatalogPage(views(tx, rows), total, page, max(1, (total + 29) // 30))


def views(tx: Session, rows) -> tuple[OfferView, ...]:
    refs, codes = {}, {}
    ids = [r.id for r in rows]
    for r in tx.scalars(
        select(OfferRef).where(OfferRef.offer_id.in_(ids)).order_by(OfferRef.kind, OfferRef.value)
    ):
        refs.setdefault(r.offer_id, []).append((r.kind, r.value))
    for r in tx.scalars(select(OfferCode).where(OfferCode.offer_id.in_(ids)).order_by(OfferCode.value)):
        codes.setdefault(r.offer_id, []).append(r.value)
    return tuple(
        OfferView(
            r.id,
            r.account_id,
            r.name,
            r.article,
            r.image_url,
            r.archived,
            r.source_present,
            r.revision,
            r.observed_at,
            tuple(refs.get(r.id, ())),
            tuple(dict.fromkeys(codes.get(r.id, ()))),
        )
        for r in rows
    )


def matching(tx: Session, account_id: UUID, groups) -> dict:
    wanted = {(r.kind, r.value) for refs in groups.values() for r in refs}
    index = {
        (r.kind, r.value): r.offer_id
        for r in tx.scalars(
            select(OfferRef).where(
                OfferRef.account_id == account_id, tuple_(OfferRef.kind, OfferRef.value).in_(wanted)
            )
        )
    }
    result = {}
    for key, refs in groups.items():
        primary = [(r.kind, r.value) for r in refs if r.kind in {"product_id", "sku"}]
        selected = primary or [(r.kind, r.value) for r in refs if r.kind == "offer_id"]
        found = {index[ref] for ref in selected if ref in index}
        result[key] = OfferMatch(
            next(iter(found)) if len(found) == 1 else None,
            "matched" if len(found) == 1 else "conflict" if found else "unmatched",
        )
    return result
