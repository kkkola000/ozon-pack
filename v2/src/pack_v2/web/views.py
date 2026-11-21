from zoneinfo import ZoneInfo

from fastapi import Request

from pack_v2.modules.identity.public import ROLES, Authenticated

NAV = (
    ("pack", "Сборка", "/pack"),
    ("orders", "Заказы", "/orders"),
    ("returns", "Возвраты", "/returns"),
    ("products", "Товары", "/products"),
)
REPORTS = (
    ("reports", "Отчёты", "/reports"),
    ("logs", "Журнал", "/logs"),
    ("stickers", "Стикеры", "/reports/stickers"),
)
TABS = {
    "printing": (
        "Сборка и печать",
        "Параметры сборки и принтеров будут доступны после подключения рабочих операций.",
    ),
    "accounts": ("Кабинеты", ""),
    "returns": ("Возвраты", "Параметры возвратов будут перенесены вместе с приёмкой и актами."),
    "staff": ("Сотрудники", "Форма управления сотрудниками будет добавлена на следующем этапе."),
}
ACCOUNT_STATUSES = {
    "unverified": ("Не проверен", ""),
    "checking": ("Проверяется", "accent"),
    "passed": ("Чтение доступно", "ok"),
    "partial": ("Доступ частичный", "warn"),
    "failed": ("Нет доступа", "err"),
    "unavailable": ("Повторите проверку", "warn"),
}
NOTICES = {
    "saved": "Кабинет сохранён",
    "updated": "Настройки кабинета сохранены",
    "checked": "Проверка завершена. Результат указан под кабинетом",
    "active": "Состояние кабинета изменено",
}


def render(request: Request, template: str, *, saved: Authenticated | None = None, **context):
    user = saved.user if saved else None
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "user": user,
            "csrf": saved.csrf_token if saved else "",
            "roles": ROLES,
            "nav": [v for v in NAV if user and user.can(v[0])],
            "reports_nav": [v for v in REPORTS if user and user.can(v[0])],
            "active_tab": "",
            **context,
        },
    )


def settings_page(
    request: Request,
    saved: Authenticated,
    tab: str = "printing",
    *,
    error: str | None = None,
    form: dict | None = None,
    status: int = 200,
):
    saved.user.require("settings")
    selected = tab if tab in TABS else "printing"
    accounts = request.app.state.connections.list_accounts(saved.user) if selected == "accounts" else []
    response = render(
        request,
        "settings.html",
        saved=saved,
        tabs=TABS,
        selected=selected,
        title="Настройки",
        active_tab="settings",
        accounts=accounts,
        account_statuses=ACCOUNT_STATUSES,
        notice=NOTICES.get(request.query_params.get("notice")) if not error else None,
        error=error,
        form=form or {},
        msk=lambda value: (
            value.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M") if value else ""
        ),
    )
    response.status_code = status
    return response
