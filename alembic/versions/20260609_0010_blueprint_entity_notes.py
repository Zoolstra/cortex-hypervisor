"""clinic_blueprint_entity_note — admin notes on Blueprint appointment types / providers

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-09

Appointment types and providers live in Blueprint (fetched live from
clinicConfiguration), so we can't add columns to them. This table lets admins
attach a free-text note to one — keyed by the Blueprint entity id — which the
voice-agent factory merges into the system prompt's Clinic Reference section.

One row per (clinic, entity_kind, entity_id); empty notes are deleted rather
than stored. ``entity_kind`` is 'appointment_type' or 'provider'.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE_OPTS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def upgrade() -> None:
    op.create_table(
        "clinic_blueprint_entity_note",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "clinic_id", sa.CHAR(36),
            sa.ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("entity_kind", sa.String(32), nullable=False),  # appointment_type | provider
        sa.Column("entity_id", sa.Integer, nullable=False),
        sa.Column("note", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime, nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime, nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "clinic_id", "entity_kind", "entity_id",
            name="uq_clinic_entity_note",
        ),
        **_TABLE_OPTS,
    )


def downgrade() -> None:
    op.drop_table("clinic_blueprint_entity_note")
