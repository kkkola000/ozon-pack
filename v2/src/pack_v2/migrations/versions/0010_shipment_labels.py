"""Durable acquisition and one encrypted original label per shipment/format."""

import sqlalchemy as sa
from alembic import op

revision = "0010_labels"
down_revision = "0009_marking"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shipment_labels",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("size", sa.String(8), nullable=False),
        sa.Column("basis", sa.String(64), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("pack_v2.jobs.id"), unique=True),
        sa.Column("task_id", sa.String(32)),
        sa.Column("encrypted_pdf", sa.LargeBinary()),
        sa.Column("checksum", sa.String(64)),
        sa.Column("byte_count", sa.Integer()),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String(500)),
        sa.ForeignKeyConstraint(
            ["account_id", "shipment_id"], ["pack_v2.shipments.account_id", "pack_v2.shipments.id"]
        ),
        sa.UniqueConstraint("shipment_id", "size"),
        sa.CheckConstraint("size IN ('small','big')", name=op.f("ck_shipment_labels_size")),
        sa.CheckConstraint(
            "status IN ('waiting_ready','queued','processing','ready','failed','stale')",
            name=op.f("ck_shipment_labels_status"),
        ),
        sa.CheckConstraint(
            "generation > 0 AND credential_revision > 0", name=op.f("ck_shipment_labels_revision")
        ),
        sa.CheckConstraint(
            "(encrypted_pdf IS NULL) = (checksum IS NULL) AND (encrypted_pdf IS NULL) = (byte_count IS NULL)",
            name=op.f("ck_shipment_labels_file"),
        ),
        sa.CheckConstraint(
            "status <> 'ready' OR (encrypted_pdf IS NOT NULL AND received_at IS NOT NULL)",
            name=op.f("ck_shipment_labels_ready_file"),
        ),
        sa.CheckConstraint(
            "byte_count IS NULL OR byte_count BETWEEN 1 AND 8388608",
            name=op.f("ck_shipment_labels_byte_count"),
        ),
        schema="pack_v2",
    )


def downgrade():
    op.execute(
        "UPDATE pack_v2.jobs SET status='failed', lease_token=NULL, lease_until=NULL, finished_at=now(), error='Версия загрузки стикеров удалена' WHERE kind='documents.label' AND status IN ('pending','running')"
    )
    op.drop_table("shipment_labels", schema="pack_v2")
