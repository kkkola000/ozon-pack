"""«Принтеры»: куда и на какой лист печатается каждый документ, и QZ Tray.

Страница доступна всем, кто работает в панели, включая сборщика, — и только
она: остальные «Настройки» по-прежнему по ролям. Сборщик у стола сам знает,
куда воткнут какой принтер, и поправить это должен уметь без владельца. Кто
что поменял, пишется в журнал.

Сертификат и подпись нужны каждому, кто печатает: браузер подписывает ими
запросы к QZ Tray на своём компьютере. Как это устроено — в core/printers.py.
"""
from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

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
            "custom": core_printers.CUSTOM,
            "limits": {"paper": core_printers.PAPER_MM, "gap": core_printers.GAP_MAX,
                       "shift": core_printers.SHIFT_MAX},
            "orientations": list(core_printers.ORIENTATION.items()),
            "limit": core_printers.ROWS_PER_KIND,
            "csrf": request.state.session.get("csrf"),
            "active_tab": "printers",
        },
    )


@router.post("/api/printers")
def api_save(request: Request, payload: dict = Body(...), user: dict = Depends(current_user)):
    """Сохранить строки «документ — размер листа — принтер — ориентация». Пусто — «через браузер»."""
    check_csrf(request)
    try:
        saved = core_printers.save(payload.get("rows"))
        formats = core_printers.save_formats(payload.get("formats"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    titles = {doc["kind"]: doc["title"] for doc in core_printers.documents()}
    summary = "; ".join(
        f"{titles.get(row['kind'], row['kind'])}, {core_printers.paper_title(row)}: "
        f"{row['printer'] or 'браузер'}"
        + (f", {core_printers.ORIENTATION[row['orientation']].lower()}" if row["orientation"] else "")
        + (f", подгонка: {core_printers.fit_title(row)}" if core_printers.fit_title(row) else "")
        for row in saved
    )
    if formats:
        summary += "; наклейки: " + ", ".join(f"{titles.get(kind, kind)} — {code}" for kind, code in formats.items())
    db.log_event("printers_saved", user=user, message=summary)
    return {"status": "ok", "printers": core_printers.setup()}


# Пачка стикеров на смену — десятки страниц; больше этого через подгонку не гоним.
FIT_LIMIT = 40 * 1024 * 1024


@router.post("/api/printers/fit")
def api_fit(request: Request, payload: dict = Body(...), user: dict = Depends(current_user)):  # noqa: ARG001
    """Подгонка печати для QZ Tray: сдвиг и зазор прямо в PDF (core/printers.fit_pdf).

    PDF приходит из браузера — тот, что он только что скачал для печати, — и
    уходит обратно подогнанным. Сервер у себя его не хранит.
    """
    check_csrf(request)
    raw = str(payload.get("pdf") or "")
    if not raw or len(raw) > FIT_LIMIT:
        raise HTTPException(status_code=400, detail="Нет файла для печати или он слишком большой")
    try:
        pdf = base64.b64decode(raw, validate=True)
        values = {"gap": core_printers.parse_mm(payload.get("gap"), 0, core_printers.GAP_MAX, "Зазор")}
        for side in core_printers.SIDES:
            values[side] = core_printers.parse_mm(payload.get(side), 0, core_printers.SHIFT_MAX, "Сдвиг")
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Файл не прочитался") from exc
    try:
        fitted = core_printers.fit_pdf(pdf, **values)
    except Exception as exc:  # noqa: BLE001 - битый PDF: печать пойдёт как есть, а причину покажем
        raise HTTPException(status_code=400, detail=f"PDF не удалось подогнать: {exc}") from exc
    return {"pdf": base64.b64encode(fitted).decode()}


@router.get("/api/printers/sample.pdf")
def api_sample(width: str = "58", height: str = "40", title: str = "",
               user: dict = Depends(current_user)):  # noqa: ARG001
    """Пробная этикетка нужного размера: рамка у края бумаги — по ней видно сдвиг."""
    try:
        w = core_printers.parse_mm(width, *core_printers.PAPER_MM, "Ширина")
        h = core_printers.parse_mm(height, *core_printers.PAPER_MM, "Высота")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pdf = core_printers.sample_pdf(w, h, (title or "Ozon Pack · пробная печать")[:200])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="sample.pdf"', "Cache-Control": "no-store",
                             **core_printers.size_header(pdf)})


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
