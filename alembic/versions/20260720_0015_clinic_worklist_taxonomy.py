"""clinic_worklist_taxonomy: per-clinic reactivation worklist cohorts

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-20

Creates ``clinic_worklist_taxonomy``, a 1:1-per-clinic table holding a JSON
``config`` that defines the reactivation worklist cohorts (tested-not-sold,
fitted-not-sold, no-show, …). Each cohort names the Blueprint appointment
``event_type``s and ``status``es that qualify and whether a "no hearing-aid
sale" exclusion applies; ``ha_item_types`` names the invoice item types that
count as a sale. This replaces the hard-coded constants in
intelligence_report/queries.py and the throwaway scripts/tested_not_sold_*.

Seeds Alto Hearing's verified taxonomy (test types Test-New/reTEST/Annual/
Hearing; fitting Fit/Re-fit; sale item type 'ha'; no-show status 'No show').
Idempotent via INSERT ... ON DUPLICATE KEY UPDATE. Prod DDL runs the same way:
``alembic upgrade head`` against the live Cloud SQL instance (online mode, IAM
auth — see alembic/env.py).
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text


revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ALTO_CLINIC_ID = "1ce69d99-ec12-4d74-9d31-54806201987f"

_ATTENDED = ["Completed", "Arrived"]
_ALTO_CONFIG = json.dumps({
    "ha_item_types": ["ha"],
    "cohorts": [
        {
            "key": "tested_not_sold",
            "label": "Tested — not sold",
            "event_types": ["Test - New", "Test - reTEST", "Test - Annual", "Test - Hearing"],
            "statuses": _ATTENDED,
            "require_no_sale": True,
            "enabled": True,
        },
        {
            "key": "fitted_not_sold",
            "label": "Fitted — not sold",
            "event_types": ["Fit", "Re-fit"],
            "statuses": _ATTENDED,
            "require_no_sale": True,
            "enabled": True,
        },
        {
            "key": "no_show",
            "label": "No-show",
            "event_types": ["Test - New", "Test - reTEST", "Test - Annual", "Test - Hearing",
                            "Fit", "Re-fit"],
            "statuses": ["No show"],
            "require_no_sale": False,
            "enabled": True,
        },
    ],
})


def upgrade() -> None:
    op.create_table(
        "clinic_worklist_taxonomy",
        sa.Column("clinic_id", sa.CHAR(36), nullable=False),
        sa.Column("config", sa.JSON, nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp(),
                  server_onupdate=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.clinic_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("clinic_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    # Seed Alto's verified taxonomy. CAST(:cfg AS JSON) so it lands as a JSON
    # object, not a string scalar. Idempotent.
    op.get_bind().execute(
        text(
            "INSERT INTO clinic_worklist_taxonomy (clinic_id, config, updated_by) "
            "VALUES (:cid, CAST(:cfg AS JSON), :by) "
            "ON DUPLICATE KEY UPDATE config = CAST(:cfg AS JSON)"
        ),
        {"cid": ALTO_CLINIC_ID, "cfg": _ALTO_CONFIG, "by": "migration_0015"},
    )


def downgrade() -> None:
    op.drop_table("clinic_worklist_taxonomy")
