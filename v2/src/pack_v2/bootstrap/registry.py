from dataclasses import dataclass


@dataclass(frozen=True)
class Module:
    code: str
    title: str
    stage: str


MODULES = (
    Module("identity", "Пользователи и доступ", "foundation"),
    Module("integrations", "Интеграции", "connections"),
    Module("catalog", "Товары", "synchronization"),
    Module("orders", "Заказы", "synchronization"),
    Module("assembly", "Сборка", "instance_capture"),
    Module("instance_data", "Данные экземпляров", "ozon_submission"),
    Module("marking", "Честный знак", "assignments"),
    Module("returns", "Возвраты", "planned"),
    Module("documents", "Документы и стикеры", "planned"),
    Module("printing", "Печать", "planned"),
    Module("reports", "Отчёты", "planned"),
)
