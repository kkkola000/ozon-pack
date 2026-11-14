"""Read marking declaration from marketplace product cards, without guessing IDs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db


def declarations(account: dict, skus: list[str], api) -> set[str]:
    known, missing = set(), []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    for sku in skus:
        row = db.query_one('SELECT marking_required,marking_checked_at FROM products WHERE account_id=? AND sku=?',
                           (account['id'], sku))
        if row and row['marking_required']:
            known.add(sku)
        if not row or not row['marking_checked_at'] or row['marking_checked_at'] < cutoff:
            missing.append(sku)
    if not missing:
        return known
    values = {}
    if account['marketplace'] == 'yandex':
        rows, _ = api.offer_mappings(offer_ids=missing)
        for mapping in rows:
            offer = mapping.get('offer') or {}
            if isinstance(offer.get('markingDeclared'), bool):
                values[str(offer.get('offerId'))] = offer['markingDeclared']
    else:
        values = _ozon_values(api, missing)
    with db.write() as conn:
        for sku in missing:
            declared = values.get(sku)
            conn.execute('INSERT INTO products(account_id,sku,marking_required,marking_checked_at) VALUES(?,?,?,?) '
                         'ON CONFLICT(account_id,sku) DO UPDATE SET marking_required=COALESCE(excluded.marking_required,products.marking_required),'
                         'marking_checked_at=excluded.marking_checked_at',
                         (account['id'], sku, int(declared) if declared is not None else None, db.now_iso()))
    return (known - {s for s, v in values.items() if v is False}) | {s for s, v in values.items() if v is True and s in missing}


def _ozon_values(api, missing):
    values = {}
    definitions = {}
    for card in api.product_attributes(missing):
        sku = str(card.get('sku') or '')
        category, type_id = card.get('description_category_id'), card.get('type_id')
        if not category or not type_id:
            continue
        pair = (category, type_id)
        if pair not in definitions:
            definitions[pair] = api.category_attributes(category, type_id)
        ids = {str(a['id']) for a in definitions[pair]
               if str(a.get('name') or '').strip().casefold() == 'нужна маркировка'}
        for attr in card.get('attributes') or []:
            if str(attr.get('id')) not in ids:
                continue
            raw = {str(v.get('value') or '').strip().casefold() for v in attr.get('values') or []}
            if raw & {'да', 'true', '1'}:
                values[sku] = True
            elif raw & {'нет', 'false', '0'}:
                values[sku] = False
    return values
