"""Очередь печати для локального агента.

VPS не может достучаться до принтера в складской сети (адрес 192.168.x.x не
маршрутизируется из интернета). Поэтому панель складывает готовые задания в
очередь, а маленький агент на складе забирает их и отдаёт принтеру по TCP.
"""
from __future__ import annotations

import hmac
import logging
from typing import Any

from . import db, options, packing, pdfrender
from .tspl import PrinterConfig, build_label_job, build_test_job

log = logging.getLogger("printing")

MAX_ATTEMPTS = 3
JOB_KEEP_DAYS = 7


def printer_config() -> PrinterConfig:
    values = options.get_printer_config()
    return PrinterConfig(
        host=values["host"],
        port=values["port"],
        dpi=values["dpi"],
        gap_mm=values["gap_mm"],
        gap_offset_mm=values["gap_offset_mm"],
        direction=values["direction"],
        copies=values["copies"],
        invert=values["invert"],
        threshold=values["threshold"],
    )


def is_enabled() -> bool:
    values = options.get_printer_config()
    return bool(values["enabled"] and values["host"])


def check_token(token: str | None) -> bool:
    expected = options.get_agent_token(create=False)
    if not expected or not token:
        return False
    # Сравниваем байты: на строке с кириллицей compare_digest бросает
    # исключение, и вместо отказа в доступе получилась бы ошибка сервера.
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


# ------------------------------------------------------------------ постановка в очередь
def _enqueue(payload: bytes, *, kind: str, title: str, posting_number: str | None, user: dict | None) -> int:
    cursor = db.execute(
        "INSERT INTO print_jobs(kind, posting_number, title, payload, status, created_by, created_at)"
        " VALUES(?,?,?,?,'queued',?,?)",
        (kind, posting_number, title, payload, (user or {}).get("login"), db.now_iso()),
    )
    job_id = cursor.lastrowid
    db.log_event(
        "print_queued",
        user=user,
        posting_number=posting_number,
        message=f"Задание #{job_id} в очереди принтера ({len(payload)} байт)",
    )
    return job_id


def enqueue_label(user: dict, posting_numbers: list[str]) -> dict:
    """Стикер -> картинка -> команды TSPL -> очередь агента."""
    if not pdfrender.is_available():
        raise RuntimeError("На сервере нет библиотеки рендера PDF (pypdfium2)")

    pdf, _filename = packing.label_pdf(user, posting_numbers)
    pages = pdfrender.render_pdf(pdf, dpi=options.get_printer_config()["dpi"])
    if not pages:
        raise RuntimeError("Не удалось преобразовать стикер в картинку")

    config = printer_config()
    payload = build_label_job(
        [(page.gray, page.width, page.height) for page in pages],
        config,
        width_mm=pages[0].width_mm,
        height_mm=pages[0].height_mm,
    )
    title = ", ".join(posting_numbers)
    job_id = _enqueue(
        payload,
        kind="label",
        title=title,
        posting_number=posting_numbers[0] if len(posting_numbers) == 1 else None,
        user=user,
    )
    return {"job_id": job_id, "pages": len(pages), "bytes": len(payload)}


def enqueue_test(user: dict) -> dict:
    payload = build_test_job(printer_config())
    job_id = _enqueue(payload, kind="test", title="Тестовая этикетка", posting_number=None, user=user)
    return {"job_id": job_id, "bytes": len(payload)}


# ------------------------------------------------------------------ работа агента
def next_job() -> dict | None:
    """Взять следующее задание. Агент опрашивает панель, входящие порты не нужны."""
    with db.write() as conn:
        row = conn.execute(
            "SELECT id, kind, title, payload, attempts FROM print_jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE print_jobs SET status = 'sent', taken_at = ?, attempts = attempts + 1 WHERE id = ?",
            (db.now_iso(), row["id"]),
        )
    return {"id": row["id"], "kind": row["kind"], "title": row["title"], "payload": bytes(row["payload"])}


def ack(job_id: int, ok: bool, error: str | None = None) -> dict:
    row = db.query_one("SELECT * FROM print_jobs WHERE id = ?", (job_id,))
    if not row:
        return {"status": "error", "message": f"Задание #{job_id} не найдено"}

    if ok:
        db.execute("UPDATE print_jobs SET status = 'done', done_at = ?, error = NULL WHERE id = ?", (db.now_iso(), job_id))
        db.log_event("print_done", posting_number=row["posting_number"], message=f"Задание #{job_id} напечатано")
        return {"status": "ok"}

    # Не получилось — вернём в очередь, пока не исчерпаны попытки
    status = "queued" if row["attempts"] < MAX_ATTEMPTS else "failed"
    db.execute("UPDATE print_jobs SET status = ?, error = ? WHERE id = ?", (status, (error or "")[:500], job_id))
    db.log_event(
        "print_failed",
        level="error" if status == "failed" else "warn",
        posting_number=row["posting_number"],
        message=f"Задание #{job_id}: {error or 'ошибка печати'}",
    )
    return {"status": "ok", "job_status": status}


def cleanup(days: int = JOB_KEEP_DAYS) -> int:
    cursor = db.execute(
        "DELETE FROM print_jobs WHERE status IN ('done','failed') AND created_at < datetime('now', ?)",
        (f"-{days} days",),
    )
    return cursor.rowcount or 0


def status() -> dict[str, Any]:
    counts = {
        row["status"]: row["c"]
        for row in db.query("SELECT status, COUNT(*) AS c FROM print_jobs GROUP BY status")
    }
    last = db.query_one("SELECT * FROM print_jobs ORDER BY id DESC LIMIT 1")
    last_done = db.query_one("SELECT done_at FROM print_jobs WHERE status = 'done' ORDER BY done_at DESC LIMIT 1")
    return {
        "enabled": is_enabled(),
        "queued": counts.get("queued", 0),
        "sent": counts.get("sent", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "last_title": last["title"] if last else None,
        "last_status": last["status"] if last else None,
        "last_error": last["error"] if last else None,
        "last_done_at": last_done["done_at"] if last_done else None,
        "has_token": bool(options.get_agent_token(create=False)),
    }
