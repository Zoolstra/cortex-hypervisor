"""clinic_protocols (rename of voice_agent_capabilities for the Protocol migration)

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-27

Step 3 of the Protocol migration (see ``resources/protocols-design.md``).

Adds the new ``clinic_protocols`` table, a 1:1 rename of
``voice_agent_capabilities`` (``capability_id`` → ``protocol_id``), and
backfills it from the old table. The old table is left in place — during
the transition window the hypervisor writes both, reads from the new
table only. Step 6 drops the old table once nothing depends on it.

Schema match notes:
- ``protocol_id`` is VARCHAR(64), matching the old ``capability_id`` length
  and Protocol.id slugs (e.g. ``submit_ticket``).
- ``config`` stays JSON NULL — bare ``{}`` is not enforced as a default
  yet; protocols with non-empty ``config_model`` will get validation when
  the first such protocol lands (step 5).
- ``updated_by`` stays nullable to match the old table; backfill copies
  whatever was there.
- Composite PK (clinic_id, protocol_id), CASCADE on clinic delete — same
  as ``voice_agent_capabilities``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import JSON


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE_OPTS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def upgrade() -> None:
    # ── New table ────────────────────────────────────────────────
    op.create_table(
        "clinic_protocols",
        sa.Column(
            "clinic_id", sa.CHAR(36),
            sa.ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("protocol_id", sa.String(64), primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("config", JSON),
        sa.Column("updated_by", sa.String(255)),
        sa.Column(
            "created_at", sa.DateTime, nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime, nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
        **_TABLE_OPTS,
    )

    # ── Backfill from voice_agent_capabilities ───────────────────
    # Carry created_at/updated_at across so timestamps don't reset.
    # An existing clinic that previously toggled `patient_match` on
    # 2026-05-10 should still show that as the row's `created_at`.
    op.execute(
        sa.text(
            """
            INSERT INTO clinic_protocols
                (clinic_id, protocol_id, enabled, config, updated_by,
                 created_at, updated_at)
            SELECT clinic_id, capability_id, enabled, config, updated_by,
                   created_at, updated_at
            FROM voice_agent_capabilities
            """
        )
    )


def downgrade() -> None:
    op.drop_table("clinic_protocols")
