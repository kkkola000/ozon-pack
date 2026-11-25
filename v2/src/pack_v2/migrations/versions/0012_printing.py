"""Local printer settings and at-most-once dispatch intentions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_printing"
down_revision = "0011_archive"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "print_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("product_barcodes", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("marking_duplicates", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("profiles", JSONB(), server_default="[]", nullable=False),
        sa.Column("encrypted_key", sa.LargeBinary()),
        sa.Column("certificate", sa.Text()),
        sa.CheckConstraint("id = 1 AND revision >= 0", name=op.f("ck_print_settings_singleton")),
        sa.CheckConstraint(
            "(encrypted_key IS NULL) = (certificate IS NULL)", name=op.f("ck_print_settings_signing_pair")
        ),
        schema="pack_v2",
    )
    op.create_table(
        "print_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("pack_v2.users.id"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("automatic_key", sa.String(160), unique=True),
        sa.Column("section", sa.String(16), nullable=False),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("pack_v2.assembly_sessions.id")),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("label_id", sa.Uuid(), sa.ForeignKey("pack_v2.shipment_labels.id")),
        sa.Column("generation", sa.Integer()),
        sa.Column("assignments", JSONB(), server_default="[]", nullable=False),
        sa.Column("product_codes", JSONB(), server_default="[]", nullable=False),
        sa.Column("profile", JSONB(), nullable=False),
        sa.Column("state", sa.String(20), server_default="prepared", nullable=False),
        sa.Column("dispatch_token", sa.Uuid(), nullable=False),
        sa.Column("delivery_token", sa.Uuid()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "request_id"),
        sa.CheckConstraint("section IN ('pack','stickers','test')", name=op.f("ck_print_jobs_section")),
        sa.CheckConstraint("kind IN ('ozon:label','marking:duplicate')", name=op.f("ck_print_jobs_kind")),
        sa.CheckConstraint(
            "state IN ('prepared','dispatching','submitted','browser','uncertain','failed')",
            name=op.f("ck_print_jobs_state"),
        ),
        sa.CheckConstraint("generation IS NULL OR generation > 0", name=op.f("ck_print_jobs_generation")),
        sa.CheckConstraint(
            "(label_id IS NULL) = (generation IS NULL)", name=op.f("ck_print_jobs_label_generation")
        ),
        schema="pack_v2",
    )
    op.create_index("ix_print_jobs_expires_at", "print_jobs", ["expires_at"], schema="pack_v2")


def downgrade():
    op.drop_table("print_jobs", schema="pack_v2")
    op.drop_table("print_settings", schema="pack_v2")
