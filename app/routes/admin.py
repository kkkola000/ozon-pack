"""Журнал событий, настройки, пользователи."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from urllib.parse import urlencode

from ..core import access, accounts, db, report, security, shops, sync
from ..core.deps import check_csrf, require_manager, require_section, templates
from ..markets import registry
from ..markets.base import KeyCheckError, MarketError


router = APIRouter()

# Панель часто стоит открытой в интернете, а вход у неё один на склад: шесть
# символов перебираются словарём за минуты, сколько бы итераций ни было у PBKDF2.
MIN_PASSWORD = 12

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
    "printers_saved": "Настройка принтеров",
    "qz_certificate": "Сертификат для QZ Tray",
    "labels_archive": "Выгрузка стикеров",
    "ship": "Сборка в Ozon",
    "posting_reset": "Сброшена отметка сборки",
    "ship_error": "Ошибка сборки в Ozon",
    "returns_print": "Печать листа возвратов",
    "returns_pdf": "Лист возвратов в PDF",
    "return_mark": "Отметка о возврате",
    "return_act_confirm": "Акт возвратов подтверждён",
    "return_act_received": "Составлен акт возвратов",
    "return_act_upload": "Акт возвратов загружен файлом",
    "return_act_import": "Акты возвратов добавлены из Ozon",
    "returns_giveout": "Штрихкод выдачи",
    "returns_statuses_set": "Изменены статусы возвратов",
    "returns_received_set": "Изменены статусы «Получен»",
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
    "avito_labels_archive": "Выгрузка этикеток Avito",
    "avito_order_reset": "Сброшена отметка сборки Avito",
    # Своего листа возвратов у Avito больше нет — лист общий. Подпись оставлена
    # для записей в журнале, сделанных до объединения разделов.
    "avito_returns_print": "Печать листа возвратов Avito",
    "avito_error": "Ошибка Avito",
    "yandex_pack_start": "Начата сборка (Маркет)",
    "yandex_pack_complete": "Заказ Маркета собран",
    "yandex_pack_release": "Сборка Маркета отменена",
    "yandex_label_print": "Печать ярлыка Маркета",
    "yandex_labels_archive": "Выгрузка ярлыков Маркета",
    "yandex_order_reset": "Сброшена отметка сборки (Маркет)",
    "yandex_error": "Ошибка Маркета",
}


@router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    kind: str = "",
    level: str = "",
    posting: str = "",
    shop: str = shops.ALL,
    limit: int = 300,
    user: dict = Depends(require_section("logs")),
):
    """Журнал — только администратору.

    В нём видны входы с IP-адресами, неудачные попытки входа вместе с
    логинами и действия всех сотрудников: сборщику для разбора пересорта это
    не нужно, а как список учётных записей вполне пригодно.
    """
    # Фильтр кабинетов, как в остальных разделах: «Все» — вся лента; выбран
    # кабинет — его события и общие (вход, пользователи, настройки).
    everyone = accounts.all_accounts()
    picked = shops.picked_of(shop, everyone)
    conditions, params = ["1 = 1"], []
    if picked != shops.ALL:
        conditions, params = ["(account_id IS NULL OR account_id = ?)"], [int(picked)]
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
            "shop_chips": shops.chips(everyone, picked, None, lambda value: "/logs?" + urlencode(
                {key: val for key, val in (("shop", value), ("kind", kind), ("level", level),
                                           ("posting", posting)) if val})),
            "picked": picked,
            "shop_titles": {account["id"]: account["title"] for account in everyone},
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
def settings_page(request: Request, user: dict = Depends(require_section("settings"))):
    users = []
    for row in db.query("SELECT id, login, role, sections, active, created_at FROM users ORDER BY role, login"):
        item = dict(row)
        item["sections"] = access.sections_of(item)
        item["role_label"] = access.role_label(item["role"])
        # Владельца администратор не трогает — кнопки ему показывать незачем.
        item["editable"] = access.may_manage(user, item)
        users.append(item)
    # Плитки — у каждого кабинета в его карточке: какие именно, знает площадка.
    cabinets = []
    for item in accounts.all_accounts():
        market = registry.get(item["marketplace"])
        cabinets.append({**item, "status": accounts.status(item),
                         "stats": dict(market.stats(item["id"])) if market else {}})
    stats = {"Событий": db.query_one("SELECT COUNT(*) AS c FROM events")["c"]}
    # Свои настройки площадки (у Ozon — статусы возвратов) — если у панели есть
    # хоть один её кабинет. Они общие на все кабинеты площадки.
    present = [market for market in registry.all_markets()
               if any(item["marketplace"] == market.code for item in cabinets)]
    extra: dict = {}
    for market in present:
        if market.settings_context:
            extra.update(market.settings_context(None))
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "stats": stats,
            "cabinets": cabinets,
            "marketplaces": registry.MARKETS,
            "report_cutoff": report.get_cutoff(),
            "report_cutoff_hint": report.cutoff_hint(),
            "roles": [r for r in access.ROLES if access.may_set_role(user, r[0])],
            "sections": access.SECTIONS,
            "manager_only": access.MANAGER_ONLY,
            "can_manage_users": access.is_manager(user),
            "is_owner": access.is_owner(user),
            # Что ещё показать в настройках — знает площадка (у Ozon: статусы возвратов).
            # Её значения и её же куски страницы; у кого их нет, у того раздел пуст.
            **extra,
            "settings_rows": [market.settings_rows for market in present if market.settings_rows],
            "settings_panel": [market.settings_panel for market in present if market.settings_panel],
            "sync": sync.status(),
            "csrf": request.state.session.get("csrf"),
            "active_tab": "settings",
        },
    )


def _last_owner(user_id: int) -> bool:
    """Останется ли панель без владельца, если этого убрать.

    Без владельца некому менять пароли и права — чинить панель пришлось бы
    руками в базе. Поэтому последнего не отключаем и не понижаем.
    """
    row = db.query_one(
        "SELECT COUNT(*) AS c FROM users WHERE role = ? AND active = 1 AND id != ?",
        (access.OWNER, user_id),
    )
    return not row["c"]


@router.post("/api/users")
def api_create_user(request: Request, payload: dict = Body(...), admin: dict = Depends(require_manager)):
    check_csrf(request)
    login = str(payload.get("login") or "").strip()
    password = str(payload.get("password") or "").strip()
    role = str(payload.get("role") or access.PACKER)
    if not access.may_set_role(admin, role):
        raise HTTPException(status_code=403, detail="Владельца назначает только владелец")
    if len(login) < 3:
        raise HTTPException(status_code=400, detail="Логин короче 3 символов")
    if len(password) < MIN_PASSWORD:
        raise HTTPException(status_code=400, detail=f"Пароль короче {MIN_PASSWORD} символов")
    if db.query_one("SELECT id FROM users WHERE login = ?", (login,)):
        raise HTTPException(status_code=409, detail="Такой логин уже есть")
    sections = access.dump_sections(role, payload.get("sections", access.DEFAULT_SECTIONS[role]))
    db.execute(
        "INSERT INTO users(login, password_hash, role, sections, active, created_at) VALUES(?,?,?,?,1,?)",
        (login, security.hash_password(password), role, sections, db.now_iso()),
    )
    db.log_event("user_created", user=admin,
                 message=f"{login} ({access.role_label(role)}): {sections}")
    return {"status": "ok", "message": f"{access.role_label(role)} {login} создан"}


@router.post("/api/users/{user_id}")
def api_update_user(user_id: int, request: Request, payload: dict = Body(...), admin: dict = Depends(require_manager)):
    """Роль, разделы, пароль и включение сотрудника.

    Администратор владельца не трогает — ни роль, ни разделы, ни пароль. Иначе
    «администратор» и «владелец» были бы одним и тем же: первый вторым же
    ключом и открыл бы себе всё.
    """
    check_csrf(request)
    row = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    target = dict(row)
    if not access.may_manage(admin, target):
        raise HTTPException(status_code=403, detail=access.why_not(admin, target))

    changes = []
    # Роль меняем первой: разделы чистятся уже по новой роли, иначе сборщику
    # осталась бы галочка «Настройки» от прежней администраторской.
    role = str(payload.get("role") or "")
    if role and role != target["role"]:
        if not access.may_set_role(admin, role):
            raise HTTPException(status_code=403, detail="Владельца назначает только владелец")
        if target["role"] == access.OWNER and _last_owner(user_id):
            raise HTTPException(status_code=400, detail="Это последний владелец — панель останется без хозяина")
        db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        target["role"] = role
        changes.append(f"роль: {access.role_label(role)}")

    if "sections" in payload:
        if target["role"] == access.OWNER:
            raise HTTPException(status_code=400, detail="У владельца доступны все разделы — их не урезают")
        sections = access.dump_sections(target["role"], payload.get("sections"))
        db.execute("UPDATE users SET sections = ? WHERE id = ?", (sections, user_id))
        names = [access.SECTION_LABELS[key] for key in access.clean_sections(target["role"], payload.get("sections"))]
        changes.append("разделы: " + (", ".join(names) or "нет"))

    if "active" in payload:
        active = 1 if payload["active"] else 0
        if not active and target["role"] == access.OWNER and _last_owner(user_id):
            raise HTTPException(status_code=400, detail="Нельзя отключить последнего владельца")
        db.execute("UPDATE users SET active = ? WHERE id = ?", (active, user_id))
        changes.append("включён" if active else "отключён")

    if payload.get("password"):
        password = str(payload["password"]).strip()
        if len(password) < MIN_PASSWORD:
            raise HTTPException(status_code=400, detail=f"Пароль короче {MIN_PASSWORD} символов")
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (security.hash_password(password), user_id))
        changes.append("сменён пароль")

    message = f"{row['login']}: {', '.join(changes) or 'без изменений'}"
    db.log_event("user_updated", user=admin, message=message)
    return {"status": "ok", "message": message}


def _probe(marketplace: str, client_id: str, api_key: str) -> None:
    """Проверить ключи до сохранения: опечатка не должна оставить склад без данных."""
    market = registry.get(marketplace)
    if market is None:
        raise HTTPException(status_code=400, detail="Неизвестная площадка")
    try:
        market.probe(client_id, api_key)
    except KeyCheckError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/accounts")
def api_create_account(request: Request, payload: dict = Body(...), admin: dict = Depends(require_manager)):
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
    hint = "" if client_id else " Кабинет пока без ключей: данные не загружаются."
    return {
        "status": "ok",
        "message": f"Кабинет «{title}» добавлен.{hint}",
        "account_id": account_id,
    }


@router.post("/api/accounts/{account_id}")
def api_update_account(account_id: int, request: Request, payload: dict = Body(...),
                       admin: dict = Depends(require_manager)):
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
def api_delete_account(account_id: int, request: Request, admin: dict = Depends(require_manager)):
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
def api_test_account(account_id: int, request: Request, admin: dict = Depends(require_manager)):
    """Проверить связь с площадкой ключами кабинета."""
    check_csrf(request)
    account = accounts.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Кабинет не найден")
    if not accounts.is_configured(account):
        raise HTTPException(status_code=400, detail=f"«{account['title']}»: ключи не заданы")
    market = registry.get(account["marketplace"])
    if market is None:
        raise HTTPException(status_code=400, detail=f"«{account['title']}»: неизвестная площадка")
    try:
        result = market.ping(account)
    except MarketError as exc:
        raise HTTPException(status_code=502, detail=f"{account['title']}: {exc}") from exc
    return {"status": "ok", "message": f"«{account['title']}»: ключи работают", "result": result}


@router.post("/api/report/cutoff")
def api_report_cutoff(request: Request, payload: dict = Body(...), admin: dict = Depends(require_manager)):
    """Во сколько закрывается отчётный день об отгрузке."""
    check_csrf(request)
    try:
        value = report.set_cutoff(str(payload.get("cutoff") or ""), user=admin)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "ok",
        "message": f"Отчётный день закрывается в {value}. Сканы после этого времени идут в следующий день.",
        "cutoff": value,
    }
