"""Role-scoped agent compilers — single-purpose assistants authored per role.

The general factory (``factory.py``) assembles a do-everything receptionist from
a hardcoded stage flow + concatenated protocol fragments. That shape failed for
ACNA: with four competing jobs in one prompt and the booking playbook trailing
the "capture & close" instruction, gpt-4o routinely took a message instead of
calling the booking tools (salience inversion).

A ROLE compiler builds the opposite shape: a single-purpose assistant whose
entire system prompt IS the job's linear procedure and whose tool list contains
only that job's tools. Protocols stay the reusable unit (tools + config); the
role authors the conversational frame around them instead of concatenating
fragments.

Dispatch: ``ClinicVoiceAgentConfiguration.agent_role`` selects the compiler
('general' → the legacy stage-flow factory; 'annual_booking' → this module's
booking specialist). Adding a role = adding a builder here + a registry entry.

Scalability note: this is the N=1 case of the squad topology. When a clinic
needs multiple specialist jobs, these same role-built assistants become squad
members behind a receptionist entry member — the role builders don't change.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from api.core.orm import Clinic, ClinicVoiceAgentPersona
from api.voice_agent.locale import resolve as resolve_locale
from api.voice_agent.protocols import PROTOCOL_REGISTRY, load_protocol_config
from api.voice_agent.protocols.base import Protocol

log = logging.getLogger(__name__)

ROLE_GENERAL = "general"
ROLE_ANNUAL_BOOKING = "annual_booking"


def _make_protocol(
    db: Session, clinic: Clinic, protocol_id: str, credential_id: str,
) -> Protocol:
    """Instantiate one protocol with its stored per-clinic config.

    Roles declare the protocols they REQUIRE — unlike the general factory's
    toggle-driven assembly, a missing/incompatible protocol here is a
    configuration bug and raises loudly at sync time rather than silently
    dropping a tool the prompt depends on.
    """
    cls = PROTOCOL_REGISTRY[protocol_id]
    config = load_protocol_config(db, clinic.clinic_id, protocol_id)
    return cls(
        clinic_id=clinic.clinic_id,
        clinic_name=clinic.clinic_name,
        pms_type=clinic.pms_type or "none",
        credential_id=credential_id,
        config=config,
    )


# ── Annual-booking specialist ─────────────────────────────────────────────────

# Protocols this role requires. The search protocol contributes only its
# find_available_slots tool (list_appointment_types is excluded — the decision
# tool supplies the event type, so the list tool would only be a wrong choice).
_BOOKING_PROTOCOLS = (
    "verify_caller_identification",
    "acna_determine_appointment",
    "acna_search_availability",
    "acna_book_appointment",
    "submit_ticket",
)

_EXCLUDED_TOOL_NAMES = {"list_appointment_types"}


def build_annual_booking_config(db: Session, clinic: Clinic) -> dict:
    """Compile the single-purpose after-hours booking specialist for a clinic.

    Returns the same VAPI assistant payload shape as
    ``factory.build_agent_config`` — callers (sync endpoints, resync script)
    don't care which compiler produced it.
    """
    # Local import: factory imports this module lazily for dispatch; importing
    # factory at module load would be circular.
    from api.voice_agent.factory import (  # noqa: PLC0415
        _hours_block, _persona_or_defaults,
        _vapi_credential_id, build_first_message,
    )

    locale = resolve_locale(clinic)
    credential_id = _vapi_credential_id()
    persona = db.get(ClinicVoiceAgentPersona, clinic.clinic_id)

    protos = {
        pid: _make_protocol(db, clinic, pid, credential_id)
        for pid in _BOOKING_PROTOCOLS
    }

    tools = [
        t
        for pid in _BOOKING_PROTOCOLS
        for t in protos[pid].tools()
        if t.get("name") not in _EXCLUDED_TOOL_NAMES
    ]

    agent_name, _title, voice_id, _override, ai_model = _persona_or_defaults(persona)

    system_prompt = _annual_booking_prompt(
        clinic=clinic,
        locale=locale,
        agent_name=agent_name,
        hours_block=_hours_block(clinic),
        verify_fragment=protos["verify_caller_identification"].prompt_fragment,
        decision_fragment=protos["acna_determine_appointment"].prompt_fragment,
        book_fragment=protos["acna_book_appointment"].prompt_fragment,
    )

    return {
        "name": clinic.clinic_name,
        "first_message": build_first_message(clinic.clinic_name, persona),
        "first_message_interruptions_enabled": True,
        "model": {
            "provider": "openai",
            "model": ai_model,
            "messages": [{"role": "system", "content": system_prompt}],
            "tools": tools,
        },
        "voice": {"speed": 0.9, "provider": "vapi", "voiceId": voice_id},
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-2",
            "language": locale["transcriber_language"],
        },
    }


def _annual_booking_prompt(
    *,
    clinic: Clinic,
    locale: dict,
    agent_name: str,
    hours_block: str,
    verify_fragment: str,
    decision_fragment: str,
    book_fragment: str,
) -> str:
    """Author the specialist's linear system prompt.

    Ordering is deliberate and load-bearing: the booking procedure IS the
    prompt's spine; the take-a-message path appears only as an explicitly gated
    step inside it; the closing/ticket block is last. Tool use is imperative and
    falsifiable ("saying it's booked without the tool succeeding is a failure").

    The behavior rules and the closing block are AUTHORED here rather than
    reusing the general factory's — the generalist versions reference stages
    and cancel/reschedule tools this assistant doesn't have, and the generic
    ticket close says "you cannot confirm a specific appointment time", which
    contradicts a call where a booking just succeeded.
    """
    flow = f"""## Your one job
You book appointments for existing patients of {clinic.clinic_name}, after hours. Work through these steps IN ORDER on every call. Do not skip ahead, and do not fall back to taking a message unless a step below explicitly sends you there.

### Step 1 — Opening
Your greeting already told the caller the office is closed and that you can book their annual hearing test or take a message. Find out which they need. Anything booking-shaped — "annual", "yearly test", "hearing check", "check-up", "service", "am I due?" — goes to Step 2. Only a clearly non-booking need (billing, a complaint, hearing-aid trouble, "I want to talk to a person") goes to Step 5.

### Step 2 — Identify the patient
{verify_fragment}

If the caller is NOT found (unmatched/ambiguous after the retry) or says they are new to the clinic: do NOT book. Go to Step 5 and take a message — note in the ticket that they may need a new-patient intake with a clinician.

### Step 3 — Determine the right appointment
{decision_fragment}

### Step 4 — Find a time and book it
Call `find_available_slots` with the `real_event_type_id` that `determine_appointment_type` returned, over a 1-2 week window (wider if the caller asks). Offer the open days/times conversationally — summarize ("Tuesday morning or Thursday afternoon") rather than reading every slot; name providers only if the caller asks or stated a preference.

{book_fragment}

### Step 5 — Take a message (ONLY when sent here)
You reach this step only when: the caller's need isn't booking, the patient can't be identified, the decision says the appointment type isn't self-bookable, no offered time works, or a tool fails. Collect their name, the best callback number, and what they need. Tell them a team member will call back the next business day. Never present this step as the first option to a caller who wants to book.

### Step 6 — Close (every call)
Before ending ANY call, call `submit_ticket` EXACTLY ONCE — it is how the clinic sees this call; without it the call is lost. Include: caller_name, caller_phone (E.164), patient_match_status, blueprint_patient_id (if matched), intent_category (short label like "annual booking — booked" or "message — service question"), a 1-2 sentence summary, suggested_followup, and urgency ('urgent' only for a time-sensitive medical concern).

- If you BOOKED on this call: briefly restate the booking one last time ("You're on the schedule for your Annual on Tuesday July 21st at 11 — the team will confirm it") and set suggested_followup to "Confirm tentative booking".
- If you TOOK A MESSAGE: remind them a team member will call back the next business day; if `determine_appointment_type` returned an outcome you couldn't book, put that outcome in suggested_followup so staff book the right thing.
- Then a warm goodbye and end the call. If `submit_ticket` fails, apologize, say a team member will call them back, and end gracefully."""

    rules = """## Non-negotiable tool rules
- NEVER tell the caller an appointment is booked unless `book_appointment` returned status "booked" on THIS call. Claiming a booking without that is a failure.
- NEVER quote an available time you did not receive from `find_available_slots` on THIS call.
- NEVER decide eligibility or the appointment type yourself — that is `determine_appointment_type`'s job, always.
- NEVER promise a live transfer or a confirmed time. The office is closed; bookings are tentative until staff confirm ("you're on the schedule", not "confirmed").
- If any tool errors twice, apologize, go to Step 5, and note the failure in the ticket."""

    identity = (
        f"## Identity\nYou are {agent_name}, the after-hours virtual booking "
        f"assistant for {clinic.clinic_name}. The office is closed right now. "
        "You speak warmly and efficiently — callers are often older adults; "
        "keep sentences short and never rush them."
    )

    behavior = """## Conversational behavior
- **Never re-ask information the caller has already volunteered.**
- **Acknowledge briefly** — don't recite every detail back. Verbatim readback IS required right before a value goes into a tool: spell back the name and confirm the phone digits before `verify_caller_identification`, and read back the appointment type, day, and time before `book_appointment`.
- **Ask one focused question per turn.** No multi-part interrogations.
- **Tool calls happen quietly.** You may say "one moment while I check" once; never narrate tool names or systems.
- Say numbers and times naturally ("ten thirty in the morning", not "10:30")."""

    parts = [
        locale["prompt_block"],
        identity,
        behavior,
        flow,
        rules,
        hours_block,
    ]
    return "\n\n".join(p for p in parts if p)


# ── Role registry + dispatch ──────────────────────────────────────────────────

ROLE_BUILDERS = {
    ROLE_ANNUAL_BOOKING: build_annual_booking_config,
}

# Protocols each role's compiler hard-requires. Used by the capability
# endpoints to LOCK these toggles for role clinics — the role builder
# instantiates them unconditionally, so a dashboard "disable" would otherwise
# be a silent no-op (report success, change nothing on the live agent).
ROLE_REQUIRED_PROTOCOLS: dict[str, tuple[str, ...]] = {
    ROLE_ANNUAL_BOOKING: _BOOKING_PROTOCOLS,
}


def resolve_agent_role(clinic: Clinic) -> str:
    """Read the clinic's agent role off its voice-agent configuration row."""
    va = getattr(clinic, "voice_agent", None)
    role = getattr(va, "agent_role", None) if va else None
    return role or ROLE_GENERAL


def role_required_protocol_ids(clinic: Clinic) -> frozenset[str]:
    """Protocol ids the clinic's agent role hard-requires (empty for general)."""
    return frozenset(ROLE_REQUIRED_PROTOCOLS.get(resolve_agent_role(clinic), ()))
