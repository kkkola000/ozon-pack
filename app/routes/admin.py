"""Журнал событий, настройки, пользователи."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import db, security, store, sync
from ..config import settings
from ..deps import check_csrf, current_user, require_admin, templates
from ..ozon import OzonError, get_client

router = APIRouter()

EVENT_LABELS = {
    "login": "Вход",
    "login_failed": "Неудачный вход",
    "logout": "Выход",
    "pack_start": "Начата сборка",
    "pack_complete": "Отправление собрано",
    "pack_release": "Сборка отменена",
    "scan_product": "Скан товара",
    "scan_wrong_product": "Чужой товар",
    "scan_extra_product": "Лишний скан товара",
    "scan_wrong_label": "Чужой стикер",
    "scan_label_incomplete": "Стикер до сборки",
    "scan_packed_again": "Повторный скан собранного",
    "scan_no_candidates": "Товар не нужен",
    "scan_choice": "Выбор отправления",
    "scan_unknown": "Неизвестный код",
    "scan_unknown_posting": "Отправление не найдено",
    "label_print": "Печать стикера",
    "ship": "Сборка в Ozon",
    "ship_error": "Ошибка сборки в Ozon",
    "returns_print": "Печать листа возвратов",
    "returns_taken": "Возвраты забраны",
    "returns_untaken": "Отметка снята",
    "returns_giveout": "Штрихкод выдачи",
    "user_created": "Создан пользователь",
    "user_updated": "Изменён пользователь",
}


@router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    kind: str = "",
    level: str = "",
    posting: str = "",
    limit: int = 300,
    user: dict = Depends(current_user),
):
    conditions, params = [], []
    if kind:
        conditions.append("kind = ?")
        params.append(kind)
    if level:
        conditions.append("level = ?")
        params.append(level)
    if posting:
        conditions.append("(posting_number LIKE ? OR barcode LIKE ? OR sku LIKE ?)")
        params += [f"%{posting}%"] * 3
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    events = db.query(f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ?", params + [min(limit, 2000)])
    kinds = [row["kind"] for row in db.query("SELECT DISTINCT kind FROM events ORDER BY kind")]
    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "request": request,
            "user": user,
            "events": [dict(e) for e in events],
            "labels": EVENT_LABELS,
            "kinds": kinds,
            "kind": kind,
            "level": level,
            "posting": posting,
            "active_tab": "logs",
            "csrf": request.state.session.get("csrf"),
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: dict = Depends(require_admin)):
    users = [dict(row) for row in db.query("SELECT id, login, role, active, created_at FROM users ORDER BY login")]
    stats = {
        "postings": db.query_one("SELECT COUNT(*) AS c FROM postings")["c"],
        "packed": db.query_one("SELECT COUNT(*) AS c FROM postings WHERE local_state = 'packed'")["c"],
        "products": db.query_one("SELECT COUNT(*) AS c FROM products")["c"],
        "barcodes": db.query_one("SELECT COUNT(*) AS c FROM product_barcodes")["c"],
        "returns": db.query_one("SELECT COUNT(*) AS c FROM returns")["c"],
        "events": db.query_one("SELECT COUNT(*) AS c FROM events")["c"],
    }
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "stats": stats,
            "sync": sync.status(),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "settings",
        },
    )


@router.post("/api/users")
def api_create_user(request: Request, payload: dict = Body(...), admin: dict = Depends(require_admin)):
    check_csrf(request)
    login = str(payload.get("login") or "").strip()
    password = str(payload.get("password") or "").strip()
    role = "admin" if payload.get("role") == "admin" else "packer"
    if len(login) < 3:
        raise HTTPException(status_code=400, detail="Логин короче 3 символов")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Пароль короче 6 символов")
    if db.query_one("SELECT id FROM users WHERE login = ?", (login,)):
        raise HTTPException(status_code=409, detail="Такой логин уже есть")
    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES(?,?,?,1,?)",
        (login, security.hash_password(password), role, db.now_iso()),
    )
    db.log_event("user_created", user=admin, message=f"{login} ({role})")
    return {"status": "ok", "message": f"Пользователь {login} создан"}


@router.post("/api/users/{user_id}")
def api_update_user(user_id: int, request: Request, payload: dict = Body(...), admin: dict = Depends(require_admin)):
    check_csrf(request)
    row = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    changes = []
    if "active" in payload:
        active = 1 if payload["active"] else 0
        if not active and row["role"] == "admin":
            others = db.query_one("SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND active = 1 AND id != ?", (user_id,))
            if not others["c"]:
                raise HTTPException(status_code=400, detail="Нельзя отключить последнего администратора")
        db.execute("UPDATE users SET active = ? WHERE id = ?", (active, user_id))
        changes.append("включён" if active else "отключён")
    if payload.get("password"):
        password = str(payload["password"]).strip()
        if len(password) < 6:
            raise HTTPException(status_code=400, detail="Пароль короче 6 символов")
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (security.hash_password(password), user_id))
        changes.append("сменён пароль")
    if payload.get("role") in ("admin", "packer"):
        db.execute("UPDATE users SET role = ? WHERE id = ?", (payload["role"], user_id))
        changes.append(f"роль {payload['role']}")
    db.log_event("user_updated", user=admin, message=f"{row['login']}: {', '.join(changes) or 'без изменений'}")
    return {"status": "ok", "message": f"{row['login']}: {', '.join(changes) or 'без изменений'}"}


@router.post("/api/ozon/test")
def api_ozon_test(request: Request, admin: dict = Depends(require_admin)):
    check_csrf(request)
    try:
        result = get_client().ping()
    except OzonError as exc:
        raise HTTPException(status_code=502, detail=f"Ozon недоступен: {exc}") from exc
    return {
        "status": "ok",
        "message": "Демо-режим: ключи не используются" if settings.demo else "Ключи Ozon работают",
        "result": result,
    }


@router.post("/api/postings/{posting_number}/reset")
def api_reset_posting(posting_number: str, request: Request, admin: dict = Depends(require_admin)):
    """Снять отметку «собрано» — например, если сборку закрыли по ошибке."""
    check_csrf(request)
    row = db.query_one("SELECT * FROM postings WHERE posting_number = ?", (posting_number,))
    if not row:
        raise HTTPException(status_code=404, detail="Отправление не найдено")
    state = "cancelled" if row["status"] == "cancelled" else "new"
    db.execute(
        "UPDATE postings SET local_state = ?, packed_at = NULL, packed_by = NULL,"
        " claim_user_id = NULL, claim_login = NULL, claim_at = NULL WHERE posting_number = ?",
        (state, posting_number),
    )
    db.log_event("posting_reset", level="warn", user=admin, posting_number=posting_number, message="Сброшена отметка сборки")
    return {"status": "ok", "message": f"{posting_number}: отметка сборки снята"}
