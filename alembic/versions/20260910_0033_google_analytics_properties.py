"""google_analytics_properties: register a clinic's GA4 property as a campaign

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-10

Fourth typed campaign table (after ``google_ads_campaigns``, ``invoca_campaigns``
and ``jotform_forms``), presented through the same campaigns API as
``campaign_type = "google_analytics"``. A row (``active=1``, clinic not
soft-deleted) tells the ETL job ``ga4-ingest`` to pull that GA4 property's daily
reports into ``ClinicData.ga4_*``. Registering IS enabling — there is nothing to
configure on the Google side beyond the read grant the impersonated identity
already holds (see ``resources/google-analytics-integration-plan.md`` §2).

``ga4_property_id`` is UNIQUE globally, not per clinic — Jotform semantics. A
multi-location business usually runs ONE property for one website that lists
every site, so the property is registered against a DEFAULT clinic and the group
rollup dedupes by property. Linking one property to every clinic in a group
would count each session once per clinic. Per-location splitting (by key-event
name / landing-page path) is a later table, not this one.

Also adds ``instances.ga4_account_id`` — the instance-level GA account handle,
parallel to ``google_ads_customer_id`` / ``invoca_profile_id`` — so the campaign
picker can filter the property catalog to this client's account. Nullable: an
instance without it falls back to manual property-id entry in the admin UI.

Structural only — no seed. The registry is populated through the admin UI /
``POST /campaigns/{clinic_id}`` from the mapping in the plan's §2.6. Prod DDL
runs the same way as every other migration: ``alembic upgrade head`` against the
live Cloud SQL instance (online mode, IAM auth — see alembic/env.py).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column("ga4_account_id", sa.String(32), nullable=True),
    )

    op.create_table(
        "google_analytics_properties",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("clinic_id", sa.CHAR(36), nullable=False, index=True),
        sa.Column("ga4_property_id", sa.String(32), nullable=False),
        sa.Column("property_name", sa.String(255), nullable=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp(),
                  server_onupdate=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.clinic_id"], ondelete="CASCADE"),
        # One property → one default clinic. Two rows would double-count every
        # session in the group rollup.
        sa.UniqueConstraint("ga4_property_id", name="uq_ga4_property"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("google_analytics_properties")
    op.drop_column("instances", "ga4_account_id")
