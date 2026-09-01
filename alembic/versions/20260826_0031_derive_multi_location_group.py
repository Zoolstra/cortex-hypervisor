"""multi_location_group is derived from the clinic count, not stored

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-26

``instances.multi_location_group`` (alembic 0013) was a capability flag a
super_admin toggled per client. It restated something the data already said, and
so could disagree with it: an instance that grew from one clinic to four kept
404ing its Group Intelligence rollup until somebody remembered the switch.
Calgary Hearing Aid and Audiology hit exactly that.

The rule is now `clinic_count >= 2`, evaluated in ``api/core/grouping.py`` and
read by all six consumers (four rollup gates, two payload fields). The column is
read by nothing and is no longer settable — it is dropped from ``InstanceUpdate``.

Kept rather than dropped, with a COMMENT saying so, because the stored values are
the only record of which clients had the rollup deliberately switched OFF while
having several locations. If someone needs that distinction back, it wants a
nullable override column, and the old values are the seed for it. Dropping the
column now would throw that away for the sake of tidiness.

Data is deliberately untouched: nothing reads it, so its contents cannot mislead
any code path — only a person reading the table, which the COMMENT addresses.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NOTE = (
    "DEPRECATED by alembic 0031 — Group Intelligence is derived from the clinic "
    "count (api/core/grouping.py, >= 2). Read by nothing and not settable. "
    "Retained as the record of which instances had it explicitly off."
)


def upgrade() -> None:
    # MySQL requires the full column definition on a comment change.
    op.execute(
        "ALTER TABLE instances MODIFY multi_location_group TINYINT(1) NOT NULL "
        f"DEFAULT 0 COMMENT '{_NOTE}'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE instances MODIFY multi_location_group TINYINT(1) NOT NULL "
        "DEFAULT 0 COMMENT ''"
    )
