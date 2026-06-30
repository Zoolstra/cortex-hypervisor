"""clinic_counselear_config + pms_type 'counselear'

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-18

Adds CounselEar as a supported PMS:
  - Extends the ``pms_type`` enum with ``counselear``.
  - Creates ``clinic_counselear_config`` mapping a CORTEX clinic to its
    CounselEar feed identifiers (the per-practice SFTP location folder code and
    CounselEar's per-row clinic id), mirroring ``clinic_blueprint_config``.

The big-query-ingestion ``counselear-sync`` job reads this table (joined to
``clinics`` on ``pms_type='counselear'`` + ``etl_enabled``) to know which SFTP
folder to pull and how to resolve each feed row to a CORTEX clinic.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Widen the pms_type enum. MySQL stores ENUM as a column-level type, so an
    # ALTER COLUMN with the full member list (keeping order stable) is the
    # supported way to add a value.
    op.alter_column(
        "clinics", "pms_type",
        existing_type=sa.Enum("blueprint", "audit_data", "none", name="pms_type_enum"),
        type_=sa.Enum("blueprint", "counselear", "audit_data", "none", name="pms_type_enum"),
        existing_nullable=False,
        existing_server_default="none",
    )

    op.create_table(
        "clinic_counselear_config",
        sa.Column("clinic_id", sa.CHAR(36), nullable=False),
        sa.Column("counselear_location_code", sa.String(64), nullable=True),
        sa.Column("counselear_clinic_id", sa.String(64), nullable=True),
        sa.Column("counselear_sftp_username", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp(),
                  server_onupdate=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.clinic_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("clinic_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    # Resolve a feed row (carrying CounselEar's clinic id) to a CORTEX clinic.
    op.create_index(
        "ix_counselear_clinic_id", "clinic_counselear_config", ["counselear_clinic_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_counselear_clinic_id", table_name="clinic_counselear_config")
    op.drop_table("clinic_counselear_config")
    op.alter_column(
        "clinics", "pms_type",
        existing_type=sa.Enum("blueprint", "counselear", "audit_data", "none", name="pms_type_enum"),
        type_=sa.Enum("blueprint", "audit_data", "none", name="pms_type_enum"),
        existing_nullable=False,
        existing_server_default="none",
    )
