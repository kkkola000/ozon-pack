"""Durable shipment layouts and Ozon split reconciliation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_split"
down_revision = "0007_readiness"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "shipments",
        sa.Column("layout_review", sa.Boolean(), server_default="false", nullable=False),
        schema="pack_v2",
    )
    op.create_index("ix_shipments_parent", "shipments", ["parent_id"], schema="pack_v2")
    op.create_table(
        "packing_layouts",
        sa.Column("shipment_id", sa.Uuid(), sa.ForeignKey("pack_v2.shipments.id"), primary_key=True),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("basis", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("groups", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("revision > 0 AND source_revision >= 0", name=op.f("ck_packing_layouts_revision")),
        schema="pack_v2",
    )
    for name in ("layout", "split_targets", "split_result"):
        op.add_column(
            "assembly_dispatches",
            sa.Column(name, postgresql.JSONB(), server_default="[]", nullable=False),
            schema="pack_v2",
        )
    op.add_column(
        "assembly_dispatches",
        sa.Column("encrypted_evidence", sa.LargeBinary(), nullable=True),
        schema="pack_v2",
    )


def downgrade():
    op.execute(
        "UPDATE pack_v2.jobs SET status='failed',lease_token=NULL,lease_until=NULL,finished_at=now(),error='Версия операции удалена' WHERE kind='assembly.ready' AND status IN ('pending','running')"
    )
    op.execute(
        "UPDATE pack_v2.assembly_sessions SET status='stale',revision=revision+1 WHERE status='preparing'"
    )
    for name in ("layout", "split_targets", "split_result", "encrypted_evidence"):
        op.drop_column("assembly_dispatches", name, schema="pack_v2")
    op.drop_table("packing_layouts", schema="pack_v2")
    op.drop_column("shipments", "layout_review", schema="pack_v2")
    op.drop_index("ix_shipments_parent", table_name="shipments", schema="pack_v2")
