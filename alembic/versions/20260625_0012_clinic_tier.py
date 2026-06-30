"""clinic tier (none/bridge/growth)

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-25

Adds a service ``tier`` to ``clinics``. The Intelligence Overview uses it to pick
which System-Performance KPI to emphasise: Bridge-tier clinics lead with
"revenue per clinic hour", Growth-tier clinics with "cost per patient contact".
Default ``none`` so existing clinics are unaffected until classified.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clinics",
        sa.Column(
            "tier",
            sa.Enum("none", "bridge", "growth", name="tier_enum"),
            nullable=False,
            server_default="none",
        ),
    )


def downgrade() -> None:
    op.drop_column("clinics", "tier")
