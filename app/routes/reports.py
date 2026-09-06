"""Отчёт об отгруженных товарах — раздел администратора."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .. import accounts, report
from ..deps import require_admin, templates

router = APIRouter()

STATUS_FILTERS = [
    ("", "все строки"),
    (report.STATUS_OK, "только отгруженные"),
    (report.STATUS_UNMATCHED, "штрихкод не опознан"),
    (report.STATUS_ERROR, "только ошибки"),
]


def _account_filter(raw: str | None) -> int | None:
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return None
    return value or None


def _valid_day(day: str) -> str:
    try:
        return date.fromisoformat(day).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Неверная дата отчёта") from exc


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, account_id: str | None = None,
                 user: dict = Depends(require_admin)):
    chosen = _account_filter(account_id)
    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "request": request,
            "user": user,
            "days": report.days(account_id=chosen),
            "accounts": accounts.all_accounts(),
            "account_id": chosen,
            "cutoff": report.get_cutoff(),
            "cutoff_hint": report.cutoff_hint(),
            "today": report.report_date(),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "reports",
        },
    )


@router.get("/reports/{day}", response_class=HTMLResponse)
def report_day_page(day: str, request: Request, account_id: str | None = None,
                    status: str | None = None, user: dict = Depends(require_admin)):
    day = _valid_day(day)
    chosen = _account_filter(account_id)
    return templates.TemplateResponse(
        request,
        "report_day.html",
        {
            "request": request,
            "user": user,
            "day": day,
            "closed": report.is_closed(day),
            "rows": report.rows(day, account_id=chosen, status=status),
            "totals": report.totals(day, account_id=chosen),
            "accounts": accounts.all_accounts(),
            "account_id": chosen,
            "status": status or "",
            "status_filters": STATUS_FILTERS,
            "cutoff": report.get_cutoff(),
            "cutoff_hint": report.cutoff_hint(),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "reports",
        },
    )


@router.get("/reports/{day}/csv")
def report_csv(day: str, account_id: str | None = None, status: str | None = None,
               user: dict = Depends(require_admin)):
    day = _valid_day(day)
    blob = report.to_csv(day, account_id=_account_filter(account_id), status=status)
    name = f"otgruzka-{day}.csv"
    return Response(
        content=blob,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )
