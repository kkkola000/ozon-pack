"""Encrypted Ozon connections and revision-bound access checks."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_accounts"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("marketplace_accounts", sa.Column("identity_fingerprint", sa.String(64)), schema="pack_v2")
    op.add_column(
        "marketplace_accounts",
        sa.Column("credential_revision", sa.Integer(), nullable=False, server_default="0"),
        schema="pack_v2",
    )
    op.add_column(
        "marketplace_accounts",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        schema="pack_v2",
    )
    op.create_unique_constraint(
        "uq_marketplace_accounts_marketplace_identity_fingerprint",
        "marketplace_accounts",
        ["marketplace", "identity_fingerprint"],
        schema="pack_v2",
    )
    op.create_check_constraint(
        op.f("ck_marketplace_accounts_credential_revision"),
        "marketplace_accounts",
        "credential_revision >= 0",
        schema="pack_v2",
    )
    op.create_table(
        "account_checks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="checking"),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("results", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_checks")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["pack_v2.marketplace_accounts.id"],
            name=op.f("fk_account_checks_account_id_marketplace_accounts"),
        ),
        sa.CheckConstraint(
            "status IN ('checking','passed','partial','failed','unavailable','stale')",
            name=op.f("ck_account_checks_status"),
        ),
        schema="pack_v2",
    )
    op.create_index(
        "ix_account_checks_history", "account_checks", ["account_id", "created_at"], schema="pack_v2"
    )
    op.create_index(
        "uq_account_checks_running",
        "account_checks",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("finished_at IS NULL"),
        schema="pack_v2",
    )


def downgrade():
    op.drop_table("account_checks", schema="pack_v2")
    op.drop_constraint(
        op.f("ck_marketplace_accounts_credential_revision"),
        "marketplace_accounts",
        schema="pack_v2",
        type_="check",
    )
    op.drop_constraint(
        "uq_marketplace_accounts_marketplace_identity_fingerprint",
        "marketplace_accounts",
        schema="pack_v2",
        type_="unique",
    )
    for name in ("updated_at", "credential_revision", "identity_fingerprint"):
        op.drop_column("marketplace_accounts", name, schema="pack_v2")
