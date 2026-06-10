"""clinic_blueprint_config.user_id

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-09

Adds an optional ``user_id`` to ``clinic_blueprint_config`` — the Blueprint
"user creating the appointment" sent on create/cancel/reschedule. Nullable:
when unset, the adapter falls back to the booking's resolved providerId
(always a valid Blueprint user), so single-provider clinics work without
extra config. Set it explicitly to attribute API writes to a dedicated
service-account user.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clinic_blueprint_config",
        sa.Column("user_id", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("clinic_blueprint_config", "user_id")
