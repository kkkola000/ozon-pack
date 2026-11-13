"""Скачивание оригинального стикера открытого заказа без отметки о печати.

Архив и его очередь остаются в sticker_archive. При наличии готового файла
читаем его оттуда; иначе берём файл у площадки тем же источником наклеек.
"""
from __future__ import annotations

import io
from urllib.parse import quote

from . import board, db, labels, sticker_archive


class NotReady(ValueError):
    """Площадка ещё не подтвердила готовность заказа к отгрузке."""


def filename(account: dict, number: str) -> str:
    # Номер кабинета предотвращает совпадение имён у двух магазинов площадки.
    safe = labels._safe(number).encode('ascii', 'replace').decode().replace('?', '_')
    return f"{account['marketplace']}-{account['id']}-{safe}.pdf"


def descriptor(account: dict, number: str) -> dict:
    return {'url': f"/api/pack/sticker/{account['id']}/{quote(number, safe='')}.pdf",
            'filename': filename(account, number), 'ready': not bool(board.not_ready(account, [number]))}


def load(account: dict, number: str, user: dict) -> bytes:
    from ..markets import registry

    market = registry.get(account['marketplace'])
    source = market.labels if market else None
    if not source or not db.query_one(
        f'SELECT 1 FROM {source.table} WHERE account_id = ? AND {source.key} = ?', (account['id'], number),
    ):
        raise LookupError('Заказ не найден в кабинете')
    if board.not_ready(account, [number]):
        raise NotReady('Стикер будет скачан после передачи ЧЗ и подтверждения готовности заказа площадкой')
    saved = db.query_one(
        'SELECT v.pdf FROM sticker_versions v JOIN sticker_orders o ON o.id = v.archive_id '
        'WHERE o.account_id = ? AND o.order_id = ? ORDER BY v.id DESC LIMIT 1', (account['id'], number),
    )
    pdf = bytes(saved['pdf']) if saved else None
    if pdf is None:
        if source.files:
            pdf = (source.files(account, user, [number]) or {}).get(number)
        else:
            got = source.pdf(account, user, [number])
            pdf, missing = got if isinstance(got, tuple) else (got, [])
            if number in missing:
                pdf = None
        _validate(pdf)
        # Прежний путь сохранения: один PDF на заказ, без новых версий.
        sticker_archive.remember_batch(account, [number], pdf)
    labels.mark_saved(source.table, account['id'], [number], source.key)
    db.log_event('sticker_download', account_id=account['id'], user=user, posting_number=number,
                 message='Стикер отправлен на компьютер сборщика')
    return pdf


def _validate(pdf: bytes | None) -> None:
    from pypdf import PdfReader

    if not pdf or not pdf.startswith(b'%PDF') or len(pdf) > sticker_archive.MAX_PDF_BYTES:
        raise ValueError('Нет допустимого PDF стикера')
    if not PdfReader(io.BytesIO(pdf)).pages:
        raise ValueError('Пустой стикер')
