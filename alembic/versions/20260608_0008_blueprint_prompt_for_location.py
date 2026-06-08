"""clinic_blueprint_config.prompt_for_location

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-08

Adds an opt-in ``prompt_for_location`` flag to ``clinic_blueprint_config``.
When TRUE the voice agent asks the caller which location to book into before
searching availability; when FALSE (default) the agent uses the clinic's sole
Blueprint location silently. Most clinics are single-location, so the default
is FALSE.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clinic_blueprint_config",
        sa.Column(
            "prompt_for_location",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("clinic_blueprint_config", "prompt_for_location")
