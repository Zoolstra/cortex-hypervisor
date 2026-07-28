"""jotform_forms: registry for the Jotform → webhook → BigQuery lead pipeline

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-22

Creates ``jotform_forms``, the per-clinic registry of Jotform lead forms wired
(or to be wired) to the hypervisor's webhook relay
(``POST /webforms/jotform/{clinic_id}`` → ``ClinicData.webforms``). Until now
the only record of which form feeds which clinic was a hardcoded dict in
``configure_jotform_webhooks.py``; this table replaces it as the source of
truth so the admin UI, the provisioning script, and coverage reporting all
read the same mapping.

``jotform_form_id`` is UNIQUE globally: a form's webhook targets exactly one
clinic_id, so two rows for one form would double-ingest every submission.

Seeds the 8 forms already wired (or intentionally pre-wired) as of 2026-07-22,
consolidating the script's MAPPING dict + the Alto form IDs documented in
``resources/jotform-webform-setup.md``. Prairie is seeded even though its
``etl_enabled=0`` — webform ingestion is not gated on that flag, so data
accumulates before ETL is switched on. Idempotent via INSERT ... ON DUPLICATE
KEY UPDATE. Prod DDL runs the same way: ``alembic upgrade head`` against the
live Cloud SQL instance (online mode, IAM auth — see alembic/env.py).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text


revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (jotform_form_id, clinic_id, form_title)
_SEED = [
    # Calgary Ear Centre — INLINED forms on the CEC Astro site.
    ("261767450350053", "5e256c93-1369-4f6b-9106-456ab08a1b55", "CortexHQ Lead (contact)"),
    ("261766594045062", "5e256c93-1369-4f6b-9106-456ab08a1b55", "CortexHQ Lead LP (appt request)"),
    # Alto Hearing — iframe embeds (IDs from resources/jotform-webform-setup.md).
    ("261067364350050", "1ce69d99-ec12-4d74-9d31-54806201987f", "Alto contact-us"),
    ("261113834988263", "1ce69d99-ec12-4d74-9d31-54806201987f", "Alto book-a-consultation"),
    ("261067886127061", "1ce69d99-ec12-4d74-9d31-54806201987f", "Alto hearing-survey"),
    # Prairie Hearing Centers — iframe embeds; etl_enabled=0 but wired anyway.
    ("260826975341060", "07752b12-31e5-4168-af37-1e894b0707e6", "Prairie main lead form"),
    ("260836177941263", "07752b12-31e5-4168-af37-1e894b0707e6", "Prairie landing-page opt-in"),
    # Ears for Life — one form serves contact + /lp + /lp-lenire.
    ("261103802261039", "2ead789f-c735-4f32-b1c7-0d69942fc726", "Ear For Life Contact"),
]


def upgrade() -> None:
    op.create_table(
        "jotform_forms",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("clinic_id", sa.CHAR(36), nullable=False, index=True),
        sa.Column("jotform_form_id", sa.String(32), nullable=False),
        sa.Column("form_title", sa.String(255), nullable=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp(),
                  server_onupdate=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.clinic_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("jotform_form_id", name="uq_jotform_form"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    # Seed known wirings. Guard on the clinic existing (INSERT ... SELECT) so
    # the migration succeeds on environments where a seed clinic is absent —
    # the FK would otherwise abort the whole upgrade.
    bind = op.get_bind()
    for form_id, clinic_id, title in _SEED:
        bind.execute(
            text(
                "INSERT INTO jotform_forms (clinic_id, jotform_form_id, form_title) "
                "SELECT clinic_id, :fid, :title FROM clinics WHERE clinic_id = :cid "
                "ON DUPLICATE KEY UPDATE form_title = :title"
            ),
            {"fid": form_id, "cid": clinic_id, "title": title},
        )


def downgrade() -> None:
    op.drop_table("jotform_forms")
