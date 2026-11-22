def load_models() -> None:
    # Composition root is the only place that imports all model definitions.
    from pack_v2.infrastructure import models  # noqa: F401
    from pack_v2.integrations import models as integration_models  # noqa: F401
    from pack_v2.modules.assembly import models as assembly_models  # noqa: F401
    from pack_v2.modules.catalog import models as catalog_models  # noqa: F401
    from pack_v2.modules.identity import models as identity_models  # noqa: F401
    from pack_v2.modules.instance_data import models as instance_models  # noqa: F401
    from pack_v2.modules.marking import models as marking_models  # noqa: F401
    from pack_v2.modules.orders import models as order_models  # noqa: F401
