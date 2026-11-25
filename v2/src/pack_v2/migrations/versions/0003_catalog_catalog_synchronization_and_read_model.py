"""Catalog synchronization and read model

Revision ID: 0003_catalog
Revises: 0002_accounts
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_catalog"
down_revision = "0002_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "catalog_syncs",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pages", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','running','retrying','succeeded','failed','stale')",
            name=op.f("ck_catalog_syncs_status"),
        ),
        sa.CheckConstraint(
            "pages >= 0 AND items >= 0 AND credential_revision >= 0", name=op.f("ck_catalog_syncs_progress")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["pack_v2.marketplace_accounts.id"],
            name=op.f("fk_catalog_syncs_account_id_marketplace_accounts"),
        ),
        sa.ForeignKeyConstraint(["job_id"], ["pack_v2.jobs.id"], name=op.f("fk_catalog_syncs_job_id_jobs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_syncs")),
        sa.UniqueConstraint("account_id", "id", name=op.f("uq_catalog_syncs_account_id_id")),
        sa.UniqueConstraint("job_id", name=op.f("uq_catalog_syncs_job_id")),
        schema="pack_v2",
    )
    op.create_index(
        "ix_catalog_syncs_history",
        "catalog_syncs",
        ["account_id", "created_at"],
        unique=False,
        schema="pack_v2",
    )
    op.create_index(
        "uq_catalog_syncs_active",
        "catalog_syncs",
        ["account_id"],
        unique=True,
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('queued','running','retrying')"),
    )
    op.add_column("offers", sa.Column("article", sa.String(length=256), nullable=True), schema="pack_v2")
    op.add_column("offers", sa.Column("image_url", sa.String(length=4096), nullable=True), schema="pack_v2")
    op.add_column(
        "offers",
        sa.Column("source_present", sa.Boolean(), server_default="true", nullable=False),
        schema="pack_v2",
    )
    op.add_column(
        "offers", sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True), schema="pack_v2"
    )
    op.add_column(
        "offers", sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True), schema="pack_v2"
    )
    op.add_column("offers", sa.Column("last_seen_run_id", sa.Uuid(), nullable=True), schema="pack_v2")
    op.add_column(
        "offers", sa.Column("revision", sa.Integer(), server_default="0", nullable=False), schema="pack_v2"
    )
    op.alter_column(
        "offers",
        "archived",
        existing_type=sa.BOOLEAN(),
        nullable=True,
        server_default=None,
        existing_server_default=sa.text("false"),
        schema="pack_v2",
    )
    op.create_foreign_key(
        op.f("fk_offers_account_id_last_seen_run_id_catalog_syncs"),
        "offers",
        "catalog_syncs",
        ["account_id", "last_seen_run_id"],
        ["account_id", "id"],
        source_schema="pack_v2",
        referent_schema="pack_v2",
    )

    for name, table, columns in (
        ("ix_offers_listing", "offers", ["created_at", "id"]),
        ("ix_offers_account_listing", "offers", ["account_id", "created_at", "id"]),
        ("ix_offer_refs_offer", "offer_refs", ["offer_id"]),
        ("ix_offer_refs_value", "offer_refs", ["value"]),
        ("ix_offer_codes_offer", "offer_codes", ["offer_id"]),
    ):
        op.create_index(name, table, columns, schema="pack_v2")


def downgrade():
    for name, table in (
        ("ix_offers_listing", "offers"),
        ("ix_offers_account_listing", "offers"),
        ("ix_offer_refs_offer", "offer_refs"),
        ("ix_offer_refs_value", "offer_refs"),
        ("ix_offer_codes_offer", "offer_codes"),
    ):
        op.drop_index(name, table_name=table, schema="pack_v2")
    op.drop_constraint(
        op.f("fk_offers_account_id_last_seen_run_id_catalog_syncs"),
        "offers",
        schema="pack_v2",
        type_="foreignkey",
    )
    op.execute("UPDATE pack_v2.offers SET archived=false WHERE archived IS NULL")
    op.alter_column(
        "offers",
        "archived",
        existing_type=sa.BOOLEAN(),
        server_default=sa.text("false"),
        nullable=False,
        existing_server_default=sa.text("false"),
        schema="pack_v2",
    )
    op.drop_column("offers", "revision", schema="pack_v2")
    op.drop_column("offers", "last_seen_run_id", schema="pack_v2")
    op.drop_column("offers", "synced_at", schema="pack_v2")
    op.drop_column("offers", "observed_at", schema="pack_v2")
    op.drop_column("offers", "source_present", schema="pack_v2")
    op.drop_column("offers", "image_url", schema="pack_v2")
    op.drop_column("offers", "article", schema="pack_v2")
    op.drop_index(
        "uq_catalog_syncs_active",
        table_name="catalog_syncs",
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('queued','running','retrying')"),
    )
    op.drop_index("ix_catalog_syncs_history", table_name="catalog_syncs", schema="pack_v2")
    op.drop_table("catalog_syncs", schema="pack_v2")
