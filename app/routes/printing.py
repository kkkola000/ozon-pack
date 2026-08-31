"""API для локального агента печати.

Агент работает в складской сети, куда у сервера нет доступа: он сам приходит за
заданиями и сам отчитывается о результате. Поэтому авторизация здесь не по
сессии, а по отдельному ключу агента.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import Response

from .. import db, options, printing

router = APIRouter(prefix="/api/print")


def _require_token(token: str | None) -> None:
    if not printing.check_token(token):
        raise HTTPException(status_code=401, detail="Неверный ключ агента печати")


@router.get("/next")
def next_job(token: str = Query(default="")):
    """Следующее задание — сырыми байтами для принтера."""
    _require_token(token)
    job = printing.next_job()
    if not job:
        return Response(status_code=204)
    return Response(
        content=job["payload"],
        media_type="application/octet-stream",
        headers={
            "X-Job-Id": str(job["id"]),
            "X-Job-Kind": job["kind"],
            "Cache-Control": "no-store",
        },
    )


@router.post("/ack")
def ack(payload: dict = Body(...)):
    """Агент сообщает, напечаталось задание или нет."""
    _require_token(str(payload.get("token") or ""))
    try:
        job_id = int(payload.get("job_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Не указан номер задания") from None
    return printing.ack(job_id, bool(payload.get("ok")), str(payload.get("error") or "") or None)


@router.get("/config")
def config(token: str = Query(default="")):
    """Куда печатать. Адрес принтера задаётся в панели, а не на складском ПК."""
    _require_token(token)
    values = options.get_printer_config()
    return {
        "printer": {"host": values["host"], "port": values["port"]},
        "enabled": values["enabled"],
        "poll_interval": 2,
    }


@router.get("/ping")
def ping(token: str = Query(default="")):
    """Агент отмечается, что он жив, — видно в настройках."""
    _require_token(token)
    db.kv_set("print_agent_seen", db.now_iso())
    return {"status": "ok", "queued": printing.status()["queued"]}
