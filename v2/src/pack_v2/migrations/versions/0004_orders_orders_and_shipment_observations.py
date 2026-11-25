"""Orders and shipment observations

Revision ID: 0004_orders
Revises: 0003_catalog
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_orders"
down_revision = "0003_catalog"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "order_syncs",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pages", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("mode IN ('full','working')", name=op.f("ck_order_syncs_mode")),
        sa.CheckConstraint(
            "status IN ('queued','running','retrying','succeeded','failed','stale')",
            name=op.f("ck_order_syncs_status"),
        ),
        sa.CheckConstraint(
            "pages >= 0 AND items >= 0 AND credential_revision >= 0", name=op.f("ck_order_syncs_progress")
        ),
        sa.CheckConstraint("since <= until", name=op.f("ck_order_syncs_period")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["pack_v2.marketplace_accounts.id"],
            name=op.f("fk_order_syncs_account_id_marketplace_accounts"),
        ),
        sa.ForeignKeyConstraint(["job_id"], ["pack_v2.jobs.id"], name=op.f("fk_order_syncs_job_id_jobs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_syncs")),
        sa.UniqueConstraint("account_id", "id", name=op.f("uq_order_syncs_account_id_id")),
        sa.UniqueConstraint("job_id", name=op.f("uq_order_syncs_job_id")),
        schema="pack_v2",
    )
    op.create_index(
        "ix_order_syncs_history", "order_syncs", ["account_id", "created_at"], unique=False, schema="pack_v2"
    )
    op.create_index(
        "uq_order_syncs_active",
        "order_syncs",
        ["account_id"],
        unique=True,
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('queued','running','retrying')"),
    )
    op.create_table(
        "shipment_items",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("source_line_id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("article", sa.String(length=256), nullable=True),
        sa.Column("references", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("offer_id", sa.Uuid(), nullable=True),
        sa.Column("match_state", sa.String(length=20), server_default="unmatched", nullable=False),
        sa.Column(
            "required_fields", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "optional_fields", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("source_present", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(match_state='matched') = (offer_id IS NOT NULL)", name=op.f("ck_shipment_items_match_target")
        ),
        sa.CheckConstraint(
            "match_state IN ('matched','unmatched','conflict')", name=op.f("ck_shipment_items_match_state")
        ),
        sa.CheckConstraint("price IS NULL OR price >= 0", name=op.f("ck_shipment_items_price")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_shipment_items_quantity")),
        sa.ForeignKeyConstraint(
            ["account_id", "offer_id"],
            ["pack_v2.offers.account_id", "pack_v2.offers.id"],
            name=op.f("fk_shipment_items_account_id_offer_id_offers"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "shipment_id"],
            ["pack_v2.shipments.account_id", "pack_v2.shipments.id"],
            name=op.f("fk_shipment_items_account_id_shipment_id_shipments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipment_items")),
        sa.UniqueConstraint(
            "shipment_id", "source_line_id", name=op.f("uq_shipment_items_shipment_id_source_line_id")
        ),
        schema="pack_v2",
    )
    op.create_index("ix_shipment_items_offer", "shipment_items", ["offer_id"], unique=False, schema="pack_v2")
    op.create_index(
        "ix_shipment_items_references",
        "shipment_items",
        ["references"],
        unique=False,
        schema="pack_v2",
        postgresql_using="gin",
    )
    op.create_index(
        "ix_shipment_items_shipment", "shipment_items", ["shipment_id"], unique=False, schema="pack_v2"
    )
    op.create_table(
        "shipment_relations",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("related_number", sa.String(length=256), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "shipment_id"],
            ["pack_v2.shipments.account_id", "pack_v2.shipments.id"],
            name=op.f("fk_shipment_relations_account_id_shipment_id_shipments"),
        ),
        sa.PrimaryKeyConstraint(
            "account_id", "shipment_id", "related_number", name=op.f("pk_shipment_relations")
        ),
        schema="pack_v2",
    )
    op.create_index(
        "ix_shipment_relations_shipment",
        "shipment_relations",
        ["shipment_id"],
        unique=False,
        schema="pack_v2",
    )
    op.create_index("ix_order_refs_order", "order_refs", ["order_id"], unique=False, schema="pack_v2")
    op.add_column(
        "shipment_refs",
        sa.Column("origins", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        schema="pack_v2",
    )
    op.create_index(
        "ix_shipment_refs_shipment", "shipment_refs", ["shipment_id"], unique=False, schema="pack_v2"
    )
    op.add_column(
        "shipments", sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True), schema="pack_v2"
    )
    op.add_column(
        "shipments", sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True), schema="pack_v2"
    )
    op.add_column(
        "shipments",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        schema="pack_v2",
    )
    op.add_column(
        "shipments", sa.Column("warehouse_id", sa.String(length=256), nullable=True), schema="pack_v2"
    )
    op.add_column(
        "shipments", sa.Column("warehouse_name", sa.String(length=500), nullable=True), schema="pack_v2"
    )
    op.add_column(
        "shipments", sa.Column("parent_source_number", sa.String(length=256), nullable=True), schema="pack_v2"
    )
    op.add_column("shipments", sa.Column("package_count_hint", sa.Integer(), nullable=True), schema="pack_v2")
    op.add_column(
        "shipments",
        sa.Column("needs_review", sa.Boolean(), server_default="false", nullable=False),
        schema="pack_v2",
    )
    op.add_column(
        "shipments",
        sa.Column("transition_failed", sa.Boolean(), server_default="false", nullable=False),
        schema="pack_v2",
    )
    op.add_column(
        "shipments",
        sa.Column("requirements_complete", sa.Boolean(), server_default="false", nullable=False),
        schema="pack_v2",
    )
    op.add_column("shipments", sa.Column("last_seen_run_id", sa.Uuid(), nullable=True), schema="pack_v2")
    op.create_index(
        "ix_shipments_account_listing",
        "shipments",
        ["account_id", "processing_started_at", "id"],
        unique=False,
        schema="pack_v2",
    )
    op.create_index(
        "ix_shipments_listing", "shipments", ["processing_started_at", "id"], unique=False, schema="pack_v2"
    )
    op.create_foreign_key(
        op.f("fk_shipments_account_id_last_seen_run_id_order_syncs"),
        "shipments",
        "order_syncs",
        ["account_id", "last_seen_run_id"],
        ["account_id", "id"],
        source_schema="pack_v2",
        referent_schema="pack_v2",
    )
    op.create_check_constraint(
        op.f("ck_shipments_parent_not_self"),
        "shipments",
        "parent_id IS NULL OR parent_id <> id",
        schema="pack_v2",
    )
    op.create_check_constraint(
        op.f("ck_shipments_package_count"),
        "shipments",
        "package_count_hint IS NULL OR package_count_hint > 0",
        schema="pack_v2",
    )


def downgrade():
    op.drop_constraint(op.f("ck_shipments_parent_not_self"), "shipments", type_="check", schema="pack_v2")
    op.drop_constraint(op.f("ck_shipments_package_count"), "shipments", type_="check", schema="pack_v2")
    op.drop_constraint(
        op.f("fk_shipments_account_id_last_seen_run_id_order_syncs"),
        "shipments",
        schema="pack_v2",
        type_="foreignkey",
    )
    op.drop_index("ix_shipments_listing", table_name="shipments", schema="pack_v2")
    op.drop_index("ix_shipments_account_listing", table_name="shipments", schema="pack_v2")
    op.drop_column("shipments", "last_seen_run_id", schema="pack_v2")
    op.drop_column("shipments", "requirements_complete", schema="pack_v2")
    op.drop_column("shipments", "transition_failed", schema="pack_v2")
    op.drop_column("shipments", "needs_review", schema="pack_v2")
    op.drop_column("shipments", "package_count_hint", schema="pack_v2")
    op.drop_column("shipments", "parent_source_number", schema="pack_v2")
    op.drop_column("shipments", "warehouse_name", schema="pack_v2")
    op.drop_column("shipments", "warehouse_id", schema="pack_v2")
    op.drop_column("shipments", "processing_started_at", schema="pack_v2")
    op.drop_column("shipments", "checked_at", schema="pack_v2")
    op.drop_column("shipments", "observed_at", schema="pack_v2")
    op.drop_index("ix_shipment_refs_shipment", table_name="shipment_refs", schema="pack_v2")
    op.drop_column("shipment_refs", "origins", schema="pack_v2")
    op.drop_index("ix_order_refs_order", table_name="order_refs", schema="pack_v2")
    op.drop_index("ix_shipment_relations_shipment", table_name="shipment_relations", schema="pack_v2")
    op.drop_table("shipment_relations", schema="pack_v2")
    op.drop_index("ix_shipment_items_shipment", table_name="shipment_items", schema="pack_v2")
    op.drop_index(
        "ix_shipment_items_references", table_name="shipment_items", schema="pack_v2", postgresql_using="gin"
    )
    op.drop_index("ix_shipment_items_offer", table_name="shipment_items", schema="pack_v2")
    op.drop_table("shipment_items", schema="pack_v2")
    op.drop_index(
        "uq_order_syncs_active",
        table_name="order_syncs",
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('queued','running','retrying')"),
    )
    op.drop_index("ix_order_syncs_history", table_name="order_syncs", schema="pack_v2")
    op.drop_table("order_syncs", schema="pack_v2")
