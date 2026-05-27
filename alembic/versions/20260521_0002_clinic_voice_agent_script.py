"""clinic_voice_agent_script — per-clinic scope-of-practice script

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-21

Adds a 1:1 ``clinic_voice_agent_script`` table holding the editable text
content the voice agent uses to bound its conversations:
  - scope_of_practice   — clinical bounds (what this practice handles)
  - services_offered    — list of services / appointment types
  - services_not_offered — services to redirect callers away from
  - caller_needs        — common caller intent categories the agent recognises
  - additional_notes    — free-form practice-specific guidance

All columns are TEXT (nullable). Cascades from clinics like every other
per-clinic config table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002"
down_revision: Union[str, None] = "0001"
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
    op.create_table(
        "clinic_voice_agent_script",
        sa.Column(
            "clinic_id", sa.CHAR(36),
            sa.ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("scope_of_practice",    sa.Text),
        sa.Column("services_offered",     sa.Text),
        sa.Column("services_not_offered", sa.Text),
        sa.Column("caller_needs",         sa.Text),
        sa.Column("additional_notes",     sa.Text),
        *_audit_cols(),
        **_TABLE_OPTS,
    )


def downgrade() -> None:
    op.drop_table("clinic_voice_agent_script")
