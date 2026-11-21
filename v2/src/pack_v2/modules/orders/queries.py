from sqlalchemy import exists, func, or_, select

from pack_v2.shared.errors import NotFound

from .contracts import ItemView, ShipmentDetail, ShipmentLink, ShipmentPage, ShipmentView
from .models import OrderRef, Shipment, ShipmentItem, ShipmentRef, ShipmentRelation


def views(tx, rows, catalog, *, history=False):
    ids = [s.id for s in rows]
    refs, orders, relations, items = {}, {}, {}, {}
    for r in tx.scalars(
        select(ShipmentRef)
        .where(ShipmentRef.shipment_id.in_(ids))
        .order_by(ShipmentRef.kind, ShipmentRef.value)
    ):
        refs.setdefault(r.shipment_id, []).append((r.kind, r.value))
    for r in tx.scalars(select(OrderRef).where(OrderRef.order_id.in_([s.order_id for s in rows]))):
        orders.setdefault(r.order_id, {})[r.kind] = r.value
    for r in tx.scalars(
        select(ShipmentRelation)
        .where(ShipmentRelation.shipment_id.in_(ids))
        .order_by(ShipmentRelation.related_number)
    ):
        relations.setdefault(r.shipment_id, []).append(r.related_number)
    query = select(ShipmentItem).where(ShipmentItem.shipment_id.in_(ids))
    if not history:
        query = query.where(ShipmentItem.source_present.is_(True))
    lines = tx.scalars(query.order_by(ShipmentItem.source_line_id)).all()
    cards = catalog.cards(tx, None, {line.offer_id for line in lines if line.offer_id})
    for line in lines:
        card = cards.get(line.offer_id)
        items.setdefault(line.shipment_id, []).append(
            ItemView(
                line.id,
                line.source_line_id,
                line.name,
                line.article,
                line.quantity,
                format(line.price, "f") if line.price is not None else None,
                line.currency,
                tuple((r["kind"], r["value"]) for r in line.references),
                line.offer_id,
                line.match_state,
                card.image_url if card else None,
                card.codes if card else (),
                tuple(line.required_fields),
                tuple(line.optional_fields),
                line.source_present,
            )
        )
    result = []
    for s in rows:
        identity = orders.get(s.order_id, {})
        numbers = refs.get(s.id, [])
        number = next((value for kind, value in numbers if kind == "posting_number"), str(s.id))
        result.append(
            ShipmentView(
                s.id,
                s.order_id,
                s.account_id,
                number,
                identity.get("order_number") or identity.get("order_id"),
                tuple(numbers),
                s.state,
                s.source_status,
                s.source_substatus,
                s.needs_review,
                s.transition_failed,
                s.deadline,
                s.processing_started_at,
                s.warehouse_name,
                s.observed_at,
                s.checked_at,
                s.parent_id,
                s.parent_source_number,
                tuple(relations.get(s.id, ())),
                s.package_count_hint,
                s.requirements_complete,
                tuple(items.get(s.id, ())),
            )
        )
    return tuple(result)


def listing(tx, catalog, *, account_id, query, state, page):
    conditions = []
    if account_id:
        conditions.append(Shipment.account_id == account_id)
    if state == "review":
        conditions.append(Shipment.needs_review.is_(True))
    elif state != "all":
        conditions += [Shipment.state == state, Shipment.needs_review.is_(False)]
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        matched = catalog.code_offers(tx, query, account_id)
        conditions.append(
            or_(
                exists(
                    select(ShipmentRef.shipment_id).where(
                        ShipmentRef.shipment_id == Shipment.id, ShipmentRef.value == query
                    )
                ),
                exists(
                    select(OrderRef.order_id).where(
                        OrderRef.order_id == Shipment.order_id, OrderRef.value == query
                    )
                ),
                exists(
                    select(ShipmentItem.shipment_id).where(
                        ShipmentItem.shipment_id == Shipment.id,
                        ShipmentItem.source_present.is_(True),
                        or_(
                            ShipmentItem.name.ilike("%" + escaped + "%", escape="\\"),
                            ShipmentItem.article.ilike("%" + escaped + "%", escape="\\"),
                            ShipmentItem.references.contains([{"value": query}]),
                            ShipmentItem.offer_id.in_(matched),
                        ),
                    )
                ),
            )
        )
    total = tx.scalar(select(func.count()).select_from(Shipment).where(*conditions))
    order = [
        Shipment.processing_started_at.desc().nulls_last(),
        Shipment.created_at.desc(),
        Shipment.id.desc(),
    ]
    if state in ("awaiting_assembly", "awaiting_dispatch"):
        order.insert(0, Shipment.deadline.asc().nulls_last())
    rows = tx.scalars(
        select(Shipment).where(*conditions).order_by(*order).offset((page - 1) * 30).limit(30)
    ).all()
    return ShipmentPage(views(tx, rows, catalog), total, page, max(1, (total + 29) // 30))


def detail(tx, catalog, shipment_id):
    saved = tx.get(Shipment, shipment_id)
    if saved is None:
        raise NotFound("Отправление не найдено")
    item = views(tx, [saved], catalog, history=True)[0]
    siblings = tx.execute(
        select(Shipment.id, ShipmentRef.value, Shipment.state, Shipment.needs_review)
        .join(ShipmentRef, ShipmentRef.shipment_id == Shipment.id)
        .where(
            Shipment.order_id == saved.order_id,
            Shipment.id != saved.id,
            ShipmentRef.kind == "posting_number",
            ShipmentRef.authoritative.is_(True),
        )
        .order_by(ShipmentRef.value)
    ).all()
    return ShipmentDetail(item, tuple(ShipmentLink(*r) for r in siblings))
