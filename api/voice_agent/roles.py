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
    # Existing-appointment path. Added 2026-08-24 after a live caller asked to
    # CONFIRM an upcoming appointment and the agent had to say "I can't check
    # existing appointments right now" and take a message — it had no tool to
    # look one up. Locate supplies the appointment_id; Confirm writes the
    # PMS status. These sit before the decision/booking protocols so the
    # prompt's existing-appointment branch reads before the new-booking spine.
    "locate_appointment",
    "confirm_appointment",
    "acna_determine_appointment",
    "acna_search_availability",
    "acna_book_appointment",
    # FAQ retrieval. Answers are fetched mid-call, never baked into the prompt —
    # a growing corpus in the system prompt is precisely the dilution this role
    # was created to escape (see module docstring). Its fragment is placed AFTER
    # the booking flow so the spine keeps first position.
    "faq_lookup",
    "submit_ticket",
)

_EXCLUDED_TOOL_NAMES = {"list_appointment_types"}

# An explicit EMPTY request-start message on every tool.
#
# VAPI speaks a tool's `request-start` message when the tool fires. Leaving
# `messages` unset does not mean "say nothing" — it leaves the slot to whatever
# the model emits as content alongside its tool call, and gpt-4o's prior for
# that is a filler ("Just a sec."). Three rounds of prompt instructions have
# failed to suppress it, the last of which names the exact phrases and is
# verifiably present in the live assistant. Declaring the slot with empty
# content claims it explicitly instead of leaving it to the model.
#
# Prompt rules stay as well — they are what stops the model narrating AFTER a
# tool returns, which this does not touch.
_SILENT_TOOL_MESSAGES = [{"type": "request-start", "content": "", "blocking": False}]


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
        {**t, "messages": list(_SILENT_TOOL_MESSAGES)}
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
        locate_fragment=protos["locate_appointment"].prompt_fragment,
        confirm_fragment=protos["confirm_appointment"].prompt_fragment,
        decision_fragment=protos["acna_determine_appointment"].prompt_fragment,
        book_fragment=protos["acna_book_appointment"].prompt_fragment,
        faq_fragment=protos["faq_lookup"].prompt_fragment,
    )

    return {
        "name": clinic.clinic_name,
        "first_message": build_first_message(clinic.clinic_name, persona),
        "first_message_interruptions_enabled": True,
        "model": {
            "provider": "openai",
            "model": ai_model,
            # Pinned explicitly rather than left to VAPI's default. This role is
            # a procedure, not a conversation: the same caller state should
            # produce the same words and the same tool call every time. VAPI
            # currently defaults to 0 too, so this is a lock against that
            # default moving, not a change in behavior.
            "temperature": 0,
            "messages": [{"role": "system", "content": system_prompt}],
            "tools": tools,
        },
        # 0.85: callers are largely older adults, and the shorter turns this
        # role now speaks read as clipped at 0.9.
        "voice": {"speed": 0.85, "provider": "vapi", "voiceId": voice_id},
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
    locate_fragment: str,
    confirm_fragment: str,
    decision_fragment: str,
    book_fragment: str,
    faq_fragment: str,
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
You handle appointments for existing patients of {clinic.clinic_name}, after hours: you BOOK new ones, and you look up and CONFIRM ones they already have. Work through these steps IN ORDER on every call. Do not skip ahead, and do not fall back to taking a message unless a step below explicitly sends you there.

### Step 1 — Opening
Your greeting already told the caller the office is closed and what you can do. Find out which they need, and route:

- **About an appointment they ALREADY have** — "confirm my appointment", "am I still booked?", "what time is my appointment?", "do I have anything coming up?" → verify them (Step 2), then go to **Step 3A**.
- **A NEW appointment** — "annual", "yearly test", "hearing check", "check-up", "service", "am I due?" → verify them (Step 2), then Step 3.
- **Neither** — billing, a complaint, hearing-aid trouble, "I want to talk to a person" → Step 5.

If you are unsure which, ask ONE question: "Is that a new appointment, or one you already have booked?"

**You CAN look up existing appointments.** Never tell a caller you can't check what they have booked — `locate_appointment` does exactly that, and taking a message instead of using it is a failure.

If they open with a **general information question** — hours, parking, what to bring, what a test costs, whether you take their insurance — answer it with `answer_clinic_question` (see below), then come straight back to "would you like me to get you booked in?" and continue with Step 2. Answering a question is never a reason to abandon the booking.

### Step 2 — Identify the patient
You need TWO things and only two: their **last name** and the **last four digits** of the phone on file. Ask for both in one question, confirm both in one readback, then look them up.

**Give ONE short reason before that readback**, so the letter-by-letter check reads as care rather than bureaucracy. Use this line or something very close to it: "So I'm sure I've got the right file, let me just read that back." Then read it back. Never launch into "S-M-Y-T-H-E" cold — and never give the reason twice.

{verify_fragment}

If the caller is NOT found (unmatched/ambiguous after the retry) or says they are new to the clinic: do NOT book. Go to Step 5 and take a message — note in the ticket that they may need a new-patient intake with a clinician.

### Step 3A — An appointment they already have
Take this branch when the caller is asking about, or confirming, an EXISTING booking. Do not run the appointment-decision tool here — that decides what to book NEW, and it is not what they asked for.

1. Call `locate_appointment` with the verified `patient_id`.
2. **Nothing booked in the window?** Say so plainly, ask whether it might be further out (call again with a wider `days_ahead`) — and if there is genuinely nothing, offer to book them in and continue at Step 3.
3. **One upcoming appointment?** Read it back — type, day, time — and ask if that's the one.
4. **Several?** Name them briefly and ask which they mean. Never assume the soonest.
5. If they are CONFIRMING they'll attend, follow the Confirm Appointment steps below.
6. If they want to cancel or move it, you cannot do that yourself — say a team member will take care of it and go to Step 5, putting the appointment and what they wanted in the ticket.

Then go to Step 6 and close. Do NOT continue into Step 3/4 — they already have their appointment.

{locate_fragment}

{confirm_fragment}

### Step 3 — Determine the right appointment (NEW bookings only)
{decision_fragment}

### Step 4 — Find a time and book it
**Ask for a time preference BEFORE you offer anything.** One short question: "Do mornings or afternoons work better for you?" — and if they volunteer a day or a week too, take that. Don't offer times blind and don't make them listen to a list to discover what you have.

Then call `find_available_slots` with the `real_event_type_id` that `determine_appointment_type` returned, over a 1-2 week window (wider if the caller asks). Respect the date constraints the decision returned: never start before `earliest_bookable_date`, and when there's a `preferred_window`, search inside it first and say why.

**How many times to offer — this is deliberate, don't improvise:**
- **They gave a preference** (a day, a time of day, a week) → offer **TWO** slots that match it. "I've got Tuesday at ten thirty, or Wednesday at eleven — which of those works?" If only one matches, offer that one and say it's the only one in that window.
- **They gave no preference** → offer **THREE**, nearest first.

Never read out more than three — a longer list is one callers stop tracking.
- If NOTHING matches their preference, say so plainly, then offer the first two you do have ("I don't have any mornings that week — I do have Tuesday at one, or Thursday at two thirty").
- If they turn all of them down, offer the next set, and widen the window if you run out.
- Name providers only if the caller asks or stated a preference.
- Offer the times and stop. Don't add commentary about the schedule, the wait, or how busy the clinic is.

{book_fragment}

### Step 5 — Take a message (ONLY when sent here)
You reach this step only when: the caller's need isn't booking, the patient can't be identified, the decision says the appointment type isn't self-bookable, no offered time works, or a tool fails. Collect their name, the best callback number, and what they need. Tell them a team member will call back the next business day. Never present this step as the first option to a caller who wants to book.

### Step 6 — Close (every call)
Before ending ANY call, call `submit_ticket` EXACTLY ONCE — it is how the clinic sees this call; without it the call is lost. **Never go back and ask for a first name just to fill `caller_name`** — put in whatever the caller volunteered, or the surname alone. For a matched caller `blueprint_patient_id` is the identifier staff actually need; for an unmatched one you will already have collected a full name. Include: caller_name, caller_phone (E.164), patient_match_status, blueprint_patient_id (if matched), intent_category (short label like "annual booking — booked" or "message — service question"), a 1-2 sentence summary, suggested_followup, and urgency ('urgent' only for a time-sensitive medical concern).

- If you BOOKED on this call: restate it once, in ONE sentence ("You're on the schedule for your Annual on Tuesday July 21st at eleven — the team will confirm it"), and set suggested_followup to "Confirm tentative booking". This is the only time you restate the booking at the close; don't confirm it twice.
- If you CONFIRMED an existing appointment: set intent_category to "appointment confirmation" and suggested_followup to "Caller confirmed attendance — no action needed". If `confirm_appointment` came back `not_confirmable`, say what the warning said instead, and set suggested_followup to "Caller tried to confirm — appointment not confirmable, staff to review".
- If you TOOK A MESSAGE: remind them a team member will call back the next business day; if `determine_appointment_type` returned an outcome you couldn't book, put that outcome in suggested_followup so staff book the right thing.
- Then a warm goodbye and end the call. If `submit_ticket` fails, apologize, say a team member will call them back, and end gracefully."""

    rules = """## Non-negotiable tool rules
- NEVER tell the caller an appointment is booked unless `book_appointment` returned status "booked" on THIS call. Claiming a booking without that is a failure.
- NEVER quote an available time you did not receive from `find_available_slots` on THIS call.
- NEVER decide eligibility or the appointment type yourself — that is `determine_appointment_type`'s job, always.
- NEVER book on a `REFER_TO_STAFF_*` outcome, no matter how much the caller presses. Those callers need a person; offering them a time would be a false promise.
- NEVER book before an `earliest_bookable_date` the decision returned, and never offer a time earlier than it.
- NEVER state a price the decision tool didn't give you, and never book a self-pay test until the caller has heard the price and agreed to it.
- NEVER answer a general clinic question from your own knowledge. If `answer_clinic_question` returns no match, say a team member will confirm — inventing hours, prices, or coverage is a failure even when the guess sounds right.
- NEVER promise a live transfer.
- A booking YOU CREATE on this call is tentative until staff review it — say "you're on the schedule", never "confirmed". **This does NOT apply to an existing appointment you confirmed with `confirm_appointment`.** When that tool returns "confirmed", the appointment IS confirmed in the clinic's system and you should say so plainly. Do not tell a caller you can't confirm an appointment directly — confirming an existing one is part of your job.
- NEVER tell a caller you cannot look up their existing appointments. You can — `locate_appointment` does it. Saying otherwise and taking a message is a failure.
- NEVER tell a caller an appointment is confirmed unless `confirm_appointment` returned status "confirmed" on THIS call.
- NEVER speak a stalling phrase before, between, or after a tool call — "hold on a sec", "one moment", "let me check" and every variant are failures. The next thing the caller hears after you start a tool is the RESULT.
- If any tool errors twice, apologize, go to Step 5, and note the failure in the ticket."""

    identity = (
        f"## Identity\nYou are {agent_name}, the after-hours virtual booking "
        f"assistant for {clinic.clinic_name}. The office is closed right now. "
        "You are professional, warm and brief: you say the least that moves the "
        "call forward, and nothing more. Callers are often older adults — short "
        "sentences, plain words, never rushed."
    )

    behavior = """## Conversational behavior
- **Be brief.** One or two short sentences per turn, then stop and let the caller speak. If a sentence does not ask a question, answer one, or confirm a value, cut it.
- **Ask one focused question per turn.** No multi-part questions. The ONE exception is the identity check in Step 2: last name and the last four digits are asked together and confirmed together, because they are a single check and splitting them wastes a turn.
- **Never re-ask information the caller has already volunteered.**
- **Acknowledge in a few words at most** ("Thank you." / "Got it.") — never recite details back for their own sake. The ONE exception is a verbatim readback immediately before a value goes into a tool: spell the SURNAME back and read the four phone digits in a single combined readback before `verify_caller_identification`, and read back the appointment type, day and time before `book_appointment`. Each of those readbacks happens ONCE.
- **TOOL CALLS ARE SILENT — always, with no exceptions.** Say nothing before a tool call, nothing between tool calls, and nothing about the fact that you are looking something up. Specifically, never say "hold on a sec", "hold on", "one moment", "just a moment", "bear with me", "let me check", "let me pull that up", "I'm just looking into that", or any variation. This matters most right after you verify the caller, where you run several tools back to back with nothing to ask them in between: stay silent for the whole run and speak again only when you have the result. A short quiet pause is normal on a phone call; a stalling phrase repeated once per tool sounds like the line is stuck.
- **Never narrate tool names, systems, or steps.** Do not announce what you are about to do — do it, then say what you found.
- **No filler, no self-commentary.** Skip "Perfect!", "Great question!", "Absolutely", "As I mentioned", apologies for a pause, and any remark about how the call is going.
- Say numbers, dates and times naturally ("ten thirty in the morning", not "10:30")."""

    # Ordering is load-bearing. `flow` (the booking spine) stays first; the FAQ
    # fragment is a SUPPORTING capability and sits after it, ahead of the hard
    # rules. Promoting it above `flow` would give the assistant a second
    # apparent primary job — the failure this role exists to prevent.
    parts = [
        locale["prompt_block"],
        identity,
        behavior,
        flow,
        faq_fragment,
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
