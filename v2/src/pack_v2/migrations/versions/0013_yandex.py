"""Marketplace declaration is independent of native order instance requirements."""

import sqlalchemy as sa
from alembic import op

revision = "0013_yandex"
down_revision = "0012_printing"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("offers", sa.Column("marking_declared", sa.Boolean(), nullable=True), schema="pack_v2")


def downgrade():
    op.drop_column("offers", "marking_declared", schema="pack_v2")
