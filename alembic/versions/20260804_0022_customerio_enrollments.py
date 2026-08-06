"""customerio_enrollments: send-once log for database-reactivation outbound

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-04

The Alto Hearing pilot pushes tested-not-sold patients into an event-triggered
Customer.io campaign (``api/worklists.py`` customerio-sync endpoint →
``api/services/customerio.py``). This table is the idempotency layer: one row
per (clinic_id, client_id, cohort_key), UNIQUE, so the initial push and every
subsequent daily sync can run over the full current cohort and only NEW
entrants generate a Customer.io event. Consent-blocked / no-contact patients
are logged with their status so they are not re-evaluated every run and the
eligible→sent funnel is auditable. Dry runs write nothing.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "customerio_enrollments",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "clinic_id", sa.CHAR(36),
            sa.ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("cohort_key", sa.String(64), nullable=False),
        sa.Column("event_name", sa.String(128), nullable=False),
        sa.Column(
            "status",
            sa.Enum("sent", "blocked_consent", "no_contact",
                    name="cio_enrollment_status_enum"),
            nullable=False,
        ),
        sa.Column("enrolled_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.text(
                      "CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("clinic_id", "client_id", "cohort_key",
                            name="uq_cio_enroll_clinic_client_cohort"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("customerio_enrollments")
