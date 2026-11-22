"""Instance requirements marking assignments and durable submission

Revision ID: 0006_instances
Revises: 0005_assembly
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_instances"
down_revision = "0005_assembly"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "marking_units",
        sa.Column("identity", sa.String(length=64), nullable=False),
        sa.Column("full_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("gtin", sa.String(length=14), nullable=False),
        sa.Column("serial", sa.String(length=20), nullable=False),
        sa.Column("encrypted_code", sa.LargeBinary(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_marking_units")),
        sa.UniqueConstraint("identity", name=op.f("uq_marking_units_identity")),
        schema="pack_v2",
    )
    op.create_table(
        "instance_contexts",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("basis", sa.String(length=64), nullable=False),
        sa.Column("posting_number", sa.String(length=256), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("policy_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "requirements", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("encrypted_snapshot", sa.LargeBinary(), nullable=True),
        sa.Column("signature", sa.String(length=64), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','reading','ready','failed','stale')",
            name=op.f("ck_instance_contexts_status"),
        ),
        sa.CheckConstraint(
            "credential_revision >= 0 AND source_revision >= 0 AND policy_version > 0",
            name=op.f("ck_instance_contexts_revision"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "shipment_id"],
            ["pack_v2.shipments.account_id", "pack_v2.shipments.id"],
            name=op.f("fk_instance_contexts_account_id_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["pack_v2.jobs.id"], name=op.f("fk_instance_contexts_job_id_jobs")
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "shipment_id"],
            ["pack_v2.shipments.order_id", "pack_v2.shipments.id"],
            name=op.f("fk_instance_contexts_order_id_shipment_id_shipments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instance_contexts")),
        sa.UniqueConstraint(
            "account_id", "shipment_id", "id", name=op.f("uq_instance_contexts_account_id_shipment_id_id")
        ),
        sa.UniqueConstraint("job_id", name=op.f("uq_instance_contexts_job_id")),
        schema="pack_v2",
    )
    op.create_index(
        "ix_instance_context_shipment",
        "instance_contexts",
        ["shipment_id", "created_at"],
        unique=False,
        schema="pack_v2",
    )
    op.create_index(
        "uq_instance_discovery_active",
        "instance_contexts",
        ["shipment_id"],
        unique=True,
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('queued','reading')"),
    )
    op.create_table(
        "instance_submissions",
        sa.Column("context_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("encrypted_payload", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="prepared", nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("set_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("update_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column(
            "field_errors", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "field_results", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('prepared','checking','accepted','rejected','uncertain','stale')",
            name=op.f("ck_instance_submissions_status"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "shipment_id", "context_id"],
            [
                "pack_v2.instance_contexts.account_id",
                "pack_v2.instance_contexts.shipment_id",
                "pack_v2.instance_contexts.id",
            ],
            name=op.f("fk_instance_submissions_account_id_shipment_id_context_id_instance_contexts"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["pack_v2.jobs.id"], name=op.f("fk_instance_submissions_job_id_jobs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instance_submissions")),
        sa.UniqueConstraint(
            "context_id", "fingerprint", name=op.f("uq_instance_submissions_context_id_fingerprint")
        ),
        sa.UniqueConstraint("job_id", name=op.f("uq_instance_submissions_job_id")),
        schema="pack_v2",
    )
    op.create_index(
        "ix_instance_submission_context",
        "instance_submissions",
        ["context_id", "created_at"],
        unique=False,
        schema="pack_v2",
    )
    op.create_index(
        "uq_instance_submission_active",
        "instance_submissions",
        ["shipment_id"],
        unique=True,
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('prepared','checking')"),
    )
    op.create_table(
        "marking_assignments",
        sa.Column("mark_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("allocation_id", sa.Uuid(), nullable=False),
        sa.Column("posting_number", sa.String(length=256), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(length=40), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(released_at IS NULL) = (release_reason IS NULL)", name=op.f("ck_marking_assignments_release")
        ),
        sa.CheckConstraint("revision >= 0", name=op.f("ck_marking_assignments_revision")),
        sa.ForeignKeyConstraint(
            ["account_id", "shipment_id"],
            ["pack_v2.shipments.account_id", "pack_v2.shipments.id"],
            name=op.f("fk_marking_assignments_account_id_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["pack_v2.users.id"], name=op.f("fk_marking_assignments_actor_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["mark_id"],
            ["pack_v2.marking_units.id"],
            name=op.f("fk_marking_assignments_mark_id_marking_units"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "shipment_id"],
            ["pack_v2.shipments.order_id", "pack_v2.shipments.id"],
            name=op.f("fk_marking_assignments_order_id_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "unit_id"],
            ["pack_v2.order_units.order_id", "pack_v2.order_units.id"],
            name=op.f("fk_marking_assignments_order_id_unit_id_order_units"),
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id", "unit_id", "allocation_id"],
            [
                "pack_v2.shipment_units.shipment_id",
                "pack_v2.shipment_units.unit_id",
                "pack_v2.shipment_units.id",
            ],
            name=op.f("fk_marking_assignments_shipment_id_unit_id_allocation_id_shipment_units"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_marking_assignments")),
        sa.UniqueConstraint(
            "shipment_id", "unit_id", "id", name=op.f("uq_marking_assignments_shipment_id_unit_id_id")
        ),
        schema="pack_v2",
    )
    op.create_index(
        "ix_marking_assignment_shipment",
        "marking_assignments",
        ["shipment_id", "released_at"],
        unique=False,
        schema="pack_v2",
    )
    op.create_index(
        "uq_marking_current_identity",
        "marking_assignments",
        ["mark_id"],
        unique=True,
        schema="pack_v2",
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "uq_marking_current_unit",
        "marking_assignments",
        ["unit_id"],
        unique=True,
        schema="pack_v2",
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_table(
        "unit_details",
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("allocation_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("exemplar_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("encrypted_values", sa.LargeBinary(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "input_errors", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.CheckConstraint("revision >= 0", name=op.f("ck_unit_details_revision")),
        sa.ForeignKeyConstraint(
            ["account_id", "shipment_id"],
            ["pack_v2.shipments.account_id", "pack_v2.shipments.id"],
            name=op.f("fk_unit_details_account_id_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "shipment_id"],
            ["pack_v2.shipments.order_id", "pack_v2.shipments.id"],
            name=op.f("fk_unit_details_order_id_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "unit_id"],
            ["pack_v2.order_units.order_id", "pack_v2.order_units.id"],
            name=op.f("fk_unit_details_order_id_unit_id_order_units"),
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id", "item_id"],
            ["pack_v2.shipment_items.shipment_id", "pack_v2.shipment_items.id"],
            name=op.f("fk_unit_details_shipment_id_item_id_shipment_items"),
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id", "unit_id", "allocation_id"],
            [
                "pack_v2.shipment_units.shipment_id",
                "pack_v2.shipment_units.unit_id",
                "pack_v2.shipment_units.id",
            ],
            name=op.f("fk_unit_details_shipment_id_unit_id_allocation_id_shipment_units"),
        ),
        sa.PrimaryKeyConstraint("unit_id", name=op.f("pk_unit_details")),
        sa.UniqueConstraint(
            "shipment_id", "exemplar_id", name=op.f("uq_unit_details_shipment_id_exemplar_id")
        ),
        schema="pack_v2",
    )
    op.create_index(
        "ix_unit_details_shipment", "unit_details", ["shipment_id"], unique=False, schema="pack_v2"
    )
    op.create_table(
        "marking_transmissions",
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','accepted','rejected','unknown')",
            name=op.f("ck_marking_transmissions_status"),
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["pack_v2.marking_assignments.id"],
            name=op.f("fk_marking_transmissions_assignment_id_marking_assignments"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["pack_v2.instance_submissions.id"],
            name=op.f("fk_marking_transmissions_batch_id_instance_submissions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_marking_transmissions")),
        sa.UniqueConstraint(
            "assignment_id", "batch_id", name=op.f("uq_marking_transmissions_assignment_id_batch_id")
        ),
        schema="pack_v2",
    )
    op.add_column(
        "assembly_sessions", sa.Column("instance_context_id", sa.Uuid(), nullable=True), schema="pack_v2"
    )
    op.create_foreign_key(
        op.f("fk_assembly_sessions_account_id_shipment_id_instance_context_id_instance_contexts"),
        "assembly_sessions",
        "instance_contexts",
        ["account_id", "shipment_id", "instance_context_id"],
        ["account_id", "shipment_id", "id"],
        source_schema="pack_v2",
        referent_schema="pack_v2",
    )

    # Alpha6 proofs did not check provider requirements. Keep their history but
    # require a fresh session under this policy; never claim them as accepted data.
    op.execute(
        "UPDATE pack_v2.assembly_sessions SET status='stale', revision=revision+1, initial_code=NULL WHERE status IN ('opening','scanning','blocked','verified')"
    )


def downgrade():
    op.execute(
        "UPDATE pack_v2.jobs SET status='failed', lease_token=NULL, lease_until=NULL, finished_at=now(), error='Проверка данных отключена при откате схемы' WHERE kind IN ('instances.discover','instances.submit') AND status IN ('pending','running')"
    )
    op.drop_constraint(
        op.f("fk_assembly_sessions_account_id_shipment_id_instance_context_id_instance_contexts"),
        "assembly_sessions",
        schema="pack_v2",
        type_="foreignkey",
    )
    op.drop_column("assembly_sessions", "instance_context_id", schema="pack_v2")
    op.drop_table("marking_transmissions", schema="pack_v2")
    op.drop_index("ix_unit_details_shipment", table_name="unit_details", schema="pack_v2")
    op.drop_table("unit_details", schema="pack_v2")
    op.drop_index(
        "uq_marking_current_unit",
        table_name="marking_assignments",
        schema="pack_v2",
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.drop_index(
        "uq_marking_current_identity",
        table_name="marking_assignments",
        schema="pack_v2",
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.drop_index("ix_marking_assignment_shipment", table_name="marking_assignments", schema="pack_v2")
    op.drop_table("marking_assignments", schema="pack_v2")
    op.drop_index(
        "uq_instance_submission_active",
        table_name="instance_submissions",
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('prepared','checking')"),
    )
    op.drop_index("ix_instance_submission_context", table_name="instance_submissions", schema="pack_v2")
    op.drop_table("instance_submissions", schema="pack_v2")
    op.drop_index(
        "uq_instance_discovery_active",
        table_name="instance_contexts",
        schema="pack_v2",
        postgresql_where=sa.text("status IN ('queued','reading')"),
    )
    op.drop_index("ix_instance_context_shipment", table_name="instance_contexts", schema="pack_v2")
    op.drop_table("instance_contexts", schema="pack_v2")
    op.drop_table("marking_units", schema="pack_v2")
