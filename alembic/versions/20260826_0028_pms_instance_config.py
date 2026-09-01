"""instance_pms_config + pms_clinic_locations: account-level PMS config

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-26

PMS was the last integration with no instance level. Google Ads and Invoca put
the account on ``instances`` (``google_ads_customer_id``, ``invoca_profile_id``)
and resolve rows to clinics through a map table (``google_ads_campaigns``,
``invoca_campaigns``); Jotform maps forms to clinics via ``jotform_forms``.
Blueprint had only ``clinic_blueprint_config``, which is 1:1 with a clinic — so
one Blueprint account serving several physical locations could not be expressed
without giving every location its own duplicate copy of the account's
credentials and S3 feed URL, which would download and load the whole account
once per clinic.

This adds the missing level:

  instance_pms_config    one PMS account per (instance, pms_type)
                         secrets → instance_{instance_id}_blueprint_*
                         (the per-clinic layout stays clinic_{clinic_id}_blueprint_*)

  pms_clinic_locations   vendor location id → CORTEX clinic, for that account

Resolution order in the ETL and the API is unchanged for existing clients: a
``clinic_blueprint_config`` row wins, and only when a clinic has none does the
instance-level config plus the map apply. No backfill, and every current
single-location Blueprint client keeps its existing config and secret names.

``vendor_location_key`` is UNIQUE per (instance, pms_type) — the same rule
``jotform_forms`` enforces globally on ``jotform_form_id``, and for the same
reason: a location mapped to two clinics would double-ingest its rows. It is
stored as a string rather than an int because vendors differ (Blueprint uses a
numeric ``location_id``, CounselEar a string clinic id) and the ids are not
contiguous — Calgary Hearing Aid and Audiology's live account exposes 1, 2, 4,
5, 6 with 3 absent and 6 (Strathmore) inactive.

``primary_clinic_id`` is where rows land when the feed gives no location for
them. Blueprint tags ``Appointments`` with ``location_id`` and ``InvoiceMaster``
with a location name, but the patient-level tables (``ClientDemographics``,
``ClientAids``, ``ClientRecall``, …) carry only ``branch_id``, which is
account-wide. Those rows must go to exactly one clinic, never be replicated
across all of them: the reactivation and dormant worklists anti-join
``ClientDemographics`` against that clinic's ``Appointments``, so a replicated
patient list makes every patient look dormant at the locations they don't
attend. Assigning them to one clinic understates the others; replicating them
would invent work. A later revision replaces this with a home location derived
from each patient's appointment history.

No rows are seeded — accounts are provisioned through the pms_config API.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _pms_type_enum() -> sa.Enum:
    # Same member list and order as clinics.pms_type (migration 0011). MySQL
    # ENUMs are column-level, so this is an independent copy of the type, not a
    # shared one — keep the members in sync when the clinics enum widens.
    return sa.Enum("blueprint", "counselear", "audit_data", "none", name="pms_type_enum")


def upgrade() -> None:
    op.create_table(
        "instance_pms_config",
        sa.Column("instance_id", sa.CHAR(36), nullable=False),
        sa.Column("pms_type", _pms_type_enum(), nullable=False),
        # Same non-secret fields as clinic_blueprint_config, at account scope.
        sa.Column("clinic_code", sa.String(64), nullable=True),
        sa.Column("api_url", sa.String(512), nullable=True),
        sa.Column("aws_url", sa.String(512), nullable=True),
        # Fallback clinic for feed rows that carry no location discriminator.
        sa.Column("primary_clinic_id", sa.CHAR(36), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp(),
                  server_onupdate=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["instance_id"], ["instances.instance_id"],
                                ondelete="CASCADE"),
        # SET NULL rather than CASCADE: losing the fallback clinic must not
        # silently delete the account's credentials along with it.
        sa.ForeignKeyConstraint(["primary_clinic_id"], ["clinics.clinic_id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("instance_id", "pms_type"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "pms_clinic_locations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("instance_id", sa.CHAR(36), nullable=False),
        sa.Column("pms_type", _pms_type_enum(), nullable=False),
        sa.Column("vendor_location_key", sa.String(64), nullable=False),
        sa.Column("clinic_id", sa.CHAR(36), nullable=False, index=True),
        # The vendor's own label for the site, kept for provisioning UIs and for
        # matching feed columns that carry a location name but no id.
        sa.Column("location_name", sa.String(255), nullable=True),
        # Inactive locations keep their row so historical rows stay attributable,
        # but the ETL stops routing new rows to them.
        sa.Column("active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp(),
                  server_onupdate=sa.func.current_timestamp()),
        # A map row without an account config row is meaningless, so the parent
        # is the config, not the instance.
        sa.ForeignKeyConstraint(
            ["instance_id", "pms_type"],
            ["instance_pms_config.instance_id", "instance_pms_config.pms_type"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.clinic_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("instance_id", "pms_type", "vendor_location_key",
                            name="uq_pms_location"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("pms_clinic_locations")
    op.drop_table("instance_pms_config")
