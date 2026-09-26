"""Выгрузка наклеек на компьютер — одним архивом, до начала сборки.

Наклейка это стикер Ozon, этикетка Avito или ярлык Маркета. Панель их не
хранит: файл уезжает в браузер и живёт там, на диске сервера не остаётся
ничего. В базе только отметка `label_saved_at` — по ней видно, выгружена ли
наклейка, и по ней же запирается сборка.

Зачем запирать. Площадка отдаёт наклейку, пока заказ ждёт отгрузки; после
отгрузки её уже не взять. Не выгруженная вовремя пропадает навсегда — поэтому
выгрузка идёт первой, до сканирования, а не «когда-нибудь».

Выгрузка общая на все кабинеты: сборка объединена, склад один, и собирать
заказы трёх магазинов, скачав наклейки одного, — верный способ остаться без
наклейки посреди смены. Поэтому и замок общий: пока не выгружено всё, поля
сканирования нет.

Отгруженное при этом замок не держит: такой заказ уходит из рабочего статуса,
взять его наклейку всё равно нельзя, и останавливать из-за него склад
бессмысленно.
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
    """Имя файла или папки внутри архива: номер и название, без лишнего.

    Пробел оставляем: папка называется кабинетом, и «Кабинет Avito» должен
    читаться как «Кабинет Avito», а не «КабинетAvito».
    """
    keep = [c for c in str(name) if c.isalnum() or c in "-_. "]
    return ("".join(keep).strip() or "label")[:80]


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


def collect(keys: list[str], fetch, *, prefix: str, folder: str = "") -> tuple[dict[str, bytes], list[str]]:
    """Забрать наклейки у площадки. Возвращает (файлы для архива, выгруженные ключи).

    `fetch` получает пачку номеров и отдаёт PDF. Пачка, на которой площадка
    отказала, пропускается: остальные наклейки важнее — из-за одного отказа
    сборщик остался бы вообще без архива. Что не вышло, останется без отметки
    и попадёт в следующую выгрузку.
    """
    saved: list[str] = []
    files: dict[str, bytes] = {}
    at = f"{_safe(folder)}/" if folder else ""
    for start in range(0, len(keys), BATCH):
        batch = keys[start : start + BATCH]
        try:
            pdf = fetch(batch)
        except Exception as exc:  # noqa: BLE001 - причина уходит в журнал
            log.warning("Наклейки (%s): площадка отказала на пачке из %d — %s",
                        prefix, len(batch), exc)
            continue
        if not pdf:
            continue
        pages = _split(pdf, batch)
        if pages:
            for key, page in pages.items():
                files[f"{at}{_safe(key)}.pdf"] = page
        else:
            first, last = _safe(batch[0]), _safe(batch[-1])
            files[f"{at}{prefix}-{first}-{last}.pdf"] = pdf
        saved.extend(batch)
    return files, saved


def collect_files(keys: list[str], fetch, *, prefix: str, folder: str = "") -> tuple[dict[str, bytes], list[str]]:
    """То же, что collect, для площадки, которая отдаёт наклейки по заказу.

    `fetch` получает пачку номеров и отдаёт {номер: PDF} по тем, что взять
    удалось. Файл — на заказ, со всеми его коробками: резать нечего, и заказ
    в несколько мест не превращается в безымянную пачку. Не отданный заказ
    остаётся без отметки и попадёт в следующую выгрузку.
    """
    saved: list[str] = []
    files: dict[str, bytes] = {}
    at = f"{_safe(folder)}/" if folder else ""
    for start in range(0, len(keys), BATCH):
        batch = keys[start : start + BATCH]
        try:
            got = fetch(batch) or {}
        except Exception as exc:  # noqa: BLE001 - причина уходит в журнал
            log.warning("Наклейки (%s): площадка отказала на пачке из %d — %s", prefix, len(batch), exc)
            continue
        for key in batch:
            if got.get(key):
                files[f"{at}{_safe(key)}.pdf"] = got[key]
                saved.append(key)
        if len(got) < len(batch):
            log.warning("Наклейки (%s): площадка не отдала %d из %d", prefix, len(batch) - len(got), len(batch))
    return files, saved


def zip_files(files: dict[str, bytes]) -> bytes:
    """Сложить готовые файлы в один архив."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, blob in files.items():
            archive.writestr(name, blob)
    return buffer.getvalue()


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


# ------------------------------------------------------------------ все кабинеты
def pending_everywhere(shops: list[dict]) -> list[dict]:
    """Что ждёт выгрузки по каждому кабинету. Пустые кабинеты в список не идут.

    Кабинет без наклеек (площадка их не отдаёт) и кабинет, у которого всё
    выгружено, выглядят одинаково: работы нет — строки нет.
    """
    from ..markets import registry

    out = []
    for account in shops:
        market = registry.get(account["marketplace"])
        if market is None or market.labels is None:
            continue
        keys = market.labels.pending(account["id"])
        if keys:
            out.append({"account": account, "market": market, "keys": keys})
    return out


def state_everywhere(waiting: list[dict]) -> dict:
    """Что показать на «Сборке»: сколько ждёт выгрузки и по каким магазинам.

    Разбивка по кабинетам нужна на экране: «9 заказов» без неё не отвечает на
    вопрос «чьих», а качать человек идёт за всеми сразу.
    """
    return {
        "pending": sum(len(item["keys"]) for item in waiting),
        "locked": bool(waiting),
        "shops": [
            {
                "title": item["account"]["title"],
                "market": item["market"].code,
                "word": item["market"].labels.word,
                "count": len(item["keys"]),
            }
            for item in waiting
        ],
    }


def archive_everywhere(waiting: list[dict], user: dict | None = None) -> tuple[bytes, int, list[str]]:
    """Один архив на все кабинеты: папка на магазин, отметки по своим таблицам.

    Возвращает (архив, сколько наклеек выгружено, отказавшие магазины). Отказ
    одной площадки не отменяет выгрузку остальных: сборщик получит, что есть, а
    невыгруженное останется в замке и попадёт в следующую попытку.
    """
    files: dict[str, bytes] = {}
    total = 0
    refused: list[str] = []
    # Папка на кабинет: в архиве трёх магазинов иначе не разобраться. Когда
    # магазин один, папки нет — лишний клик там не объясняет ничего.
    by_folders = len(waiting) > 1
    for item in waiting:
        account, market = item["account"], item["market"]
        keys = item["keys"][:MAX_AT_ONCE]
        source = market.labels
        folder = account["title"] if by_folders else ""
        if source.files:
            part, saved = collect_files(
                keys,
                lambda batch, account=account, source=source: source.files(account, user or {}, batch),
                prefix=source.word, folder=folder,
            )
        else:
            part, saved = collect(
                keys,
                lambda batch, account=account, source=source: source.pdf(account, user or {}, batch),
                prefix=source.word, folder=folder,
            )
        if not saved:
            refused.append(account["title"])
            continue
        files.update(part)
        mark_saved(source.table, account["id"], saved, source.key)
        total += len(saved)
        db.log_event(
            "labels_archive", account_id=account["id"], user=user,
            message=f"Выгружены {source.word}: {len(saved)} шт.",
        )
    return zip_files(files), total, refused
