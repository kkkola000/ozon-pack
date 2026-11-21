from dataclasses import dataclass


@dataclass(frozen=True)
class Module:
    code: str
    title: str
    stage: str


MODULES = (
    Module("identity", "Пользователи и доступ", "foundation"),
    Module("integrations", "Интеграции", "connections"),
    Module("catalog", "Товары", "schema"),
    Module("orders", "Заказы", "schema"),
    Module("assembly", "Сборка", "planned"),
    Module("instance_data", "Данные экземпляров", "planned"),
    Module("marking", "Честный знак", "planned"),
    Module("returns", "Возвраты", "planned"),
    Module("documents", "Документы и стикеры", "planned"),
    Module("printing", "Печать", "planned"),
    Module("reports", "Отчёты", "planned"),
)
