"""Архив стикеров: доступ к сохранённым файлам без обращения к площадкам."""
from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from ..core import accounts, db, labels, sticker_archive
from ..core.config import settings
from ..core.deps import check_csrf, pdf_response, require_section, safe_filename, templates
from ..markets import registry

router = APIRouter()


def _date(value: str) -> str:
    if not value:
        return ''
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise HTTPException(400, 'Неверная дата фильтра') from exc


@router.get('/reports/stickers', response_class=HTMLResponse)
def archive_page(request: Request, account_id: str = '', market: str = '', q: str = '',
                 since: str = '', until: str = '', state: str = 'saved', page: int = Query(1, ge=1, le=1000000),
                 user: dict = Depends(require_section('stickers'))):
    try:
        chosen = int(account_id) if account_id else None
    except ValueError as exc:
        raise HTTPException(400, 'Неверный кабинет') from exc
    since, until = _date(since), _date(until)
    if since and until and since > until:
        raise HTTPException(400, 'Начало периода позже окончания')
    items, total = sticker_archive.search(account_id=chosen, market=market, q=q.strip(), since=since,
                                         until=until, state=state, page=page)
    filters = dict(account_id=chosen or '', market=market, q=q, since=since, until=until, state=state)

    def page_url(number):
        return '/reports/stickers?' + urlencode({key: value for key, value in {**filters, 'page': number}.items() if value != ''})

    # Архив содержит снимок названия кабинета: он остаётся понятным и после переименования.
    return templates.TemplateResponse(request, 'stickers.html', {
        'request': request, 'user': user, 'items': items, 'total': total, 'page': page,
        'pages': max(1, (total + sticker_archive.PAGE_SIZE - 1) // sticker_archive.PAGE_SIZE),
        'previous': page_url(page - 1), 'following': page_url(page + 1),
        'accounts': accounts.all_accounts(), 'markets': registry.all_markets(), 'filters': filters,
        'sync_enabled': settings.sync_enabled, 'csrf': request.state.session.get('csrf'), 'active_tab': 'stickers',
    })


@router.get('/api/stickers/{version_id}.pdf')
def archive_pdf(version_id: int, download: bool = False, user: dict = Depends(require_section('stickers'))):
    item = sticker_archive.version(version_id)
    if not item:
        raise HTTPException(404, 'Стикер не найден или срок хранения истёк')
    filename = safe_filename(f"{item['marketplace']}-{item['order_id']}-v{item['id']}.pdf")
    response = pdf_response(item['pdf'], filename)
    if download:
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@router.post('/api/stickers/archive.zip')
def archive_zip(request: Request, ids: list[int] = Body(..., max_length=100),
                user: dict = Depends(require_section('stickers'))):
    check_csrf(request)
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise HTTPException(400, 'Выберите стикеры для скачивания')
    marks = ','.join('?' for _ in ids)
    size = db.query_one(
        f'SELECT COUNT(*) AS n, SUM(length(v.pdf)) AS size FROM sticker_versions v '
        f'JOIN sticker_orders o ON o.id = v.archive_id WHERE v.id IN ({marks}) AND v.saved_at >= ?',
        ids + [sticker_archive.cutoff()],
    )
    if size['n'] != len(ids):
        raise HTTPException(404, 'Один из выбранных стикеров отсутствует или устарел')
    if size['size'] > sticker_archive.MAX_ZIP_BYTES:
        raise HTTPException(400, 'Архив больше 100 МБ. Выберите меньше стикеров.')
    files = {}
    for version_id in ids:
        item = sticker_archive.version(version_id)
        if not item:
            raise HTTPException(404, 'Стикер больше недоступен')
        filename = safe_filename(f"{item['marketplace']}-{item['account_id']}-v{version_id}-{item['order_id']}.pdf")
        files[filename] = item['pdf']
    db.log_event('stickers_download', user=user, message=f'Скачаны стикеры из архива: {len(files)}')
    return Response(labels.zip_files(files), media_type='application/zip', headers={
        'Content-Disposition': 'attachment; filename="stickers.zip"', 'Cache-Control': 'no-store',
    })


@router.post('/api/stickers/{archive_id}/retry')
def archive_retry(archive_id: int, request: Request, user: dict = Depends(require_section('stickers'))):
    check_csrf(request)
    if not settings.sync_enabled:
        raise HTTPException(409, 'Фоновая синхронизация отключена. Включите SYNC_ENABLED для загрузки стикеров.')
    try:
        sticker_archive.retry(archive_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.log_event('stickers_retry', user=user, message=f'Повтор загрузки стикера: {archive_id}')
    return {'status': 'ok', 'message': 'Стикер добавлен в очередь фоновой загрузки'}
