"""«Настройки → Настройка принтеров» и служебные ручки для QZ Tray.

Выбор принтеров меняет только владелец. Сертификат и подпись нужны каждому,
кто печатает: браузер сборщика подписывает ими запросы к QZ Tray на своём
компьютере. Как это устроено и почему так — в core/printers.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..core import db
from ..core import printers as core_printers
from ..core.deps import check_csrf, current_user, require_owner

router = APIRouter()


@router.post("/api/printers")
def api_save(request: Request, payload: dict = Body(...), user: dict = Depends(require_owner)):
    """Сохранить принтер для каждого размера листа. Пусто — «через браузер»."""
    check_csrf(request)
    try:
        chosen = core_printers.save(payload.get("printers"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    summary = ", ".join(f"{code}: {name or 'браузер'}" for code, name in chosen.items())
    db.log_event("printers_saved", user=user, message=summary)
    return {"status": "ok", "printers": chosen}


@router.get("/api/printers/qz/certificate", response_class=PlainTextResponse)
def api_certificate(download: int = 0, user: dict = Depends(current_user)):  # noqa: ARG001 - только для вошедших
    """Сертификат панели для QZ Tray. С download=1 — файлом, чтобы поставить его в QZ Tray.

    Файл сразу называется override.crt — под этим именем QZ Tray его и ищет в
    своей папке, переименовывать ничего не нужно.
    """
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="override.crt"'
    return PlainTextResponse(core_printers.certificate(), headers=headers)


@router.post("/api/printers/qz/sign", response_class=PlainTextResponse)
async def api_sign(request: Request, user: dict = Depends(current_user)):  # noqa: ARG001 - только для вошедших
    """Подписать запрос QZ Tray. Тело — хеш запроса, ответ — подпись в base64."""
    check_csrf(request)
    body = (await request.body())[:256].decode("ascii", errors="replace")
    try:
        signature = core_printers.sign(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PlainTextResponse(signature, headers={"Cache-Control": "no-store"})
