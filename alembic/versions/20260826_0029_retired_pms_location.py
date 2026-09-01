"""pms_clinic_locations.clinic_id nullable — record a retired vendor location

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-26

A PMS account's feed keeps delivering a closed site's rows forever. The ETL has
to distinguish two cases that look identical in the data:

  * a location nobody mapped — a wiring gap. Its rows are missing from every
    clinic until someone notices, so it must be reported loudly.
  * a location we deliberately stopped ingesting — a decision already taken.
    Reporting it every sync would mark the job ``partial`` in perpetuity and
    train everyone to ignore the signal that catches the first case.

Telling them apart means recording the retired location, which 0028 could not
express: ``clinic_id`` was NOT NULL, so a location that maps to no clinic had
nowhere to be written. The alternative — pointing the retired row at some
arbitrary clinic just to satisfy the FK — records a mapping that does not exist
and that a later reader would reasonably act on.

So ``clinic_id`` becomes nullable, and the pairing is:

    active=1, clinic_id NOT NULL   route this location's rows to that clinic
    active=0, clinic_id NULL       known site, deliberately not ingested

MySQL cannot express "NULL only when active=0" as a CHECK against another column
portably, so the invariant is enforced in the application (``api/core/orm.py``
documents it; the ETL treats a NULL clinic_id as retired regardless of the flag).

First use: Calgary Hearing Aid and Audiology's Strathmore site (location_id 6),
closed, with ~4.4k historical appointments and ~$0.6M of invoices still arriving
in the shared AB_iai feed on every sync.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "pms_clinic_locations", "clinic_id",
        existing_type=sa.CHAR(36),
        nullable=True,
    )


def downgrade() -> None:
    # Retired rows cannot survive the column going back to NOT NULL, and there is
    # no clinic to attribute them to — drop them rather than invent a mapping.
    op.execute("DELETE FROM pms_clinic_locations WHERE clinic_id IS NULL")
    op.alter_column(
        "pms_clinic_locations", "clinic_id",
        existing_type=sa.CHAR(36),
        nullable=False,
    )
