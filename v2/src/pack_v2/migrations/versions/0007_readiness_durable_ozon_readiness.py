"""Durable Ozon readiness

Revision ID: 0007_readiness
Revises: 0006_instances
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_readiness"
down_revision = "0006_instances"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "assembly_dispatches",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("proof_session_id", sa.Uuid(), nullable=False),
        sa.Column("context_id", sa.Uuid(), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("basis", sa.String(length=64), nullable=False),
        sa.Column("data_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("send_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ship_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_allowed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','checking','accepted','rejected','uncertain','stale')",
            name=op.f("ck_assembly_dispatches_status"),
        ),
        sa.CheckConstraint(
            "send_count BETWEEN 0 AND 3 AND source_revision >= 0 AND credential_revision >= 0",
            name=op.f("ck_assembly_dispatches_revision"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "shipment_id", "context_id"],
            [
                "pack_v2.instance_contexts.account_id",
                "pack_v2.instance_contexts.shipment_id",
                "pack_v2.instance_contexts.id",
            ],
            name=op.f("fk_assembly_dispatches_account_id_shipment_id_context_id_instance_contexts"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "shipment_id"],
            ["pack_v2.shipments.account_id", "pack_v2.shipments.id"],
            name=op.f("fk_assembly_dispatches_account_id_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["pack_v2.jobs.id"], name=op.f("fk_assembly_dispatches_job_id_jobs")
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "shipment_id"],
            ["pack_v2.shipments.order_id", "pack_v2.shipments.id"],
            name=op.f("fk_assembly_dispatches_order_id_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["proof_session_id", "shipment_id", "order_id"],
            [
                "pack_v2.assembly_sessions.id",
                "pack_v2.assembly_sessions.shipment_id",
                "pack_v2.assembly_sessions.order_id",
            ],
            name=op.f("fk_assembly_dispatches_proof_session_id_shipment_id_order_id_assembly_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assembly_dispatches")),
        sa.UniqueConstraint("job_id", name=op.f("uq_assembly_dispatches_job_id")),
        sa.UniqueConstraint(
            "shipment_id", "source_revision", name=op.f("uq_assembly_dispatches_shipment_id_source_revision")
        ),
        schema="pack_v2",
    )
    _session_status(True)


def downgrade():
    op.execute(
        "UPDATE pack_v2.jobs SET status='failed', lease_token=NULL, lease_until=NULL, finished_at=now(), error='Версия операции удалена' WHERE kind='assembly.ready' AND status IN ('pending','running')"
    )
    op.execute(
        "UPDATE pack_v2.assembly_sessions SET status='verified', revision=revision+1 WHERE status IN ('preparing','ready')"
    )
    _session_status(False)
    op.drop_table("assembly_dispatches", schema="pack_v2")


def _session_status(current):
    statuses = "'opening','scanning','blocked','verified','cancelled','stale','expired'" + (
        ",'preparing','ready'" if current else ""
    )
    active = "status IN ('opening','scanning','blocked'" + (",'preparing'" if current else "") + ")"
    op.drop_constraint(
        op.f("ck_assembly_sessions_status"), "assembly_sessions", schema="pack_v2", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_assembly_sessions_status"),
        "assembly_sessions",
        "status IN (" + statuses + ")",
        schema="pack_v2",
    )
    for suffix, column in [("user", "user_id"), ("shipment", "shipment_id")]:
        op.drop_index("uq_assembly_active_" + suffix, table_name="assembly_sessions", schema="pack_v2")
        op.create_index(
            "uq_assembly_active_" + suffix,
            "assembly_sessions",
            [column],
            unique=True,
            schema="pack_v2",
            postgresql_where=sa.text(active),
        )
