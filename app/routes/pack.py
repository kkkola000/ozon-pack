"""Рабочее место сборщика: сканирование, печать стикеров, завершение сборки."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .. import db, options, packing, pdfrender, store
from ..config import settings
from ..deps import check_csrf, current_user, templates
from ..ozon import OzonError

router = APIRouter()


@router.get("/pack", response_class=HTMLResponse)
def pack_page(request: Request, user: dict = Depends(current_user)):
    state = packing.load_state(user)
    counters = _counters()
    return templates.TemplateResponse(
        request,
        "pack.html",
        {
            "request": request,
            "user": user,
            "state": state,
            "counters": counters,
            "csrf": request.state.session.get("csrf"),
            "active_tab": "pack",
        },
    )


def _counters() -> dict:
    return {
        "awaiting_packaging": db.query_one(
            "SELECT COUNT(*) AS c FROM postings WHERE status = ?", (store.STATUS_AWAITING_PACKAGING,)
        )["c"],
        "awaiting_deliver": db.query_one(
            "SELECT COUNT(*) AS c FROM postings WHERE status = ? AND local_state = 'new'",
            (store.STATUS_AWAITING_DELIVER,),
        )["c"],
        "packed_today": db.query_one(
            "SELECT COUNT(*) AS c FROM postings WHERE local_state = 'packed' AND packed_at >= date('now')"
        )["c"],
        "returns_ready": db.query_one("SELECT COUNT(*) AS c FROM returns WHERE is_ready = 1 AND taken_at IS NULL")["c"],
    }


@router.get("/api/state")
def api_state(user: dict = Depends(current_user)):
    return {"state": packing.load_state(user), "counters": _counters()}


@router.post("/api/scan")
def api_scan(request: Request, payload: dict = Body(...), user: dict = Depends(current_user)):
    check_csrf(request)
    result = packing.scan(user, str(payload.get("code") or ""))
    result["counters"] = _counters()
    return result


@router.post("/api/select")
def api_select(request: Request, payload: dict = Body(...), user: dict = Depends(current_user)):
    check_csrf(request)
    number = str(payload.get("posting_number") or "")
    result = packing.select_posting(user, number, first_sku=payload.get("sku") or None)
    result["counters"] = _counters()
    return result


@router.post("/api/release")
def api_release(request: Request, user: dict = Depends(current_user)):
    check_csrf(request)
    result = packing.release(user)
    result["counters"] = _counters()
    return result


@router.post("/api/complete")
def api_complete(request: Request, payload: dict = Body(default={}), user: dict = Depends(current_user)):
    """Ручное завершение — например, если стикер не читается сканером."""
    check_csrf(request)
    state = packing.load_state(user)
    if not state["active"]:
        raise HTTPException(status_code=400, detail="Нет активного отправления")
    if settings.require_all_items and not state["complete"] and user.get("role") != "admin":
        raise HTTPException(status_code=400, detail="Сначала отсканируйте все товары")
    result = packing.complete(user, state["active"]["posting_number"], code=payload.get("reason") or "ручное завершение")
    result["counters"] = _counters()
    return result


@router.get("/api/label/{posting_number}.pdf")
def api_label(posting_number: str, user: dict = Depends(current_user)):
    try:
        pdf, filename = packing.label_pdf(user, [posting_number])
    except OzonError as exc:
        raise HTTPException(status_code=502, detail=f"Ozon не отдал стикер: {exc.message}") from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.post("/api/labels.pdf")
def api_labels(request: Request, payload: dict = Body(...), user: dict = Depends(current_user)):
    """Пачка стикеров — для печати нескольких отправлений сразу."""
    check_csrf(request)
    numbers = [str(n) for n in (payload.get("posting_numbers") or []) if n]
    if not numbers:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного отправления")
    try:
        pdf, filename = packing.label_pdf(user, numbers)
    except OzonError as exc:
        raise HTTPException(status_code=502, detail=f"Ozon не отдал стикеры: {exc.message}") from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-store"},
    )


def _label_print_page(request: Request, posting_numbers: list[str], user: dict) -> HTMLResponse:
    """Стикеры как обычная HTML-страница с картинками.

    Safari не печатает PDF во фрейме — выходит пустой лист. Картинку в HTML он
    печатает без нареканий, поэтому для него это основной путь.
    """
    if not posting_numbers:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного отправления")
    if not pdfrender.is_available():
        raise HTTPException(
            status_code=501,
            detail="На сервере не установлена библиотека рендера PDF (pypdfium2) — печать картинкой недоступна",
        )
    try:
        pdf, _filename = packing.label_pdf(user, posting_numbers)
    except OzonError as exc:
        raise HTTPException(status_code=502, detail=f"Ozon не отдал стикер: {exc.message}") from exc

    pages = pdfrender.render_pdf(pdf)
    if not pages:
        raise HTTPException(status_code=502, detail="Не удалось преобразовать стикер в картинку")

    import base64

    return templates.TemplateResponse(
        request,
        "label_print.html",
        {
            "request": request,
            "title": ", ".join(posting_numbers),
            "page_width": pages[0].width_mm,
            "page_height": pages[0].height_mm,
            "pages": [{"data": base64.b64encode(page.png).decode()} for page in pages],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/label/{posting_number}/print", response_class=HTMLResponse)
def api_label_print(posting_number: str, request: Request, user: dict = Depends(current_user)):
    return _label_print_page(request, [posting_number], user)


@router.get("/api/labels/print", response_class=HTMLResponse)
def api_labels_print(request: Request, numbers: str = "", user: dict = Depends(current_user)):
    posting_numbers = [n.strip() for n in numbers.split(",") if n.strip()]
    return _label_print_page(request, posting_numbers, user)


def _label_images(posting_numbers: list[str], dpi: int, user: dict) -> dict:
    import base64

    if not posting_numbers:
        raise HTTPException(status_code=400, detail="Не выбрано ни одного отправления")
    if not pdfrender.is_available():
        raise HTTPException(status_code=501, detail="На сервере нет библиотеки рендера PDF (pypdfium2)")
    try:
        pdf, _filename = packing.label_pdf(user, posting_numbers)
    except OzonError as exc:
        raise HTTPException(status_code=502, detail=f"Ozon не отдал стикер: {exc.message}") from exc

    pages = pdfrender.render_pdf(pdf, dpi=max(150, min(600, dpi)))
    if not pages:
        raise HTTPException(status_code=502, detail="Не удалось преобразовать стикер в картинку")
    return {
        "posting_numbers": posting_numbers,
        "width_mm": pages[0].width_mm,
        "height_mm": pages[0].height_mm,
        "pages": ["data:image/png;base64," + base64.b64encode(page.png).decode() for page in pages],
    }


@router.get("/api/labels/image")
def api_labels_image(numbers: str = "", dpi: int = 300, user: dict = Depends(current_user)):
    """Пачка стикеров картинками — для печати нескольких отправлений сразу."""
    return _label_images([n.strip() for n in numbers.split(",") if n.strip()], dpi, user)


@router.get("/api/label/{posting_number}/image")
def api_label_image(posting_number: str, request: Request, dpi: int = 300, user: dict = Depends(current_user)):
    """Стикер картинками — печатаются прямо на странице панели.

    Печать из отдельного окна ведёт себя по-разному в браузерах (Safari умеет
    показать два окна печати подряд), а печать основного документа одинакова
    везде. Поэтому отдаём картинки, а страница подставляет их себе и печатает.
    """
    return _label_images([posting_number], dpi, user)
