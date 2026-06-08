"""drop unused voice-agent script fields

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-04

Drops four columns from ``clinic_voice_agent_script`` that are no longer used
by the prompt assembler:

  - ``services_offered`` — "services offered" is now derived from the clinic's
    live Blueprint appointment types (PMS Data tab + ``list_appointment_types``),
    not a hand-typed field.
  - ``caller_needs`` — the agent is scoped to its enabled protocols; the ticket
    ``intent_category`` is now a free-text best-fit label, so the per-clinic
    caller-need category list is obsolete.
  - ``opening_overrides`` — Stage 1 greeting style is covered by the hardcoded
    stage + persona; the free-text style field is removed.
  - ``new_patient_intake_prompt`` — Qualifying Questions
    (``clinic_voice_agent_qualifying_question``) is the single governor of
    new-patient inquiries.

DATA LOSS: this permanently removes any content in those four columns. The
downgrade re-adds the columns as nullable TEXT but cannot restore the data.
Snapshot the table before applying if the content matters.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DROPPED = (
    "services_offered",
    "caller_needs",
    "opening_overrides",
    "new_patient_intake_prompt",
)


def upgrade() -> None:
    for col in _DROPPED:
        op.drop_column("clinic_voice_agent_script", col)


def downgrade() -> None:
    # Re-adds the columns (nullable TEXT); does NOT restore dropped data.
    for col in _DROPPED:
        op.add_column(
            "clinic_voice_agent_script",
            sa.Column(col, sa.Text, nullable=True),
        )
