"""Выгрузка стикеров на компьютер — архивом, до начала сборки.

Панель сама стикеры не хранит: файл уезжает в браузер и живёт там, на диске
сервера не остаётся ничего. В базе только отметка `label_saved_at` — по ней
видно, выгружен ли стикер, и по ней же запирается сборка.

Зачем запирать. Ozon и Avito отдают стикер, пока отправление ждёт отгрузки;
после отгрузки его уже не взять. Стикер, не выгруженный вовремя, пропадает
навсегда — поэтому выгрузка идёт первой, до сканирования, а не «когда-нибудь».

Отгруженное при этом замок не держит: такой заказ уходит из рабочего статуса,
взять его стикер всё равно нельзя, и останавливать из-за него склад бессмысленно.
"""
from __future__ import annotations

import io
import logging
import zipfile

from . import db

log = logging.getLogger(__name__)

# Столько номеров уходит в один запрос к площадке. У обеих по 50 за раз.
BATCH = 50
# Потолок на одну выгрузку. Без него утреннее нажатие уносит в площадку всю
# накопленную очередь разом и выбирает лимиты кабинета на всех сразу.
MAX_AT_ONCE = 500


def _safe(name: str) -> str:
    """Имя файла внутри архива: номер отправления и ничего лишнего."""
    keep = [c for c in str(name) if c.isalnum() or c in "-_."]
    return ("".join(keep) or "label")[:80]


def _split(pdf: bytes, keys: list[str]) -> dict[str, bytes] | None:
    """Разложить пачку по отправлениям, если страниц ровно столько же.

    Площадка отдаёт пачку одним PDF в порядке запрошенных номеров. Когда
    страниц столько же, сколько номеров, каждая — чей-то стикер, и файлы
    раскладываются по номерам: потом такой архив можно открыть и найти нужный.

    Если страниц больше (у отправления два места) — разложить нечем: какая
    страница чья, площадка не говорит. Тогда None, и пачка ляжет в архив одним
    файлом. Лучше один файл с верным содержимым, чем десять с чужими стикерами.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:  # pragma: no cover - pypdf в зависимостях
        return None
    try:
        reader = PdfReader(io.BytesIO(pdf))
        if len(reader.pages) != len(keys):
            return None
        out: dict[str, bytes] = {}
        for key, page in zip(keys, reader.pages):
            writer = PdfWriter()
            writer.add_page(page)
            buffer = io.BytesIO()
            writer.write(buffer)
            out[key] = buffer.getvalue()
        return out
    except Exception:  # noqa: BLE001 - битый PDF не должен ронять выгрузку
        log.warning("Пачку стикеров не удалось разложить по отправлениям", exc_info=True)
        return None


def build_archive(keys: list[str], fetch, *, prefix: str) -> tuple[bytes, list[str]]:
    """Собрать ZIP со стикерами. Возвращает (архив, выгруженные ключи).

    `fetch` получает пачку номеров и отдаёт PDF. Пачка, на которой площадка
    отказала, пропускается: остальные стикеры важнее — из-за одного отказа
    сборщик остался бы вообще без архива. Что не вышло, останется без отметки
    и попадёт в следующую выгрузку.
    """
    saved: list[str] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for start in range(0, len(keys), BATCH):
            batch = keys[start : start + BATCH]
            try:
                pdf = fetch(batch)
            except Exception as exc:  # noqa: BLE001 - причина уходит в журнал
                log.warning("Стикеры (%s): площадка отказала на пачке из %d — %s",
                            prefix, len(batch), exc)
                continue
            if not pdf:
                continue
            pages = _split(pdf, batch)
            if pages:
                for key, page in pages.items():
                    archive.writestr(f"{_safe(key)}.pdf", page)
            else:
                first, last = _safe(batch[0]), _safe(batch[-1])
                archive.writestr(f"{prefix}-{first}-{last}.pdf", pdf)
            saved.extend(batch)
    return buffer.getvalue(), saved


def mark_saved(table: str, account_id: int, keys: list[str], column: str) -> None:
    """Проставить отметку о выгрузке. Файла нет — есть факт, что он у нас."""
    if not keys:
        return
    now = db.now_iso()
    with db.write() as conn:
        for start in range(0, len(keys), 400):
            batch = keys[start : start + 400]
            marks = ",".join("?" for _ in batch)
            conn.execute(
                f"UPDATE {table} SET label_saved_at = ? "
                f"WHERE account_id = ? AND {column} IN ({marks})",
                [now, account_id] + batch,
            )


def state(pending: list[str]) -> dict:
    """Что показать на «Сборке»: сколько ждёт выгрузки и пускать ли к сканеру."""
    return {"pending": len(pending), "locked": bool(pending)}
