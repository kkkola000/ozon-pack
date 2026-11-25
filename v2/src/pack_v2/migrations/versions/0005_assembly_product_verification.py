"""Assembly product verification and targeted refresh

Revision ID: 0005_assembly
Revises: 0004_orders
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_assembly"
down_revision = "0004_orders"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "order_lines",
        sa.Column("quantity_source", sa.String(length=16), server_default="manual", nullable=False),
        schema="pack_v2",
    )
    op.add_column("order_syncs", sa.Column("target_shipment_id", sa.Uuid(), nullable=True), schema="pack_v2")
    op.add_column(
        "order_syncs", sa.Column("expected_shipment_revision", sa.Integer(), nullable=True), schema="pack_v2"
    )
    op.create_index(
        "uq_order_syncs_point",
        "order_syncs",
        ["account_id", "target_shipment_id"],
        unique=True,
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('queued','running','retrying') AND mode='point'"),
    )
    op.create_unique_constraint(
        op.f("uq_shipment_items_shipment_id_id"), "shipment_items", ["shipment_id", "id"], schema="pack_v2"
    )
    op.create_unique_constraint(
        op.f("uq_shipment_units_shipment_id_unit_id_id"),
        "shipment_units",
        ["shipment_id", "unit_id", "id"],
        schema="pack_v2",
    )
    op.add_column(
        "shipments",
        sa.Column("assembly_revision", sa.Integer(), server_default="0", nullable=False),
        schema="pack_v2",
    )
    op.drop_constraint(op.f("ck_order_syncs_mode"), "order_syncs", schema="pack_v2", type_="check")
    op.create_check_constraint(
        op.f("ck_order_syncs_mode"), "order_syncs", "mode IN ('full','working','point')", schema="pack_v2"
    )
    op.create_check_constraint(
        op.f("ck_order_syncs_target"),
        "order_syncs",
        "(mode='point') = (target_shipment_id IS NOT NULL) AND (mode='point') = (expected_shipment_revision IS NOT NULL)",
        schema="pack_v2",
    )
    op.create_check_constraint(
        op.f("ck_order_syncs_target_revision"),
        "order_syncs",
        "expected_shipment_revision IS NULL OR expected_shipment_revision >= 0",
        schema="pack_v2",
    )
    op.create_check_constraint(
        op.f("ck_order_lines_quantity_source"),
        "order_lines",
        "quantity_source IN ('manual','allocated')",
        schema="pack_v2",
    )
    op.drop_index("uq_order_syncs_active", table_name="order_syncs", schema="pack_v2")
    op.create_index(
        "uq_order_syncs_active",
        "order_syncs",
        ["account_id"],
        unique=True,
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('queued','running','retrying') AND mode <> 'point'"),
    )
    op.create_table(
        "assembly_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("basis", sa.String(length=64), nullable=True),
        sa.Column("posting_number", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="opening", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_run_id", sa.Uuid(), nullable=False),
        sa.Column("last_unit_id", sa.Uuid(), nullable=True),
        sa.Column("initial_code", sa.LargeBinary(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('opening','scanning','blocked','verified','cancelled','stale','expired')",
            name=op.f("ck_assembly_sessions_status"),
        ),
        sa.CheckConstraint(
            "revision >= 0 AND source_revision >= 0 AND credential_revision >= 0",
            name=op.f("ck_assembly_sessions_revision"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "refresh_run_id"],
            ["pack_v2.order_syncs.account_id", "pack_v2.order_syncs.id"],
            name=op.f("fk_assembly_sessions_account_id_refresh_run_id_order_syncs"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "shipment_id"],
            ["pack_v2.shipments.account_id", "pack_v2.shipments.id"],
            name=op.f("fk_assembly_sessions_account_id_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "last_unit_id"],
            ["pack_v2.order_units.order_id", "pack_v2.order_units.id"],
            name=op.f("fk_assembly_sessions_order_id_last_unit_id_order_units"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "shipment_id"],
            ["pack_v2.shipments.order_id", "pack_v2.shipments.id"],
            name=op.f("fk_assembly_sessions_order_id_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["pack_v2.users.id"], name=op.f("fk_assembly_sessions_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assembly_sessions")),
        sa.UniqueConstraint(
            "id", "shipment_id", "order_id", name=op.f("uq_assembly_sessions_id_shipment_id_order_id")
        ),
        sa.UniqueConstraint("user_id", "id", name=op.f("uq_assembly_sessions_user_id_id")),
        schema="pack_v2",
    )
    op.create_index(
        "ix_assembly_shipment_history",
        "assembly_sessions",
        ["shipment_id", "created_at"],
        unique=False,
        schema="pack_v2",
    )
    op.create_index(
        "ix_assembly_user_history",
        "assembly_sessions",
        ["user_id", "created_at"],
        unique=False,
        schema="pack_v2",
    )
    op.create_index(
        "uq_assembly_active_shipment",
        "assembly_sessions",
        ["shipment_id"],
        unique=True,
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('opening','scanning','blocked')"),
    )
    op.create_index(
        "uq_assembly_active_user",
        "assembly_sessions",
        ["user_id"],
        unique=True,
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('opening','scanning','blocked')"),
    )
    op.create_index(
        "uq_assembly_verified_basis",
        "assembly_sessions",
        ["shipment_id", "source_revision"],
        unique=True,
        schema="pack_v2",
        postgresql_where=sa.text("status='verified'"),
    )
    op.create_table(
        "assembly_commands",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "session_id"],
            ["pack_v2.assembly_sessions.user_id", "pack_v2.assembly_sessions.id"],
            name=op.f("fk_assembly_commands_user_id_session_id_assembly_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["pack_v2.users.id"], name=op.f("fk_assembly_commands_user_id_users")
        ),
        sa.PrimaryKeyConstraint("user_id", "command_id", name=op.f("pk_assembly_commands")),
        schema="pack_v2",
    )
    op.create_table(
        "assembly_lines",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("article", sa.String(length=256), nullable=True),
        sa.Column("source_line_id", sa.String(length=200), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("references", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("marking_hint", sa.Boolean(), server_default="false", nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_assembly_lines_quantity")),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["pack_v2.assembly_sessions.id"],
            name=op.f("fk_assembly_lines_session_id_assembly_sessions"),
        ),
        sa.PrimaryKeyConstraint("session_id", "item_id", name=op.f("pk_assembly_lines")),
        schema="pack_v2",
    )
    op.create_table(
        "assembly_units",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("allocation_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("ordinal > 0", name=op.f("ck_assembly_units_ordinal")),
        sa.ForeignKeyConstraint(
            ["account_id", "shipment_id"],
            ["pack_v2.shipments.account_id", "pack_v2.shipments.id"],
            name=op.f("fk_assembly_units_account_id_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "unit_id"],
            ["pack_v2.order_units.order_id", "pack_v2.order_units.id"],
            name=op.f("fk_assembly_units_order_id_unit_id_order_units"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "item_id"],
            ["pack_v2.assembly_lines.session_id", "pack_v2.assembly_lines.item_id"],
            name=op.f("fk_assembly_units_session_id_item_id_assembly_lines"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "shipment_id", "order_id"],
            [
                "pack_v2.assembly_sessions.id",
                "pack_v2.assembly_sessions.shipment_id",
                "pack_v2.assembly_sessions.order_id",
            ],
            name=op.f("fk_assembly_units_session_id_shipment_id_order_id_assembly_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id", "item_id"],
            ["pack_v2.shipment_items.shipment_id", "pack_v2.shipment_items.id"],
            name=op.f("fk_assembly_units_shipment_id_item_id_shipment_items"),
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id", "unit_id", "allocation_id"],
            [
                "pack_v2.shipment_units.shipment_id",
                "pack_v2.shipment_units.unit_id",
                "pack_v2.shipment_units.id",
            ],
            name=op.f("fk_assembly_units_shipment_id_unit_id_allocation_id_shipment_units"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assembly_units")),
        sa.UniqueConstraint(
            "session_id", "item_id", "ordinal", name=op.f("uq_assembly_units_session_id_item_id_ordinal")
        ),
        sa.UniqueConstraint("session_id", "unit_id", name=op.f("uq_assembly_units_session_id_unit_id")),
        schema="pack_v2",
    )
    op.create_index(
        "ix_assembly_units_session", "assembly_units", ["session_id"], unique=False, schema="pack_v2"
    )


def downgrade():
    op.drop_index("ix_assembly_units_session", table_name="assembly_units", schema="pack_v2")
    op.drop_table("assembly_units", schema="pack_v2")
    op.drop_table("assembly_lines", schema="pack_v2")
    op.drop_table("assembly_commands", schema="pack_v2")
    op.drop_index(
        "uq_assembly_verified_basis",
        table_name="assembly_sessions",
        schema="pack_v2",
        postgresql_where=sa.text("status='verified'"),
    )
    op.drop_index(
        "uq_assembly_active_user",
        table_name="assembly_sessions",
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('opening','scanning','blocked')"),
    )
    op.drop_index(
        "uq_assembly_active_shipment",
        table_name="assembly_sessions",
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('opening','scanning','blocked')"),
    )
    op.drop_index("ix_assembly_user_history", table_name="assembly_sessions", schema="pack_v2")
    op.drop_index("ix_assembly_shipment_history", table_name="assembly_sessions", schema="pack_v2")
    op.drop_table("assembly_sessions", schema="pack_v2")
    # Point jobs are not understood by alpha5. Fence them before removing their runs.
    op.execute(
        "UPDATE pack_v2.jobs SET status='failed', lease_token=NULL, lease_until=NULL, finished_at=now(), error='Проверка отключена при откате схемы' WHERE id IN (SELECT job_id FROM pack_v2.order_syncs WHERE mode='point') AND status IN ('pending','running')"
    )
    op.execute(
        "UPDATE pack_v2.shipments SET last_seen_run_id=NULL WHERE last_seen_run_id IN (SELECT id FROM pack_v2.order_syncs WHERE mode='point')"
    )
    op.execute("DELETE FROM pack_v2.order_syncs WHERE mode='point'")
    op.drop_constraint(op.f("ck_order_syncs_target_revision"), "order_syncs", schema="pack_v2", type_="check")
    op.drop_constraint(op.f("ck_order_syncs_target"), "order_syncs", schema="pack_v2", type_="check")
    op.drop_constraint(op.f("ck_order_syncs_mode"), "order_syncs", schema="pack_v2", type_="check")
    op.create_check_constraint(
        op.f("ck_order_syncs_mode"), "order_syncs", "mode IN ('full','working')", schema="pack_v2"
    )
    op.drop_constraint(op.f("ck_order_lines_quantity_source"), "order_lines", schema="pack_v2", type_="check")
    op.drop_index("uq_order_syncs_active", table_name="order_syncs", schema="pack_v2")
    op.create_index(
        "uq_order_syncs_active",
        "order_syncs",
        ["account_id"],
        unique=True,
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('queued','running','retrying')"),
    )
    op.drop_column("shipments", "assembly_revision", schema="pack_v2")
    op.drop_constraint(
        op.f("uq_shipment_units_shipment_id_unit_id_id"), "shipment_units", schema="pack_v2", type_="unique"
    )
    op.drop_constraint(
        op.f("uq_shipment_items_shipment_id_id"), "shipment_items", schema="pack_v2", type_="unique"
    )
    op.drop_index(
        "uq_order_syncs_point",
        table_name="order_syncs",
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('queued','running','retrying') AND mode='point'"),
    )
    op.drop_column("order_syncs", "expected_shipment_revision", schema="pack_v2")
    op.drop_column("order_syncs", "target_shipment_id", schema="pack_v2")
    op.drop_column("order_lines", "quantity_source", schema="pack_v2")
