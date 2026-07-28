"""voice-agent staff alert destination (alert_sms_to / alert_email_to)

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-20

Adds per-clinic alert destinations to ``clinic_voice_agent_configuration`` so the
submit_ticket endpoint can push a best-effort "a message came in" alert to clinic
staff (closing the gap where after-hours tickets were written to BigQuery but
never surfaced). Both nullable — alerts are skipped when unset, so existing
clinics are unaffected until an operator fills them in. SMS is the V1 channel;
email is a forward-compat hook.

Set ACNA's destination out-of-band once confirmed, e.g.:
  UPDATE clinic_voice_agent_configuration
     SET alert_sms_to = '+1780XXXXXXX'
   WHERE clinic_id = '0b5f0929-31fb-4e21-9dd4-030bd040335d';
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clinic_voice_agent_configuration",
        sa.Column("alert_sms_to", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "clinic_voice_agent_configuration",
        sa.Column("alert_email_to", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("clinic_voice_agent_configuration", "alert_email_to")
    op.drop_column("clinic_voice_agent_configuration", "alert_sms_to")
