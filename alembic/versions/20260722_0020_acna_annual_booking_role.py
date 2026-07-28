"""agent_role column + ACNA annual-booking specialist enablement

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-22

Introduces role-scoped agent compilation and switches ACNA to the single-purpose
after-hours annual-booking specialist:

  1. ``clinic_voice_agent_configuration.agent_role`` (default 'general' — every
     existing clinic keeps the legacy stage-flow factory).
  2. Enables the ``acna_determine_appointment`` protocol for ACNA with its
     seeded config (clinician list, plan/payer rules, outcome→type map — only
     the Annual outcome is bookable until the remaining placeholder pairs are
     confirmed). Dual-writes clinic_protocols + legacy voice_agent_capabilities,
     matching the toggle endpoint.
  3. Sets ACNA's agent_role = 'annual_booking' and refreshes the first message
     to announce the annual-booking capability.

Rollback: downgrade drops the column and disables the protocol; ACNA reverts to
the general factory on next sync.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text


revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACNA_CLINIC_ID = "0b5f0929-31fb-4e21-9dd4-030bd040335d"
PROTOCOL_ID = "acna_determine_appointment"

_DECISION_CONFIG = json.dumps({
    "clinician_names": [
        "Palmer, Essie",
        "Ohlin, Lisa",
        "Lewchuk, Larena",
        "Roy, Natalie",
        "Andres, Ashlea",
    ],
    "qualifying_plan_names": [
        "Complete Care Plan",
        "CCP LACE",
        "Care Plan, No Batts",
    ],
    "non_qualifying_plan_names": ["Pre-Plan"],
    "unknown_plan_allows_annual": True,
    "payer_min_years": {"WCB": 1.0, "Veterans Affairs": 2.0, "Other": 1.0},
    "clinician_visit_threshold_years": 1.0,
    "outcome_booking": {
        "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN": 207,
        "BOOK_CLINICIAN_SERVICE_VISIT": None,
        "BOOK_TECHNICIAN_CLEAN_AND_CHECK": None,
        "BOOK_NEW_PATIENT_INTAKE_WITH_CLINICIAN": None,
    },
})

_FIRST_MESSAGE = (
    "Thanks for calling {clinic_name}. Our office is closed right now, but I'm "
    "{agent_name}, the clinic's virtual assistant — I can book your annual "
    "hearing test, or take a message for the team. How can I help?"
)

# 0017's greeting, restored on downgrade so the reverted general-factory agent
# doesn't keep announcing an annual-booking capability it no longer has.
_PRIOR_FIRST_MESSAGE = (
    "Thanks for calling {clinic_name}. Our office is closed right now, but I'm "
    "{agent_name}, the clinic's virtual assistant — I can book you an appointment or "
    "take a message for the team. How can I help?"
)


def upgrade() -> None:
    op.add_column(
        "clinic_voice_agent_configuration",
        sa.Column("agent_role", sa.String(length=32), nullable=False,
                  server_default="general"),
    )

    bind = op.get_bind()
    for table, id_col in (
        ("clinic_protocols", "protocol_id"),
        ("voice_agent_capabilities", "capability_id"),
    ):
        bind.execute(
            text(
                f"INSERT INTO {table} (clinic_id, {id_col}, enabled, config, updated_by) "
                f"VALUES (:cid, :pid, 1, CAST(:cfg AS JSON), :ub) "
                f"ON DUPLICATE KEY UPDATE enabled = 1, config = CAST(:cfg AS JSON), "
                f"updated_by = :ub"
            ),
            {"cid": ACNA_CLINIC_ID, "pid": PROTOCOL_ID,
             "cfg": _DECISION_CONFIG, "ub": "migration-0020"},
        )

    bind.execute(
        text(
            "UPDATE clinic_voice_agent_configuration SET agent_role = 'annual_booking' "
            "WHERE clinic_id = :cid"
        ),
        {"cid": ACNA_CLINIC_ID},
    )
    bind.execute(
        text(
            "INSERT INTO clinic_voice_agent_persona (clinic_id, first_message) "
            "VALUES (:cid, :fm) "
            "ON DUPLICATE KEY UPDATE first_message = :fm"
        ),
        {"cid": ACNA_CLINIC_ID, "fm": _FIRST_MESSAGE},
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table, id_col in (
        ("clinic_protocols", "protocol_id"),
        ("voice_agent_capabilities", "capability_id"),
    ):
        bind.execute(
            text(
                f"UPDATE {table} SET enabled = 0, updated_by = 'migration-0020-downgrade' "
                f"WHERE clinic_id = :cid AND {id_col} = :pid"
            ),
            {"cid": ACNA_CLINIC_ID, "pid": PROTOCOL_ID},
        )
    # Restore 0017's greeting (only if ours is still in place — don't clobber a
    # later hand-edited message) so the reverted agent doesn't promise
    # annual-test booking it can no longer do.
    bind.execute(
        text(
            "UPDATE clinic_voice_agent_persona SET first_message = :prior "
            "WHERE clinic_id = :cid AND first_message = :ours"
        ),
        {"cid": ACNA_CLINIC_ID, "prior": _PRIOR_FIRST_MESSAGE, "ours": _FIRST_MESSAGE},
    )
    op.drop_column("clinic_voice_agent_configuration", "agent_role")
