"""voice agent new-patient qualifying questions

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-04

New ``clinic_voice_agent_qualifying_question`` (N per clinic) — an ordered,
per-clinic set of new-patient screening questions asked during Stage 3a
(New Patient Discovery). Each row is one question plus optional
``expected_responses`` guidance. Ordered by ``ordinal`` ASC; inactive rows
hidden. Cascade-deletes from clinics. Mirrors the shape of
``clinic_voice_agent_caller_bucket`` (revision 0003).

Unlike caller buckets there is no seeded default set — screening is
clinic-specific, so an unconfigured clinic simply gets no screening block.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006"
down_revision: Union[str, None] = "0005"
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
        "clinic_voice_agent_qualifying_question",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "clinic_id", sa.CHAR(36),
            sa.ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("ordinal",            sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("question_text",      sa.String(512),  nullable=False),
        sa.Column("expected_responses", sa.Text,         nullable=True),
        sa.Column("active",             sa.Boolean,      nullable=False, server_default=sa.text("1")),
        *_audit_cols(),
        **_TABLE_OPTS,
    )


def downgrade() -> None:
    op.drop_table("clinic_voice_agent_qualifying_question")
