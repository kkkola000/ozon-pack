"""Yandex originals use their own printer profile and the common print dispatcher."""

import sqlalchemy as sa
from alembic import op

revision = "0014_yandex_fulfillment"
down_revision = "0013_yandex"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text(
            "UPDATE pack_v2.shipment_refs SET authoritative = false WHERE kind = 'campaign_id' AND account_id IN (SELECT id FROM pack_v2.marketplace_accounts WHERE marketplace = 'yandex')"
        )
    )
    op.add_column("print_jobs", sa.Column("page_index", sa.Integer(), nullable=True), schema="pack_v2")
    op.drop_constraint("ck_print_jobs_kind", "print_jobs", schema="pack_v2")
    op.create_check_constraint(
        "kind", "print_jobs", "kind IN ('ozon:label','yandex:label','marking:duplicate')", schema="pack_v2"
    )
    op.execute(
        sa.text("""UPDATE pack_v2.print_settings SET profiles = profiles ||
        '[{"kind":"yandex:label","size":"75x120","printer":"","orientation":"","width": 75,"height": 120,"gap": 0,"x": 0,"y": 0}]'::jsonb
        WHERE NOT profiles @> '[{"kind":"yandex:label"}]'::jsonb""")
    )


def downgrade():
    op.drop_column("print_jobs", "page_index", schema="pack_v2")
    # Completed dispatch records are audit history; do not silently delete Yandex jobs.
    op.drop_constraint("ck_print_jobs_kind", "print_jobs", schema="pack_v2")
    op.create_check_constraint(
        "kind", "print_jobs", "kind IN ('ozon:label','marking:duplicate')", schema="pack_v2"
    )
    op.execute(
        sa.text("""UPDATE pack_v2.print_settings SET profiles =
        (SELECT coalesce(jsonb_agg(value), '[]'::jsonb) FROM jsonb_array_elements(profiles) WHERE value->>'kind' <> 'yandex:label')""")
    )
