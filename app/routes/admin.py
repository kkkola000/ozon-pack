"""Журнал событий, настройки, пользователи."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import accounts, db, options, security, sync
from ..avito import AvitoClient, AvitoError
from ..deps import check_csrf, current_account, current_user, require_admin, require_ozon_account, templates
from ..ozon import OzonClient, OzonError, get_client

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
    "returns_statuses_set": "Изменены статусы возвратов",
    "user_created": "Создан пользователь",
    "user_updated": "Изменён пользователь",
    "account_created": "Добавлен кабинет",
    "account_updated": "Изменён кабинет",
    "account_credentials_set": "Сохранены ключи кабинета",
    "account_deleted": "Удалён кабинет",
    "account_switch": "Переключение кабинета",
    "avito_confirm": "Заказ Avito подтверждён",
    "avito_ship": "Заказ Avito отправлен",
    "avito_label_print": "Печать этикетки Avito",
    "avito_returns_print": "Печать листа возвратов Avito",
    "avito_returns_taken": "Возвраты Avito забраны",
    "avito_returns_untaken": "Отметка снята",
    "avito_error": "Ошибка Avito",
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
    account = current_account(request)
    # Журнал показываем по текущему кабинету; общие события (вход, польз.) — всегда.
    conditions, params = ["(account_id IS NULL OR account_id = ?)"], [account["id"] if account else 0]
    if kind:
        conditions.append("kind = ?")
        params.append(kind)
    if level:
        conditions.append("level = ?")
        params.append(level)
    if posting:
        conditions.append("(posting_number LIKE ? OR barcode LIKE ? OR sku LIKE ?)")
        params += [f"%{posting}%"] * 3
    where = " WHERE " + " AND ".join(conditions)
    events = db.query(f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ?", params + [min(limit, 2000)])
    kinds = [row["kind"] for row in db.query("SELECT DISTINCT kind FROM events ORDER BY kind")]
    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "request": request,
            "user": user,
            "events": [dict(e) for e in events],
            "account": account,
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
    account = current_account(request)
    aid = (account["id"] if account else 0,)
    # Показываем счётчики текущего кабинета — и только те, что для его площадки.
    if account and account["marketplace"] == "avito":
        stats = {
            "Ждут подтверждения": db.query_one(
                "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = 'on_confirmation'", aid
            )["c"],
            "Ждут отправки": db.query_one(
                "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = 'ready_to_ship'", aid
            )["c"],
            "Позиций в заказах": db.query_one(
                "SELECT COUNT(*) AS c FROM avito_order_items WHERE account_id = ?", aid
            )["c"],
        }
    else:
        stats = {
            "Отправлений": db.query_one("SELECT COUNT(*) AS c FROM postings WHERE account_id = ?", aid)["c"],
            "Собрано": db.query_one(
                "SELECT COUNT(*) AS c FROM postings WHERE account_id = ? AND local_state = 'packed'", aid
            )["c"],
            "Товаров": db.query_one("SELECT COUNT(*) AS c FROM products WHERE account_id = ?", aid)["c"],
            "Штрихкодов": db.query_one("SELECT COUNT(*) AS c FROM product_barcodes WHERE account_id = ?", aid)["c"],
            "Возвратов": db.query_one("SELECT COUNT(*) AS c FROM returns WHERE account_id = ?", aid)["c"],
        }
    stats["Событий"] = db.query_one("SELECT COUNT(*) AS c FROM events")["c"]
    cabinets = [
        {**item, **{"status": accounts.status(item)}}
        for item in accounts.all_accounts()
    ]
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "stats": stats,
            "account": account,
            "cabinets": cabinets,
            "marketplaces": accounts.MARKETPLACES,
            "ozon": accounts.status(account),
            "returns_statuses": options.get_returns_statuses(),
            "returns_choices": options.RETURN_STATUS_CHOICES,
            "returns_source": options.returns_source(),
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


def _probe(marketplace: str, client_id: str, api_key: str) -> None:
    """Проверить ключи до сохранения: опечатка не должна оставить склад без данных."""
    # Без повторов и с коротким таймаутом: оператор ждёт ответа здесь и сейчас.
    if marketplace == "avito":
        probe = AvitoClient(client_id=client_id, client_secret=api_key, max_retries=1, timeout=20)
        try:
            probe.ping()
        except AvitoError as exc:
            if exc.status in (401, 403):
                detail = (
                    f"Avito отклонил ключи: {exc.message}. "
                    "Проверьте client_id и client_secret в личном кабинете."
                )
            elif exc.status is None:
                detail = (
                    f"Не удалось связаться с Avito: {exc.message}. Проверьте доступ в интернет с сервера; "
                    "если он есть, сохраните ключи без проверки."
                )
            else:
                detail = f"Avito ответил ошибкой: {exc.message}"
            raise HTTPException(status_code=400, detail=detail) from exc
        finally:
            probe.close()
        return

    probe = OzonClient(client_id=client_id, api_key=api_key, max_retries=1, timeout=20)
    try:
        probe.ping()
    except OzonError as exc:
        if exc.status in (401, 403):
            detail = f"Ozon отклонил ключи: {exc.message}. Проверьте Client-Id и Api-Key в личном кабинете."
        elif exc.status is None:
            detail = (
                f"Не удалось связаться с Ozon: {exc.message}. Проверьте доступ в интернет с сервера; "
                "если он есть, сохраните ключи без проверки."
            )
        else:
            detail = f"Ozon ответил ошибкой: {exc.message}"
        raise HTTPException(status_code=400, detail=detail) from exc
    finally:
        probe.close()


@router.post("/api/accounts")
def api_create_account(request: Request, payload: dict = Body(...), admin: dict = Depends(require_admin)):
    """Добавить кабинет магазина. Ключи проверяются до сохранения."""
    check_csrf(request)
    marketplace = str(payload.get("marketplace") or "").strip()
    title = str(payload.get("title") or "").strip()
    client_id = str(payload.get("client_id") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    problem = accounts.validate(marketplace, title, client_id, api_key)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    if client_id and api_key and not payload.get("skip_test"):
        _probe(marketplace, client_id, api_key)

    account_id = accounts.create(marketplace, title, client_id, api_key, user=admin)
    worker = sync.get_worker()
    if worker:
        worker.request_sync()
    hint = "" if client_id else " Кабинет пока в демо-режиме: добавьте ключи."
    return {
        "status": "ok",
        "message": f"Кабинет «{title}» добавлен.{hint}",
        "account_id": account_id,
    }


@router.post("/api/accounts/{account_id}")
def api_update_account(account_id: int, request: Request, payload: dict = Body(...),
                       admin: dict = Depends(require_admin)):
    """Изменить название, ключи или включённость кабинета."""
    check_csrf(request)
    account = accounts.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Кабинет не найден")

    title = payload.get("title")
    title = str(title).strip() if title is not None else None
    client_id = payload.get("client_id")
    api_key = payload.get("api_key")
    changes = []

    if client_id is not None or api_key is not None:
        client_id = str(client_id or "").strip()
        api_key = str(api_key or "").strip()
        problem = accounts.validate(
            account["marketplace"], title if title is not None else account["title"], client_id, api_key
        )
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        if client_id and api_key and not payload.get("skip_test"):
            _probe(account["marketplace"], client_id, api_key)
        changes.append("ключи")
    elif title is not None:
        problem = accounts.validate(account["marketplace"], title, "", "")
        if problem:
            raise HTTPException(status_code=400, detail=problem)

    active = payload.get("active")
    if active is not None:
        active = bool(active)
        if not active:
            others = [a for a in accounts.all_accounts(active_only=True) if a["id"] != account_id]
            if not others:
                raise HTTPException(status_code=400, detail="Нельзя выключить единственный кабинет")
        changes.append("включён" if active else "выключен")
    if title is not None and title != account["title"]:
        changes.append(f"название «{title}»")

    accounts.update(
        account_id,
        title=title,
        client_id=client_id,
        api_key=api_key,
        active=active,
        user=admin,
    )
    db.log_event(
        "account_updated", account_id=account_id, user=admin,
        message=f"{account['title']}: {', '.join(changes) or 'без изменений'}",
    )
    worker = sync.get_worker()
    if worker:
        worker.request_sync()
    return {
        "status": "ok",
        "message": f"Кабинет «{title or account['title']}»: {', '.join(changes) or 'без изменений'}",
    }


@router.post("/api/accounts/{account_id}/delete")
def api_delete_account(account_id: int, request: Request, admin: dict = Depends(require_admin)):
    """Удалить кабинет вместе с его заказами и товарами."""
    check_csrf(request)
    account = accounts.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Кабинет не найден")
    if len(accounts.all_accounts()) < 2:
        raise HTTPException(status_code=400, detail="Нельзя удалить единственный кабинет")
    accounts.delete(account_id, user=admin)
    return {"status": "ok", "message": f"Кабинет «{account['title']}» удалён вместе с его данными"}


@router.post("/api/accounts/{account_id}/test")
def api_test_account(account_id: int, request: Request, admin: dict = Depends(require_admin)):
    """Проверить связь с площадкой ключами кабинета."""
    check_csrf(request)
    account = accounts.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Кабинет не найден")
    if accounts.is_demo(account):
        return {"status": "ok", "message": f"«{account['title']}»: демо-режим, ключи не используются"}
    try:
        if account["marketplace"] == "avito":
            from ..avito import get_client as get_avito_client

            result = get_avito_client(account).ping()
        else:
            result = get_client(account).ping()
    except (OzonError, AvitoError) as exc:
        raise HTTPException(status_code=502, detail=f"{account['title']}: {exc}") from exc
    return {"status": "ok", "message": f"«{account['title']}»: ключи работают", "result": result}


@router.post("/api/postings/{posting_number}/reset")
def api_reset_posting(posting_number: str, request: Request, admin: dict = Depends(require_admin),
                      account: dict = Depends(require_ozon_account)):
    """Снять отметку «собрано» — например, если сборку закрыли по ошибке."""
    check_csrf(request)
    row = db.query_one(
        "SELECT * FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting_number)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Отправление не найдено")
    state = "cancelled" if row["status"] == "cancelled" else "new"
    db.execute(
        "UPDATE postings SET local_state = ?, packed_at = NULL, packed_by = NULL,"
        " claim_user_id = NULL, claim_login = NULL, claim_at = NULL WHERE account_id = ? AND posting_number = ?",
        (state, account["id"], posting_number),
    )
    db.log_event(
        "posting_reset", level="warn", account_id=account["id"], user=admin,
        posting_number=posting_number, message="Сброшена отметка сборки",
    )
    return {"status": "ok", "message": f"{posting_number}: отметка сборки снята"}


@router.post("/api/returns/statuses")
def api_returns_statuses(request: Request, payload: dict = Body(...), admin: dict = Depends(require_admin),
                         account: dict = Depends(require_ozon_account)):
    """Какие статусы возвратов панель загружает и показывает как доступные."""
    check_csrf(request)
    raw = payload.get("statuses") or []
    known = {code for code, _label, _hint in options.RETURN_STATUS_CHOICES}
    statuses = [str(s).strip() for s in raw if str(s).strip() in known]
    if not statuses:
        raise HTTPException(status_code=400, detail="Выберите хотя бы один статус")

    options.set_returns_statuses(statuses, user=admin)
    try:
        result = sync.sync_returns(account)
    except Exception as exc:  # noqa: BLE001 - причину показываем оператору
        raise HTTPException(status_code=502, detail=f"Статусы сохранены, но обновить возвраты не удалось: {exc}") from exc
    names = ", ".join(options.status_label(code) for code in statuses)
    return {
        "status": "ok",
        "message": f"Загружаются возвраты в статусах: {names}. Обновлено: {result.get('returns', 0)}.",
        "result": result,
    }
