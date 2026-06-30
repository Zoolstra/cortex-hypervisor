"""instance multi_location_group capability flag

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-30

Adds a ``multi_location_group`` boolean to ``instances``. It unlocks the
multi-location "Group Intelligence" analytics section (a leaderboard + roll-up
across all of an instance's clinics). Default ``0`` so every existing instance
is unaffected; flip it on per instance (currently Virsono) with a one-off
``UPDATE instances SET multi_location_group = 1 WHERE instance_id = '…'``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column(
            "multi_location_group",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("instances", "multi_location_group")
