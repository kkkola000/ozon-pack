"""Separate current marking from reservations awaiting replacement confirmation."""

import sqlalchemy as sa
from alembic import op

revision = "0009_marking"
down_revision = "0008_split"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "marking_transmissions",
        sa.Column("assignment_revision", sa.Integer(), nullable=False, server_default="0"),
        schema="pack_v2",
    )
    op.execute(
        "UPDATE pack_v2.marking_transmissions AS t SET assignment_revision=a.revision FROM pack_v2.marking_assignments AS a WHERE t.assignment_id=a.id"
    )
    op.add_column(
        "marking_assignments", sa.Column("retired_at", sa.DateTime(timezone=True)), schema="pack_v2"
    )
    op.drop_index("uq_marking_current_unit", table_name="marking_assignments", schema="pack_v2")
    op.create_index(
        "uq_marking_current_unit",
        "marking_assignments",
        ["unit_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL AND retired_at IS NULL"),
        schema="pack_v2",
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM pack_v2.marking_assignments WHERE retired_at IS NOT NULL AND released_at IS NULL)"
        )
    ):
        raise RuntimeError("Cannot downgrade while marking replacements await confirmation")
    op.drop_column("marking_transmissions", "assignment_revision", schema="pack_v2")
    op.drop_index("uq_marking_current_unit", table_name="marking_assignments", schema="pack_v2")
    op.create_index(
        "uq_marking_current_unit",
        "marking_assignments",
        ["unit_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
        schema="pack_v2",
    )
    op.drop_column("marking_assignments", "retired_at", schema="pack_v2")
