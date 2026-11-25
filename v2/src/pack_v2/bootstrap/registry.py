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
    Module("assembly", "Сборка", "ozon_readiness"),
    Module("instance_data", "Данные экземпляров", "ozon_submission"),
    Module("marking", "Честный знак", "assignments"),
    Module("returns", "Возвраты", "planned"),
    Module("documents", "Документы и стикеры", "sticker_archive"),
    Module("printing", "Печать", "local_dispatch"),
    Module("reports", "Отчёты", "planned"),
)
