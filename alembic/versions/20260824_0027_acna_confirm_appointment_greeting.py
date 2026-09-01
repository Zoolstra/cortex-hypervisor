"""ACNA: greeting offers appointment confirmation, not just booking

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-24

The annual-booking role gained `locate_appointment` + `confirm_appointment`,
so the agent can now look up and confirm an appointment the caller already
has. The greeting still advertised only two options ("book your annual hearing
test, or take a message"), and on the 2026-08-24 test call that framing visibly
steered the caller: they opened "I gotta book an appointment here — I mean,
sorry, I wanna confirm an appointment that I have upcoming."

A greeting that names the wrong capabilities is not cosmetic. It is the only
menu the caller ever hears, and they will try to fit their need into it.

Idempotent and reversible: only rewrites the row when it still holds the exact
pre-0027 string, so a hand edit made in the dashboard is never clobbered.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACNA_CLINIC_ID = "0b5f0929-31fb-4e21-9dd4-030bd040335d"

_OLD = (
    "Thanks for calling {clinic_name}. Our office is closed right now, but I'm "
    "{agent_name}, the clinic's virtual assistant — I can book your annual "
    "hearing test, or take a message for the team. How can I help?"
)

# "check or confirm" rather than just "confirm": the same tool answers "what
# time is my appointment?", and callers who only want to check shouldn't have
# to infer that it's on offer.
_NEW = (
    "Thanks for calling {clinic_name}. Our office is closed right now, but I'm "
    "{agent_name}, the clinic's virtual assistant — I can book your annual "
    "hearing test, check or confirm an appointment you already have, or take a "
    "message for the team. How can I help?"
)


def _swap(frm: str, to: str) -> None:
    op.get_bind().execute(
        text(
            "UPDATE clinic_voice_agent_persona SET first_message = :to "
            "WHERE clinic_id = :cid AND first_message = :frm"
        ),
        {"cid": ACNA_CLINIC_ID, "frm": frm, "to": to},
    )


def upgrade() -> None:
    _swap(_OLD, _NEW)


def downgrade() -> None:
    _swap(_NEW, _OLD)
