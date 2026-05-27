"""voice agent persona + caller buckets + script extensions

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-25

Three changes to the voice-agent data model:

1. New ``clinic_voice_agent_persona`` (1:1 with clinics) — agent_name,
   agent_title, voice_id, optional first_message override, ai_model. Lets
   each clinic customise who the agent presents as without re-templating
   the conversation flow.

2. ALTER ``clinic_voice_agent_script`` ADD three TEXT columns:
   ``opening_overrides`` (optional full replacement of the 4-line opening),
   ``new_patient_intake_prompt`` (first question to a new caller), and
   ``existing_patient_intro`` (transition to the existing-patient flow).

3. New ``clinic_voice_agent_caller_bucket`` (N per clinic) — ordered set
   of caller intent categories with example phrases and canned responses.
   Replaces the hardcoded Motivated / Price Shopper / Test-Only buckets
   with per-clinic data. Unseeded clinics fall back to the hardcoded
   defaults in factory.py.

All cascade-delete from clinics. Audit cols on every new/extended table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE_OPTS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def _audit_cols() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime, nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime, nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
    ]


def upgrade() -> None:
    # 1. Persona (1:1)
    op.create_table(
        "clinic_voice_agent_persona",
        sa.Column(
            "clinic_id", sa.CHAR(36),
            sa.ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("agent_name",    sa.String(64),  nullable=False, server_default="Emma"),
        sa.Column("agent_title",   sa.String(128), nullable=False,
                  server_default="virtual hearing assistant"),
        sa.Column("voice_id",      sa.String(64),  nullable=False, server_default="Emma"),
        sa.Column("first_message", sa.Text,        nullable=True),
        sa.Column("ai_model",      sa.String(64),  nullable=False, server_default="gpt-4o"),
        *_audit_cols(),
        **_TABLE_OPTS,
    )

    # 2. Script extensions
    op.add_column(
        "clinic_voice_agent_script",
        sa.Column("opening_overrides", sa.Text, nullable=True),
    )
    op.add_column(
        "clinic_voice_agent_script",
        sa.Column("new_patient_intake_prompt", sa.Text, nullable=True),
    )
    op.add_column(
        "clinic_voice_agent_script",
        sa.Column("existing_patient_intro", sa.Text, nullable=True),
    )

    # 3. Caller bucket (N per clinic)
    op.create_table(
        "clinic_voice_agent_caller_bucket",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "clinic_id", sa.CHAR(36),
            sa.ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("ordinal",          sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("label",            sa.String(128),  nullable=False),
        sa.Column("example_phrases",  sa.Text,         nullable=True),
        sa.Column("canned_response",  sa.Text,         nullable=True),
        sa.Column("active",           sa.Boolean,      nullable=False, server_default=sa.text("1")),
        *_audit_cols(),
        **_TABLE_OPTS,
    )


def downgrade() -> None:
    op.drop_table("clinic_voice_agent_caller_bucket")
    op.drop_column("clinic_voice_agent_script", "existing_patient_intro")
    op.drop_column("clinic_voice_agent_script", "new_patient_intake_prompt")
    op.drop_column("clinic_voice_agent_script", "opening_overrides")
    op.drop_table("clinic_voice_agent_persona")
