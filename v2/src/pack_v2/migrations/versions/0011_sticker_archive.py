"""Six-month sticker archive and encrypted, session-bound search criteria."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011_archive"
down_revision = "0010_labels"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "shipment_labels",
        sa.Column("archive_snapshot", JSONB(), server_default="{}", nullable=False),
        schema="pack_v2",
    )
    op.drop_constraint(op.f("ck_shipment_labels_status"), "shipment_labels", schema="pack_v2", type_="check")
    op.create_check_constraint(
        op.f("ck_shipment_labels_status"),
        "shipment_labels",
        "status IN ('waiting_ready','queued','processing','ready','failed','stale','expired')",
        schema="pack_v2",
    )
    op.create_index("ix_shipment_labels_archive", "shipment_labels", ["received_at", "id"], schema="pack_v2")
    op.create_index(
        "ix_shipment_labels_archive_activity",
        "shipment_labels",
        [sa.text("coalesce(received_at, created_at)"), "id"],
        schema="pack_v2",
    )
    op.create_index(
        "ix_shipment_labels_archive_account",
        "shipment_labels",
        ["account_id", sa.text("coalesce(received_at, created_at)"), "id"],
        schema="pack_v2",
    )
    op.create_index("ix_marking_units_gtin", "marking_units", ["gtin"], schema="pack_v2")
    op.create_table(
        "sticker_searches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("pack_v2.users.id"), nullable=False),
        sa.Column(
            "session_id", sa.Uuid(), sa.ForeignKey("pack_v2.sessions.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("encrypted_filters", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        schema="pack_v2",
    )
    op.create_index(
        op.f("ix_sticker_searches_expires_at"), "sticker_searches", ["expires_at"], schema="pack_v2"
    )
    # Older v2 assignments must remain reachable by an administrator even when
    # no PDF was obtained. No API operation or invented file is created here.
    op.execute("""
        INSERT INTO pack_v2.shipment_labels
            (id,created_at,account_id,shipment_id,size,basis,credential_revision,generation,status)
        SELECT gen_random_uuid(),max(m.created_at),m.account_id,m.shipment_id,
               'small',repeat('0',64),greatest(1,a.credential_revision),1,'waiting_ready'
        FROM pack_v2.marking_assignments m
        JOIN pack_v2.marketplace_accounts a ON a.id=m.account_id
        WHERE NOT EXISTS (SELECT 1 FROM pack_v2.shipment_labels l WHERE l.shipment_id=m.shipment_id)
        GROUP BY m.account_id,m.shipment_id,a.credential_revision
    """)


def downgrade():
    op.execute(
        "UPDATE pack_v2.jobs SET status='failed',lease_token=NULL,lease_until=NULL,finished_at=now() WHERE kind='documents.cleanup' AND status IN ('pending','running')"
    )
    op.drop_table("sticker_searches", schema="pack_v2")
    op.drop_index("ix_marking_units_gtin", table_name="marking_units", schema="pack_v2")
    op.drop_index("ix_shipment_labels_archive_account", table_name="shipment_labels", schema="pack_v2")
    op.drop_index("ix_shipment_labels_archive_activity", table_name="shipment_labels", schema="pack_v2")
    op.drop_index("ix_shipment_labels_archive", table_name="shipment_labels", schema="pack_v2")
    op.drop_constraint(op.f("ck_shipment_labels_status"), "shipment_labels", schema="pack_v2", type_="check")
    op.execute("UPDATE pack_v2.shipment_labels SET status='failed' WHERE status='expired'")
    op.create_check_constraint(
        op.f("ck_shipment_labels_status"),
        "shipment_labels",
        "status IN ('waiting_ready','queued','processing','ready','failed','stale')",
        schema="pack_v2",
    )
    op.drop_column("shipment_labels", "archive_snapshot", schema="pack_v2")
