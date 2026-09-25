"""Роли и доступ к разделам.

Роль отвечает на вопрос «что человеку можно делать с самой панелью»: заводить
сотрудников, править ключи кабинетов, менять настройки. Разделы отвечают на
другой вопрос — «какие экраны он вообще видит». Это две разные вещи, и раньше
они были одной: администратор видел всё, сборщик — три экрана, и середины не
существовало.

Три роли:

* Владелец — хозяин панели. Видит всё и меняет всё, включая пароли и права
  других владельцев. Его права не урезаются: иначе панель можно было бы
  запереть так, что чинить её станет некому.
* Администратор — ведёт работу склада: заводит сотрудников, выдаёт доступы,
  правит ключи и настройки. Владельца не трогает — ни его роль, ни разделы,
  ни пароль. Иначе «администратор» и «владелец» были бы одним и тем же.
* Сборщик — работает на складе. Разделы ему выдают галочками.

Раздел «Настройки» сборщику выдать нельзя: там ключи площадок и выдача прав.
Кому это нужно — тот администратор, и роль у него должна быть честная.
"""
from __future__ import annotations

import json

OWNER = "owner"
ADMIN = "admin"
PACKER = "packer"

ROLES = [
    (OWNER, "Владелец", "полный доступ, меняет пароли и права всем"),
    (ADMIN, "Администратор", "ведёт работу склада и выдаёт доступы, кроме владельцев"),
    (PACKER, "Сборщик", "работает на складе, разделы выдаются галочками"),
]
ROLE_LABELS = {code: label for code, label, _hint in ROLES}

# Ключ раздела -> подпись и адрес. Порядок тот же, что в шапке: галочки должны
# читаться в одном порядке с меню, иначе их сверяют глазами дважды.
SECTIONS = [
    ("pack", "Сборка", "/pack", "рабочее место сканера"),
    ("orders", "Заказы", "/orders", "заказы всех кабинетов: статусы, наклейки, подтверждение и сборка на площадке"),
    ("returns", "Возвраты", "/returns", "выдача возвратов и акты"),
    ("products", "Товары", "/products", "каталог кабинета и наборы"),
    ("reports", "Отчёты", "/reports", "отгрузка по дням и подтверждённые акты"),
    ("logs", "Журнал", "/logs", "все действия сотрудников, входы и IP"),
    ("settings", "Настройки", "/settings", "ключи площадок, сотрудники и доступы"),
]
SECTION_KEYS = [key for key, _l, _u, _h in SECTIONS]
SECTION_LABELS = {key: label for key, label, _u, _h in SECTIONS}

# Раздел, которым панель настраивают. Сборщику он не выдаётся: там ключи
# площадок и выдача прав, а это работа администратора — и роль у такого
# человека должна быть администраторской, а не спрятанной за галочкой.
MANAGER_ONLY = {"settings"}

DEFAULT_SECTIONS = {
    OWNER: list(SECTION_KEYS),
    ADMIN: list(SECTION_KEYS),
    PACKER: ["pack", "orders", "returns"],
}


def is_owner(user: dict | None) -> bool:
    return bool(user) and user.get("role") == OWNER


def is_manager(user: dict | None) -> bool:
    """Может ли человек настраивать панель и выдавать доступы."""
    return bool(user) and user.get("role") in (OWNER, ADMIN)


def role_label(role: str | None) -> str:
    return ROLE_LABELS.get(role or "", role or "—")


def sections_of(user: dict | None) -> list[str]:
    """Разделы, которые человек видит. Порядок — как в шапке.

    У владельца всё и всегда: урезанный владелец означал бы панель, которую
    некому чинить.

    Пустое значение в базе — это «разделы не настраивали», берём умолчание
    роли. Явный пустой список — осознанный выбор «ничего не показывать»,
    и его надо уважать.
    """
    if not user:
        return []
    if is_owner(user):
        return list(SECTION_KEYS)
    raw = user.get("sections")
    if raw is None or str(raw).strip() == "":
        allowed = set(DEFAULT_SECTIONS.get(user.get("role") or PACKER, DEFAULT_SECTIONS[PACKER]))
    else:
        try:
            saved = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            saved = []
        allowed = {str(key) for key in (saved or [])}
    return [key for key in SECTION_KEYS if key in allowed and _allowed_for(user.get("role"), key)]


def _allowed_for(role: str | None, key: str) -> bool:
    """Раздел, который роли нельзя выдать вовсе."""
    return key not in MANAGER_ONLY or role in (OWNER, ADMIN)


def can(user: dict | None, section: str) -> bool:
    return section in sections_of(user)


def clean_sections(role: str, raw) -> list[str]:
    """Отобрать из присланного то, что этой роли действительно можно выдать."""
    wanted = {str(key) for key in (raw or [])}
    return [key for key in SECTION_KEYS if key in wanted and _allowed_for(role, key)]


def dump_sections(role: str, raw) -> str:
    return json.dumps(clean_sections(role, raw), ensure_ascii=False)


def may_manage(actor: dict | None, target: dict | None) -> bool:
    """Может ли actor менять target: роль, разделы, пароль, включение.

    Администратор не трогает владельца — ни роль, ни разделы, ни пароль. Иначе
    «администратор» и «владелец» были бы одним и тем же: первый вторым же
    ключом и открыл бы себе всё.
    """
    if not is_manager(actor) or not target:
        return False
    if is_owner(target):
        return is_owner(actor)
    return True


def may_set_role(actor: dict | None, role: str) -> bool:
    """Владельца назначает только владелец: иначе роль ничего не значит."""
    if role not in ROLE_LABELS:
        return False
    return is_owner(actor) if role == OWNER else is_manager(actor)


def why_not(actor: dict | None, target: dict | None) -> str:
    """Человеческое объяснение отказа — оно уходит прямо в ответ."""
    if not is_manager(actor):
        return "Доступ к сотрудникам есть у владельца и администратора"
    if is_owner(target) and not is_owner(actor):
        return "Права владельца меняет только владелец"
    return "Недостаточно прав"
