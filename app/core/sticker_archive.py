"""Оригинальные стикеры по заказам: фоновая загрузка и шесть месяцев версий.

PDF лежат в SQLite вместе с метаданными: транзакция не оставляет ссылки на
несохранённый файл, резервная копия базы включает архив. Архив не меняет
label_saved_at и отметки печати рабочего места.
"""
from __future__ import annotations

import calendar
import hashlib
import io
import logging
import threading
from datetime import datetime, timedelta, timezone

from . import accounts, db
from .config import settings

log = logging.getLogger(__name__)
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_ZIP_BYTES = 100 * 1024 * 1024
PAGE_SIZE = 30
# За один проход берём ограниченную очередь каждого кабинета. Сначала новые
# заказы, затем ошибки и проверки изменённых стикеров уже сохранённых заказов.
PER_PASS = 50
RETRY_SECONDS = 300
RECHECK_SECONDS = 6 * 3600


def cutoff(now: datetime | None = None) -> str:
    """Шесть календарных месяцев назад, с ограничением последнего дня месяца."""
    now = now or datetime.now(timezone.utc)
    month = now.year * 12 + now.month - 1 - 6
    year, month0 = divmod(month, 12)
    day = min(now.day, calendar.monthrange(year, month0 + 1)[1])
    return now.replace(year=year, month=month0 + 1, day=day).replace(microsecond=0).isoformat()


def _source(account: dict):
    from ..markets import registry

    market = registry.get(account['marketplace'])
    return market if market and market.labels and market.orders else None


def _ready(account: dict) -> list[dict]:
    market = _source(account)
    if not market:
        return []
    source = market.labels
    return [dict(row) for row in db.query(
        f"SELECT o.{source.key} AS order_id, {market.orders.number_sql} AS order_number, o.deliver_since "
        f"FROM {source.table} o WHERE o.account_id = ? AND ({market.orders.status_sql}) = 'deliver'",
        (account['id'],),
    )]


def enqueue(account: dict, *, wake: bool = True) -> None:
    """Синхронизация только добавляет очередь; обращения к API — в своём потоке."""
    ready = _ready(account)
    now = db.now_iso()
    with db.write() as conn:
        # Кабинет могли удалить между чтением заказов и получением блокировки.
        if not conn.execute('SELECT 1 FROM accounts WHERE id = ?', (account['id'],)).fetchone():
            return
        for row in ready:
            conn.execute(
                "INSERT INTO sticker_orders(account_id, marketplace, shop_title, order_id, order_number, "
                "deliver_since, created_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id, order_id) DO UPDATE SET "
                "shop_title = excluded.shop_title, order_number = excluded.order_number, "
                "checked_at = CASE WHEN sticker_orders.deliver_since IS NOT excluded.deliver_since "
                "THEN NULL ELSE sticker_orders.checked_at END, deliver_since = excluded.deliver_since",
                (account['id'], account['marketplace'], account['title'], str(row['order_id']),
                 str(row['order_number'] or row['order_id']), row['deliver_since'], now),
            )
    if wake and _worker:
        _worker.wake.set()


def save(archive_id: int, pdf: bytes) -> int:
    """Проверить полный PDF и сохранить только новую версию его содержимого."""
    if not pdf or not pdf.startswith(b'%PDF') or len(pdf) > MAX_PDF_BYTES:
        raise ValueError('Площадка не отдала допустимый PDF (не больше 20 МБ)')
    from pypdf import PdfReader

    try:
        if not PdfReader(io.BytesIO(pdf)).pages:
            raise ValueError('Пустой PDF')
    except Exception as exc:
        raise ValueError('Площадка отдала повреждённый PDF') from exc
    digest = hashlib.sha256(pdf).hexdigest()
    now = db.now_iso()
    with db.write() as conn:
        order = conn.execute('SELECT account_id FROM sticker_orders WHERE id = ?', (archive_id,)).fetchone()
        if not order:
            raise ValueError('Заказ архива не найден')
        latest = conn.execute('SELECT id, digest FROM sticker_versions WHERE archive_id = ? ORDER BY id DESC LIMIT 1',
                              (archive_id,)).fetchone()
        if latest and latest['digest'] == digest:
            version_id = latest['id']
        else:
            version_id = conn.execute(
                'INSERT INTO sticker_versions(archive_id, account_id, saved_at, digest, pdf) VALUES(?,?,?,?,?)',
                (archive_id, order['account_id'], now, digest, pdf),
            ).lastrowid
        conn.execute('UPDATE sticker_orders SET checked_at = ?, error = NULL WHERE id = ?', (now, archive_id))
        return version_id



def capture(account: dict, archive_id: int, order_id: str) -> None:
    """Один заказ за запрос: многостраничный PDF нельзя делить по предположению."""
    market = _source(account)
    if not market:
        raise ValueError('Площадка не поддерживает стикеры')
    source = market.labels
    if source.files:
        pdf = (source.files(account, {}, [order_id]) or {}).get(order_id)
    else:
        got = source.pdf(account, {}, [order_id])
        pdf, missing = got if isinstance(got, tuple) else (got, [])
        if order_id in missing:
            pdf = None
    save(archive_id, pdf)


def _capture_pending(account: dict) -> None:
    enqueue(account, wake=False)
    ready = {str(row['order_id']) for row in _ready(account)}
    rows = db.query(
        'SELECT o.*, EXISTS(SELECT 1 FROM sticker_versions v WHERE v.archive_id = o.id) AS saved '
        'FROM sticker_orders o WHERE account_id = ? '
        'ORDER BY (checked_at IS NOT NULL), saved, checked_at, id', (account['id'],),
    )
    now = datetime.now(timezone.utc)
    processed = 0
    for row in rows:
        if _worker and _worker.stopping.is_set():
            return
        if row['order_id'] not in ready:
            continue
        age = RETRY_SECONDS if row['error'] else RECHECK_SECONDS
        if row['checked_at'] and datetime.fromisoformat(row['checked_at']) > now - timedelta(seconds=age):
            continue
        try:
            capture(account, row['id'], row['order_id'])
        except Exception as exc:  # noqa: BLE001 - следующий заказ важнее отказа одного
            # Внешний ответ API может содержать секреты: в UI безопасная причина.
            log.warning('Архив стикеров: кабинет %s, заказ %s, %s', account['id'], row['order_id'], type(exc).__name__)
            db.execute('UPDATE sticker_orders SET checked_at = ?, error = ? WHERE id = ?',
                       (db.now_iso(), 'Не удалось сохранить стикер. Загрузка будет повторена.', row['id']))
        processed += 1
        if processed >= PER_PASS:
            break


def run_once() -> None:
    """Обойти кабинеты независимо: отказ одного не останавливает остальные."""
    purge()
    for account in accounts.all_accounts(active_only=True):
        if not accounts.is_configured(account):
            continue
        try:
            _capture_pending(account)
        except Exception:
            log.exception('Не удалось обработать архив кабинета %s', account['id'])


def remember(account: dict, files: dict[str, bytes]) -> None:
    """Сохранить и файлы обычной выгрузки: заказ могут сразу собрать и отгрузить.

    Пачку с неизвестным соответствием страниц заказам сюда передавать нельзя.
    Ошибка архива не отменяет печать; фоновая очередь повторяет сохранение.
    """
    if not files:
        return
    try:
        enqueue(account)
        for key, pdf in files.items():
            row = db.query_one('SELECT id FROM sticker_orders WHERE account_id = ? AND order_id = ?',
                               (account['id'], str(key)))
            if row:
                save(row['id'], pdf)
    except Exception:
        log.exception('Не удалось сохранить выгруженные стикеры кабинета %s', account['id'])


def remember_batch(account: dict, keys: list[str], pdf: bytes | None) -> None:
    if not pdf or not keys:
        return
    market = _source(account)
    if len(keys) > 1 and market and market.labels.files:
        # Источник с отдельными PDF сохраняет их перед объединением для печати.
        # Не делим такой файл по страницам и не запрашиваем те же ярлыки снова.
        return
    from .labels import _split

    files = {keys[0]: pdf} if len(keys) == 1 else _split(pdf, keys)
    if files:
        remember(account, files)
    else:
        # Например, в пачке Ozon несколько мест. По числу страниц нельзя
        # угадать границы заказов: запрашиваем каждый полный комплект отдельно.
        # Это также сохраняет файл до того, как сборщик сразу закроет заказ.
        try:
            enqueue(account)
            for key in keys:
                row = db.query_one('SELECT id FROM sticker_orders WHERE account_id = ? AND order_id = ?',
                                   (account['id'], str(key)))
                if row:
                    try:
                        capture(account, row['id'], str(key))
                    except Exception:
                        log.exception('Не удалось сохранить многостраничный стикер заказа %s', key)
        except Exception:
            log.exception('Не удалось сохранить пачку стикеров кабинета %s', account['id'])


def purge() -> None:
    before = cutoff()
    with db.write() as conn:
        conn.execute('DELETE FROM sticker_versions WHERE saved_at < ?', (before,))
        conn.execute('DELETE FROM sticker_orders WHERE created_at < ? '
                     'AND NOT EXISTS(SELECT 1 FROM sticker_versions v WHERE v.archive_id = sticker_orders.id)', (before,))


def _filters(account_id: int | None, market: str, q: str, since: str, until: str, state: str):
    conditions = ['(v.id IS NOT NULL OR o.created_at >= ?)']
    params: list = [cutoff()]
    if account_id:
        conditions.append('o.account_id = ?')
        params.append(account_id)
    if market:
        conditions.append('o.marketplace = ?')
        params.append(market)
    if q:
        conditions.append('(instr(lower(o.order_id), lower(?)) > 0 OR instr(lower(o.order_number), lower(?)) > 0)')
        params += [q, q]
    if since:
        conditions.append('date(COALESCE(v.saved_at, o.created_at), ?) >= ?')
        params += [f'{settings.timezone_offset:+d} hours', since]
    if until:
        conditions.append('date(COALESCE(v.saved_at, o.created_at), ?) <= ?')
        params += [f'{settings.timezone_offset:+d} hours', until]
    if state == 'error':
        conditions.append('o.error IS NOT NULL')
    elif state == 'pending':
        conditions.append('v.id IS NULL AND o.error IS NULL')
    elif state == 'saved':
        conditions.append('v.id IS NOT NULL')
    return ' AND '.join(conditions), params


def search(*, account_id: int | None = None, market: str = '', q: str = '', since: str = '',
           until: str = '', state: str = '', page: int = 1) -> tuple[list[dict], int]:
    where, params = _filters(account_id, market, q, since, until, state)
    join = (' FROM sticker_orders o LEFT JOIN sticker_versions v ON v.id = '
            '(SELECT MAX(id) FROM sticker_versions WHERE archive_id = o.id AND saved_at >= ?) ')
    params = [cutoff()] + params
    total = db.query_one('SELECT COUNT(*) AS n' + join + 'WHERE ' + where, params)['n']
    rows = db.query(
        'SELECT o.*, v.id AS version_id, v.saved_at, '
        '(SELECT COUNT(*) FROM sticker_versions WHERE archive_id = o.id AND saved_at >= ?) AS versions'
        + join + 'WHERE ' + where + ' ORDER BY COALESCE(v.saved_at, o.created_at) DESC, o.id DESC LIMIT ? OFFSET ?',
        [cutoff()] + params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    results = [dict(row) for row in rows]
    for row in results:
        row['history'] = [dict(v) for v in db.query(
            'SELECT id, saved_at FROM sticker_versions WHERE archive_id = ? AND saved_at >= ? ORDER BY id DESC',
            (row['id'], cutoff()),
        )] if row['versions'] > 1 else []
    return results, total


def version(version_id: int) -> dict | None:
    row = db.query_one(
        'SELECT v.*, o.order_id, o.order_number, o.marketplace, o.shop_title FROM sticker_versions v '
        'JOIN sticker_orders o ON o.id = v.archive_id WHERE v.id = ? AND v.saved_at >= ?', (version_id, cutoff()),
    )
    return dict(row) if row else None


def retry(archive_id: int) -> None:
    row = db.query_one('SELECT * FROM sticker_orders WHERE id = ?', (archive_id,))
    account = accounts.get(row['account_id']) if row else None
    if not account or not account['active'] or not accounts.is_configured(account):
        raise ValueError('Кабинет недоступен или его ключи не настроены')
    if row['order_id'] not in {str(item['order_id']) for item in _ready(account)}:
        raise ValueError('Заказ больше не ожидает отгрузки. Сохранённые файлы остаются в архиве.')
    db.execute('UPDATE sticker_orders SET checked_at = NULL WHERE id = ?', (archive_id,))
    if _worker:
        _worker.wake.set()


class ArchiveWorker(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name='sticker-archive')
        self.stopping = threading.Event()
        self.wake = threading.Event()

    def run(self):  # pragma: no cover - механика проверяется через run_once
        while not self.stopping.is_set():
            self.wake.clear()
            try:
                run_once()
            except Exception:
                log.exception('Не удалось обработать архив стикеров')
            self.wake.wait(60)
        conn = getattr(db._local, 'conn', None)
        if conn:
            conn.close()
            db._local.conn = None


_worker: ArchiveWorker | None = None


def start_worker() -> None:
    global _worker
    if settings.sync_enabled and (_worker is None or not _worker.is_alive()):
        _worker = ArchiveWorker()
        _worker.start()


def stop_worker() -> None:
    if _worker:
        _worker.stopping.set()
        _worker.wake.set()
