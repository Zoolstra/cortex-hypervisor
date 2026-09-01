"""jotform_form_locations: route one shared lead form's submissions per location

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-01

``jotform_forms`` (0019) maps a form to ONE clinic, because a Jotform webhook URL
carries exactly one clinic_id in its path. That holds for a single-site clinic,
and breaks for a group that runs every location off one form.

Sense of Hearing is the case that forced this: their "Appointment Request Form"
(262174010008038) serves all 14 Ontario sites and asks the patient to pick one.
Of its first 78 submissions, 11 chose Burlington — so wiring the webhook to the
group's only existing clinic would have attributed 86% of the leads to a site
they never asked for.

This table maps each dropdown answer to a clinic, so the resolver in
``api/webforms.py`` can override the path clinic per submission. The answer is
matched VERBATIM rather than by parsing a location name out of it: these strings
are marketing copy maintained in the Jotform builder and already disagree with
our clinic names on 3 of the 14 options ("Limestone Hearing Care Centre
(Kingston)" vs. clinic *Kingston*, "Mississauga (Eglinton)" vs. *Mississauga
Central*, "St Catharines West" vs. *St. Catharines West*).

Structural only — no seed. Rows are added by
``configure_jotform_webhooks.py --locations`` (which reads the live option list
off the form) or through the campaigns API, both of which need the form's
``jotform_forms`` row and the target clinics to exist first. Prod DDL runs the
same way as every other migration: ``alembic upgrade head`` against the live
Cloud SQL instance (online mode, IAM auth — see alembic/env.py).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "jotform_form_locations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("jotform_form_id", sa.String(32), nullable=False, index=True),
        # 255 rather than TEXT so the column can carry a UNIQUE index. The
        # longest option on any form today is 78 characters.
        sa.Column("option_value", sa.String(255), nullable=False),
        # Nullable on purpose, and it pairs with `active` exactly as
        # pms_clinic_locations does (0029): a known option whose clinic does not
        # exist yet is a different state from an option nobody has mapped, and
        # only the first is a decision. A NULL here is not an error — the
        # submission falls back to the form's own clinic and the resolver logs
        # the unmapped value.
        sa.Column("clinic_id", sa.CHAR(36), nullable=True, index=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp(),
                  server_onupdate=sa.func.current_timestamp()),
        # CASCADE from the form: dropping a form's registry row drops its map.
        sa.ForeignKeyConstraint(
            ["jotform_form_id"], ["jotform_forms.jotform_form_id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.clinic_id"], ondelete="CASCADE"),
        # One clinic per option per form. Two rows for one option would make
        # routing depend on row order.
        sa.UniqueConstraint("jotform_form_id", "option_value",
                            name="uq_jotform_form_location"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("jotform_form_locations")
