"""Versioned migrations operate only in the pack_v2 schema."""

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateSchema

from pack_v2.bootstrap.models import load_models
from pack_v2.infrastructure.config import load_settings
from pack_v2.infrastructure.database import SCHEMA, Base

load_models()
settings = context.config.attributes.get("settings") or load_settings()


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name == SCHEMA
    return type_ != "table" or parent_names.get("schema_name") == SCHEMA


options = dict(
    target_metadata=Base.metadata,
    include_schemas=True,
    include_name=include_name,
    version_table_schema=SCHEMA,
    compare_type=True,
)
if context.is_offline_mode():
    context.configure(url=settings.database_url.get_secret_value(), literal_binds=True, **options)
    with context.begin_transaction():
        context.execute(CreateSchema(SCHEMA, if_not_exists=True))
        context.run_migrations()
else:
    engine = create_engine(
        settings.database_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": "-c search_path=public"},
    )
    with engine.connect() as connection:
        connection.execute(CreateSchema(SCHEMA, if_not_exists=True))
        connection.commit()
        context.configure(connection=connection, **options)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
