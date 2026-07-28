"""lean_intake flag + ACNA after-hours voicemail-replacement persona/scope

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-20

Adds ``clinic_voice_agent_script.lean_intake`` (default 0 — existing clinics keep
the full discovery flow) and configures ACNA for the Phase-1 after-hours
voicemail-replacement agent:
  - lean_intake = 1 (drop the sales-oriented new-patient discovery/caller-bucket
    machinery in Stage 3a);
  - scope / services-not-offered / additional-notes framing the 3 allowed things,
    "no live transfer", "bookings are tentative", and "take a message → call back
    next business day";
  - an after-hours first-message greeting.

Content is a starting point — clinic admins can refine it in the dashboard's
Voice Agent Script / Persona sections. Idempotent upserts.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text


revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACNA_CLINIC_ID = "0b5f0929-31fb-4e21-9dd4-030bd040335d"

_SCOPE = (
    "You are answering after hours, while the clinic is closed. You can help the "
    "caller with three things:\n"
    "1. Book a service appointment (hearing-aid service or repair).\n"
    "2. Book an annual hearing check.\n"
    "3. Walk the caller through a few basic hearing-aid troubleshooting steps.\n"
    "For anything else, take a message so a team member can call the caller back on "
    "the next business day."
)

_NOT_OFFERED = (
    "You cannot connect the caller to a live person — the office is closed and there "
    "is no one to transfer to. Never offer, imply, or promise a transfer to a person. "
    "Anything beyond booking a service or annual appointment or basic hearing-aid "
    "troubleshooting is out of scope after hours; for those, take a message."
)

_NOTES = (
    "- Never promise a specific or confirmed appointment time. Any appointment you "
    "book is tentative until clinic staff confirm it — say \"you're on the schedule\" "
    "or \"we've got you down\", not \"confirmed\".\n"
    "- If the caller wants a person, is upset, or has a need you can't handle, warmly "
    "take a message: get their name and best callback number and what they need, and "
    "tell them a team member will call them back the next business day.\n"
    "- Keep the call efficient and kind — this is a voicemail replacement, not a "
    "sales call."
)

_FIRST_MESSAGE = (
    "Thanks for calling {clinic_name}. Our office is closed right now, but I'm "
    "{agent_name}, the clinic's virtual assistant — I can book you an appointment or "
    "take a message for the team. How can I help?"
)


def upgrade() -> None:
    op.add_column(
        "clinic_voice_agent_script",
        sa.Column("lean_intake", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )

    bind = op.get_bind()
    bind.execute(
        text(
            "INSERT INTO clinic_voice_agent_script "
            "(clinic_id, scope_of_practice, services_not_offered, additional_notes, lean_intake) "
            "VALUES (:cid, :scope, :notoff, :notes, 1) "
            "ON DUPLICATE KEY UPDATE scope_of_practice = :scope, "
            "services_not_offered = :notoff, additional_notes = :notes, lean_intake = 1"
        ),
        {"cid": ACNA_CLINIC_ID, "scope": _SCOPE, "notoff": _NOT_OFFERED, "notes": _NOTES},
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
    # Clear ACNA's after-hours content and drop the column. (Leaves the persona
    # first_message cleared — it falls back to the templated default greeting.)
    bind = op.get_bind()
    bind.execute(
        text(
            "UPDATE clinic_voice_agent_script SET lean_intake = 0, "
            "scope_of_practice = NULL, services_not_offered = NULL, additional_notes = NULL "
            "WHERE clinic_id = :cid"
        ),
        {"cid": ACNA_CLINIC_ID},
    )
    bind.execute(
        text("UPDATE clinic_voice_agent_persona SET first_message = NULL WHERE clinic_id = :cid"),
        {"cid": ACNA_CLINIC_ID},
    )
    op.drop_column("clinic_voice_agent_script", "lean_intake")
