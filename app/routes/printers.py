"""«Принтеры»: куда и на какой лист печатается каждый документ, и QZ Tray.

Таблица во вкладке «Сборка и печать» доступна всем, включая сборщика; остальные
настройки и галочки сборки — по прежним правам. Сборщик у стола сам знает,
куда воткнут какой принтер, и поправить это должен уметь без владельца. Кто
что поменял, пишется в журнал.

Сертификат и подпись нужны каждому, кто печатает: браузер подписывает ими
запросы к QZ Tray на своём компьютере. Как это устроено — в core/printers.py.
"""
from __future__ import annotations

import base64
import binascii
import json

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from ..core import access, db, marking, product_labels
from ..core import printers as core_printers
from ..core.deps import check_csrf, current_user

router = APIRouter()


@router.get('/printers')
def printers_page(user: dict = Depends(current_user)):
    """Старая закладка ведёт к общей вкладке; доступна каждому сотруднику."""
    return RedirectResponse('/settings/printing', status_code=303)


@router.post("/api/printers")
def api_save(request: Request, payload: dict = Body(...), user: dict = Depends(current_user)):
    """Сохранить строки «документ — размер листа — принтер — ориентация». Пусто — «через браузер»."""
    check_csrf(request)
    flags = {}
    for key, kv in [('print_product_barcodes', product_labels.KV_ENABLED),
                    ('print_marking_duplicates', marking.KV_AUTO)]:
        if key not in payload:
            continue
        if not access.is_manager(user):
            raise HTTPException(403, 'Параметры сборки меняет владелец или администратор')
        if not isinstance(payload[key], bool):
            raise HTTPException(400, 'Укажите галочки печати значениями true или false')
        flags[kv] = '1' if payload[key] else '0'
    try:
        validated = core_printers.validate_rows(payload.get('rows'))
        formats = core_printers.validate_formats(payload.get('formats'))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Все поля проверены до записи. Ошибка формата/флага не меняет принтеры.
    with db.write() as conn:
        values = {core_printers.KV_ROWS: json.dumps(validated, ensure_ascii=False), **flags}
        if payload.get('formats') is not None:
            values[core_printers.KV_FORMATS] = json.dumps(formats, ensure_ascii=False)
        conn.executemany('INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         list(values.items()))
    saved = core_printers.rows()
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
    if product_labels.KV_ENABLED in flags:
        summary += '; ШК перед стикером: ' + ('да' if product_labels.enabled() else 'нет')
    if marking.KV_AUTO in flags:
        summary += '; дубликат «Честный знак»: ' + ('да' if marking.enabled() else 'нет')
    db.log_event("printers_saved", user=user, message=summary)
    return {"status": "ok", "printers": core_printers.setup(),
            'print_product_barcodes': product_labels.enabled(), 'print_marking_duplicates': marking.enabled()}


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
