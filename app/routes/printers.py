"""«Принтеры»: куда и на какой лист печатается каждый документ, и QZ Tray.

Страница доступна всем, кто работает в панели, включая сборщика, — и только
она: остальные «Настройки» по-прежнему по ролям. Сборщик у стола сам знает,
куда воткнут какой принтер, и поправить это должен уметь без владельца. Кто
что поменял, пишется в журнал.

Сертификат и подпись нужны каждому, кто печатает: браузер подписывает ими
запросы к QZ Tray на своём компьютере. Как это устроено — в core/printers.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..core import db
from ..core import printers as core_printers
from ..core.deps import check_csrf, current_user, templates

router = APIRouter()


@router.get("/printers", response_class=HTMLResponse)
def printers_page(request: Request, user: dict = Depends(current_user)):
    """Страница настройки принтеров — для всех вошедших."""
    return templates.TemplateResponse(
        request,
        "printers.html",
        {
            "request": request,
            "user": user,
            "documents": core_printers.page(),
            "paper": [(code, title) for code, (title, _w, _h) in core_printers.PAPER.items()],
            "limit": core_printers.ROWS_PER_KIND,
            "csrf": request.state.session.get("csrf"),
            "active_tab": "printers",
        },
    )


@router.post("/api/printers")
def api_save(request: Request, payload: dict = Body(...), user: dict = Depends(current_user)):
    """Сохранить строки «документ — размер листа — принтер». Пусто — «через браузер»."""
    check_csrf(request)
    try:
        saved = core_printers.save(payload.get("rows"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    titles = {doc["kind"]: doc["title"] for doc in core_printers.documents()}
    summary = "; ".join(
        f"{titles.get(row['kind'], row['kind'])}, {core_printers.PAPER[row['size']][0]}: "
        f"{row['printer'] or 'браузер'}"
        for row in saved
    )
    db.log_event("printers_saved", user=user, message=summary)
    return {"status": "ok", "printers": core_printers.setup()}


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
