"""
Build a VAPI assistant configuration for a clinic.

Reads from Cloud SQL ORM:
  - Clinic + ClinicLocationDetails — name, address, hours, time_zone, country
  - ClinicProtocol rows             — which toggleable protocols are enabled
    (replaces the legacy ``voice_agent_capabilities`` table; the legacy
    table is still dual-written by the toggle endpoint, but reads here
    come from ``clinic_protocols`` only)

The assembled config is a dict shaped for ``vapi.client.Vapi.assistants.create()``
(snake_case kwargs). Voice settings, model, and transcriber model are hardcoded
for v1 — revisit if a clinic asks for variation.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.orm import (
    Clinic, ClinicProtocol, ClinicVoiceAgentCallerBucket,
    ClinicVoiceAgentPersona, ClinicVoiceAgentQualifyingQuestion,
    ClinicVoiceAgentScript,
)
from api.core.secrets import get_secret
from api.voice_agent.protocols import (
    PROTOCOL_REGISTRY,
    Protocol,
    SubmitTicketProtocol,
    VerifyCallerIdentificationProtocol,
    unmet_dependencies,
)
from api.voice_agent.defaults import (
    DEFAULT_AGENT_NAME as _DEFAULT_AGENT_NAME,
    DEFAULT_AGENT_TITLE as _DEFAULT_AGENT_TITLE,
    DEFAULT_AI_MODEL as _DEFAULT_AI_MODEL,
    DEFAULT_CALLER_BUCKETS as _DEFAULT_CALLER_BUCKETS,
    DEFAULT_VOICE_ID as _DEFAULT_VOICE_ID,
    interpolate as _interpolate,
)
from api.voice_agent.locale import resolve as resolve_locale


log = logging.getLogger(__name__)


def _persona_or_defaults(persona: ClinicVoiceAgentPersona | None) -> tuple[str, str, str, str | None, str]:
    """Return ``(agent_name, agent_title, voice_id, first_message, ai_model)``
    falling back to system defaults when the persona row is missing or
    individual fields are blank/null. ``first_message`` stays None when not
    overridden so the caller can render a templated default.
    """
    if persona is None:
        return (_DEFAULT_AGENT_NAME, _DEFAULT_AGENT_TITLE, _DEFAULT_VOICE_ID,
                None, _DEFAULT_AI_MODEL)
    return (
        (persona.agent_name or _DEFAULT_AGENT_NAME).strip(),
        (persona.agent_title or _DEFAULT_AGENT_TITLE).strip(),
        (persona.voice_id or _DEFAULT_VOICE_ID).strip(),
        (persona.first_message.strip() if persona.first_message and persona.first_message.strip() else None),
        (persona.ai_model or _DEFAULT_AI_MODEL).strip(),
    )


def build_first_message(
    clinic_name: str,
    persona: ClinicVoiceAgentPersona | None = None,
) -> str:
    """Greeting played at call start. Honors ``persona.first_message`` when
    set (with placeholder interpolation); otherwise renders the templated
    default with persona overrides for agent name + title.
    """
    name, title, _voice, override, _model = _persona_or_defaults(persona)
    if override:
        return _interpolate(override, clinic_name=clinic_name, agent_name=name, agent_title=title)
    return (
        f"Thank you for calling {clinic_name}. My name is {name}, "
        f"your {title}. How can I help you today?"
    )


def _vapi_credential_id() -> str:
    """VAPI ``apiRequest`` tools authenticate via a credentialId that injects
    ``X-Vapi-Secret`` on the call. The credential is created once per VAPI org
    (not per clinic); its ID is stored in SM."""
    return get_secret("vapi-cortex-credential-id")


def _instantiate_capabilities(
    clinic: Clinic,
    enabled_capability_ids: list[str],
    credential_id: str,
) -> list[Protocol]:
    """
    Build the ordered list of Protocol instances for this clinic.

    Order:
      1. Toggleable capabilities (in PROTOCOL_REGISTRY declaration order) —
         only those whose ID is in enabled_capability_ids AND whose
         supported_pms includes this clinic's pms_type AND whose
         ``depends_on`` set is fully covered by other enabled ids.
      2. Always-on capabilities (e.g. SubmitTicketProtocol) appended last so the
         "closing & ticket submission" block is at the end of the prompt.

    A protocol whose deps aren't met is **skipped with a warning** rather
    than included with broken references. The toggle row stays "on" — the
    operator's intent is preserved; the agent just doesn't get a tool +
    prompt fragment that would crash on use. The admin UI surfaces the
    same unmet-deps list from the GET /capabilities endpoint.
    """
    enabled = set(enabled_capability_ids)
    instantiated: list[Protocol] = []

    def make(cls: type[Protocol]) -> Protocol:
        return cls(
            clinic_id=clinic.clinic_id,
            clinic_name=clinic.clinic_name,
            pms_type=clinic.pms_type or "none",
            credential_id=credential_id,
        )

    # Toggleable first, in registry order
    for cap_id, cls in PROTOCOL_REGISTRY.items():
        if cls.always_on:
            continue
        if cap_id not in enabled:
            continue
        unmet = unmet_dependencies(cap_id, enabled)
        if unmet:
            log.warning(
                "Skipping protocol %s for clinic_id=%s — unmet deps: %s",
                cap_id, clinic.clinic_id, unmet,
            )
            continue
        try:
            instantiated.append(make(cls))
        except ValueError as e:
            log.warning(
                "Skipping enabled capability %s for clinic_id=%s: %s",
                cap_id, clinic.clinic_id, e,
            )

    # Always-on last
    for cls in PROTOCOL_REGISTRY.values():
        if not cls.always_on:
            continue
        try:
            instantiated.append(make(cls))
        except ValueError as e:
            # An always-on capability that refuses this clinic is a spec bug.
            raise RuntimeError(
                f"Always-on capability {cls.__name__} refused clinic: {e}"
            ) from e

    if not any(isinstance(c, SubmitTicketProtocol) for c in instantiated):
        raise RuntimeError(
            "No SubmitTicketProtocol capability instantiated — assistant cannot persist call outcomes."
        )

    return instantiated


def _hours_block(clinic: Clinic) -> str:
    loc = clinic.location
    if loc is None:
        return ""
    days = [
        ("Monday", loc.hours_monday),
        ("Tuesday", loc.hours_tuesday),
        ("Wednesday", loc.hours_wednesday),
        ("Thursday", loc.hours_thursday),
        ("Friday", loc.hours_friday),
        ("Saturday", loc.hours_saturday),
        ("Sunday", loc.hours_sunday),
    ]
    rendered = "\n".join(f"- {d}: {h or 'Closed'}" for d, h in days)
    return f"## Hours of Operation\n{rendered}"


def _capability_by_id(caps: list[Protocol], capability_id: str) -> Protocol | None:
    for c in caps:
        if c.id == capability_id:
            return c
    return None


def _identity_section(clinic: Clinic) -> str:
    return (
        "## Identity\n"
        f"- You are the receptionist at {clinic.clinic_name}. "
        "Your job is to help the right patients get in and to ensure every "
        "patient who does come in is ready to take action on their hearing health."
    )


def _task_overview_section() -> str:
    return (
        "## Task Overview\n"
        "- The call proceeds through a sequence of stages. Each stage has explicit "
        "goals and rules. Your job is to achieve the goals of the current stage, "
        "then move on — not to recite a script line-by-line.\n"
        "- Quoted phrases below are *examples* you may use; phrase them in your "
        "own words when the conversation calls for it. Unquoted text is instruction.\n"
        "- Items marked **[info collection point]** must end up in the ticket. "
        "Capture the value when the caller says it — acknowledge naturally so they "
        "feel heard. Verbatim readback isn't required at the capture moment; it's "
        "required at the use moment (see Conversational behavior below)."
    )


def _behavior_rules_section() -> str:
    """Cross-cutting rules that apply to every stage. Placed early in the
    prompt so the model carries them into every response.
    """
    return (
        "## Conversational behavior (applies to every stage)\n"
        "- **Never re-ask information the caller has already volunteered.** "
        "Listen to what they said and use it. If they opened with their name "
        "and reason, do not then ask \"who am I speaking with?\" or \"how can "
        "I help you?\"\n"
        "- **Acknowledge briefly so the caller feels heard — do NOT read back "
        "every piece of information verbatim.** A natural \"got it\" or a "
        "one-sentence paraphrase is enough most of the time. Overusing verbatim "
        "readback makes the call feel slow and robotic. Examples of the right "
        "default acknowledgement:\n"
        "  - Caller: \"Hi, this is John.\" → You: \"Hi John — what can I help "
        "you with today?\" (not: \"John, did I get that right?\")\n"
        "  - Caller: \"I've been having trouble at family dinners.\" → You: "
        "\"So it's been tough following conversations in groups — got it.\" "
        "(paraphrase, not verbatim readback)\n"
        "- **Verbatim readback IS required when a value is about to be passed "
        "to a tool — a patient lookup, a booking, a cancel, or a reschedule.** "
        "The act-on protocols' prompts (Verify Caller Identification, Book "
        "Appointment, Cancel Appointment, Reschedule Appointment) tell you "
        "exactly when. In summary:\n"
        "  - Before `verify_caller_identification`: spell back first + last "
        "name letter-by-letter; read back the last 4 phone digits.\n"
        "  - Before `book_appointment` / `reschedule_appointment`: read back "
        "the appointment type, day, and time. For new-patient bookings, "
        "spell-back the name (it's about to be used to create a patient "
        "record).\n"
        "  - Before `cancel_appointment`: read back which appointment will be "
        "cancelled.\n"
        "  At the capture moment (Stage 1, Stage 3) you are NOT yet using the "
        "value — acknowledge naturally and move on; readback happens later if "
        "and when a tool needs the value.\n"
        "- **Ask one focused question per turn.** No multi-part interrogations.\n"
        "- **Tool calls happen quietly.** You may say once \"let me find you "
        "in our system\" before a lookup, but do not narrate every tool call."
    )


def _stage_1_greeting_and_reason(
    clinic: Clinic,
    persona: ClinicVoiceAgentPersona | None,
) -> str:
    """Stage 1 — Greeting & Reason.

    Hardcoded goals + rules. The agent decides how to phrase the actual
    opener based on what the caller says.
    """
    agent_name, agent_title, _voice, _first, _model = _persona_or_defaults(persona)

    out = [
        "## Stage 1 — Greeting & Reason",
        "**Goals**",
        "1. Warmly greet the caller. Identify yourself by name and role.",
        "2. Learn the caller's name **[info collection point]** "
        "(or capture it if volunteered). Acknowledge it naturally — "
        "verbatim spell-back is deferred to the Verify Caller "
        "Identification step where the name is actually used for a lookup.",
        "3. Learn the reason for the call (or capture it if volunteered, "
        "then briefly paraphrase to confirm).",
        "",
        "**Rules**",
        "- Open with a brief thanks (\"Thank you for calling\") then identify "
        f"yourself: **{agent_name}**, the **{agent_title}** at "
        f"**{clinic.clinic_name}**. Do not repeat the clinic name in the "
        "thanking phrase — the caller already knows who they called.",
        "- Pick the single most natural next question based on what the caller "
        "has said so far. Do not run through a fixed list.",
        "- If the caller has already given their name and/or reason, skip "
        "those questions — a natural acknowledgement or short paraphrase is "
        "enough; do not read their name back verbatim at this stage.",
    ]
    return "\n".join(out)


def _stage_2_identity(caps: list[Protocol]) -> str:
    """Stage 2 — Identity (new vs existing + lookup + callback number)."""
    verify = _capability_by_id(caps, VerifyCallerIdentificationProtocol.id)
    existing_lookup_block = (
        verify.prompt_fragment if verify
        else "_(Verify Caller Identification protocol not enabled for this clinic.)_"
    )
    return (
        "## Stage 2 — Identity\n"
        "**Goals**\n"
        "1. Determine whether the caller is **new** or **existing** "
        "**[info collection point]**.\n"
        "2. If existing → silently call the Lookup Patient capability "
        "(see the capability block below). Confirm the match by name.\n"
        "3. Collect the **callback number** **[info collection point]**. "
        "The caller-ID number is a hint, not gospel — ask if it's the "
        "best number to reach them. Acknowledge naturally; do not read it "
        "back digit-by-digit at this stage. (If the number is later used "
        "as input to a tool — none of the current protocols do — readback "
        "would apply then.)\n"
        "\n"
        "**Rules**\n"
        "- If the caller already volunteered new-vs-existing in Stage 1, do "
        "not ask again — just acknowledge (\"got it, you're an existing patient\").\n"
        "- A failed or ambiguous lookup is fine — proceed and let the staff "
        "resolve identity later. Don't quiz the caller.\n"
        "\n"
        f"{existing_lookup_block}"
    )


def _render_caller_buckets(buckets: list[ClinicVoiceAgentCallerBucket]) -> str:
    """Render either the clinic's own active buckets (sorted by ordinal) or
    the hardcoded defaults when no DB rows exist. Output is the numbered
    block consumed by the New Patient Flow section.
    """
    active = [b for b in buckets if b.active] if buckets else []
    if active:
        active.sort(key=lambda b: (b.ordinal, b.id))
        items = [(b.label, b.example_phrases or "", b.canned_response or "") for b in active]
    else:
        items = list(_DEFAULT_CALLER_BUCKETS)

    blocks: list[str] = []
    for i, (label, phrases, response) in enumerate(items, start=1):
        phrases_block = (phrases or "").strip()
        response_block = (response or "").strip()
        block = f"{i}) {label}:"
        if phrases_block:
            block += "\nExample phrases:\n" + phrases_block
        if response_block:
            block += "\nResponse:\n" + response_block
        blocks.append(block)
    return "\n\n".join(blocks)


def _render_qualifying_questions(
    questions: list[ClinicVoiceAgentQualifyingQuestion],
) -> str:
    """Render the clinic's active new-patient screening questions as a
    numbered block, sorted by ``ordinal``. Returns '' when the clinic has
    none — unlike caller buckets there is no hardcoded default set, so an
    unconfigured clinic simply gets no screening block.
    """
    active = [q for q in questions if q.active] if questions else []
    if not active:
        return ""
    active.sort(key=lambda q: (q.ordinal, q.id))
    # Drop blank rows before numbering so the displayed numbers stay
    # contiguous (1, 2, 3 …) even if an active row has empty text.
    valid: list[tuple[str, str]] = []
    for q in active:
        text = (q.question_text or "").strip()
        if not text:
            continue
        valid.append((text, (q.expected_responses or "").strip()))
    blocks: list[str] = []
    for i, (text, expected) in enumerate(valid, start=1):
        block = f"{i}) {text}"
        if expected:
            # Collapse multi-line guidance (the dashboard field is a textarea)
            # onto one indented line so it can't run into the next numbered
            # question.
            expected_oneline = "; ".join(
                ln.strip() for ln in expected.splitlines() if ln.strip()
            )
            block += f"\n   (expected responses: {expected_oneline})"
        blocks.append(block)
    return "\n".join(blocks)


def _stage_3a_new_patient(
    buckets: list[ClinicVoiceAgentCallerBucket],
    questions: list[ClinicVoiceAgentQualifyingQuestion] | None = None,
) -> str:
    """Stage 3a — New Patient Discovery (only runs if Stage 2 = new)."""
    rendered_buckets = _render_caller_buckets(buckets)
    rendered_questions = _render_qualifying_questions(questions or [])
    out = [
        "## Stage 3a — New Patient Discovery (only if new)",
        "**Goals**",
        "1. Understand the caller's motivation **[info collection point]**.",
        "2. Categorize the motivation into one of the configured caller buckets.",
        "3. Deliver the bucket's response in your own warm voice — not as a recital.",
        "",
        "**Rules**",
        "- Open the discovery by learning what's prompting the caller to look "
        "into their hearing health, unless they've already given enough "
        "motivation context to skip straight to categorization.",
        "- Categorization is internal — never name the bucket out loud "
        "(\"I'll categorize you as a price shopper\" is wrong).",
        "- The bucket response is *guidance for what to convey*. Adapt phrasing "
        "to fit the conversation; do not read it verbatim.",
    ]
    out.extend([
        "",
        "**Caller buckets — once you understand the caller's motivation, "
        "categorize it internally then respond:**",
        "",
        rendered_buckets,
    ])
    if rendered_questions:
        out.extend([
            "",
            "**New-patient screening questions — ask these during discovery:**",
            "Work each one into the conversation (one at a time, in your own "
            "words), unless the caller has already answered it. Keep track of "
            "every answer — you must carry the question→answer pairs forward:",
            "- If the caller goes on to book, include them in the "
            "`book_appointment` `notes` as plain text, one `Question: answer` "
            "per line under a `New-patient screening:` header.",
            "- If the caller does NOT book, put them in the `submit_ticket` "
            "`details.screening_answers` field as a list of "
            "`{question, answer}` objects.",
            "",
            rendered_questions,
        ])
    return "\n".join(out)


def _stage_3b_existing_patient(script: ClinicVoiceAgentScript | None) -> str:
    """Stage 3b — Existing Patient Service (only runs if Stage 2 = existing)."""
    extra = (script.existing_patient_intro or "").strip() if script else ""
    out = [
        "## Stage 3b — Existing Patient Service (only if existing)",
        "**Goals**",
        "1. Surface the specific need (booking, troubleshooting, billing, supplies, etc.) "
        "**[info collection point]**.",
        "2. Capture the details staff will need: aid model if known, urgency, "
        "preferred time window. **[info collection point]**",
        "",
        "**Rules**",
        "- If the caller already named their need in Stage 1, do not ask "
        "\"what can I help you with?\" — go straight to capturing details.",
        "- One focused follow-up at a time. Acknowledge details as the "
        "caller mentions them; verbatim readback of dates/times waits "
        "until you're about to pass them to a booking or reschedule tool.",
    ]
    if extra:
        out.extend([
            "",
            "**Clinic-specific guidance for existing patients:**",
            extra,
        ])
    return "\n".join(out)


def _stage_4_capture_and_close() -> str:
    """Stage 4 — Capture & Close. Fully hardcoded for v1; a dedicated
    ``closing_notes`` column can be added later if clinics need to customize.
    """
    return (
        "## Stage 4 — Capture & Close\n"
        "**Goals**\n"
        "1. Make sure you have the best callback number for them. Ask if "
        "it's the right number rather than reciting digits back.\n"
        "2. Briefly summarize what you understood — give the caller a chance "
        "to correct anything.\n"
        "3. Submit the ticket via the ``submit_ticket`` capability.\n"
        "4. Warm close. Thank them for calling.\n"
        "\n"
        "**Rules**\n"
        "- The summary should be 1–2 sentences. Don't read back every field.\n"
        "- ``submit_ticket`` is called silently after the summary is confirmed.\n"
        "- Never end the call without calling ``submit_ticket`` if there is any "
        "actionable information to record."
    )


def _trailing_capability_blocks(
    caps: list[Protocol],
    inlined_ids: set[str],
) -> list[str]:
    """Protocol fragments not already inlined upstream.

    Toggleable fragments first (in registry order), always-on last so
    SubmitTicketProtocol's closing instructions sit at the very end of the prompt.
    """
    toggleable = [
        c.prompt_fragment for c in caps
        if not c.always_on and c.id not in inlined_ids
    ]
    always_on = [
        c.prompt_fragment for c in caps
        if c.always_on and c.id not in inlined_ids
    ]
    return toggleable + always_on


def _script_section(script: ClinicVoiceAgentScript | None) -> str:
    """Render the clinic-editable scope-of-practice script into a prompt block.

    Empty / null row → empty string (the block is dropped from the prompt).
    Only populated fields are emitted, each as a sub-heading. Order is
    deliberate: scope first (the broadest bound), then explicit
    offered/not-offered lists, then caller-need categories, then misc notes.
    """
    if script is None:
        return ""
    fields = [
        ("Scope of practice",    script.scope_of_practice),
        ("Services NOT offered", script.services_not_offered),
        ("Additional notes",     script.additional_notes),
    ]
    blocks = []
    for label, value in fields:
        v = (value or "").strip()
        if v:
            blocks.append(f"### {label}\n{v}")
    if not blocks:
        return ""
    return "## Scope of practice\n\n" + "\n\n".join(blocks)


def build_system_prompt(
    clinic: Clinic,
    caps: list[Protocol],
    locale: dict,
    script: ClinicVoiceAgentScript | None = None,
    persona: ClinicVoiceAgentPersona | None = None,
    caller_buckets: list[ClinicVoiceAgentCallerBucket] | None = None,
    qualifying_questions: list[ClinicVoiceAgentQualifyingQuestion] | None = None,
) -> str:
    inlined_ids = {VerifyCallerIdentificationProtocol.id}
    buckets = caller_buckets or []
    questions = qualifying_questions or []
    parts = [
        locale["prompt_block"],
        _identity_section(clinic),
        _task_overview_section(),
        _behavior_rules_section(),
        _script_section(script),
        _hours_block(clinic),
        _stage_1_greeting_and_reason(clinic, persona),
        _stage_2_identity(caps),
        _stage_3a_new_patient(buckets, questions),
        _stage_3b_existing_patient(script),
        _stage_4_capture_and_close(),
        *_trailing_capability_blocks(caps, inlined_ids),
    ]
    return "\n\n".join(p for p in parts if p)


def _enabled_capability_ids(db: Session, clinic_id: str) -> list[str]:
    """Enabled protocol IDs for a clinic.

    Reads from ``clinic_protocols`` (the post-step-3 source of truth).
    The legacy table is still dual-written by the toggle endpoint, but
    this query intentionally does not consult it — if rollback is needed,
    revert the code; the data stays consistent because of dual-write.
    """
    rows = db.scalars(
        select(ClinicProtocol).where(
            ClinicProtocol.clinic_id == clinic_id,
            ClinicProtocol.enabled.is_(True),
        )
    ).all()
    return [r.protocol_id for r in rows]


def build_agent_config(db: Session, clinic: Clinic) -> dict:
    """
    Returns a complete VAPI assistant creation payload for the given clinic.

    Reads enabled capability IDs from Cloud SQL. The clinic ORM must have its
    `location` relationship loaded (the default lazy load suffices when
    accessed inside the same session).

    Returns a dict suitable for ``client.assistants.create(**config)``.
    """
    locale = resolve_locale(clinic)
    credential_id = _vapi_credential_id()
    enabled_ids = _enabled_capability_ids(db, clinic.clinic_id)
    caps = _instantiate_capabilities(clinic, enabled_ids, credential_id)
    script = db.get(ClinicVoiceAgentScript, clinic.clinic_id)
    persona = db.get(ClinicVoiceAgentPersona, clinic.clinic_id)
    caller_buckets = list(db.scalars(
        select(ClinicVoiceAgentCallerBucket)
        .where(ClinicVoiceAgentCallerBucket.clinic_id == clinic.clinic_id)
        .order_by(ClinicVoiceAgentCallerBucket.ordinal.asc(),
                  ClinicVoiceAgentCallerBucket.id.asc())
    ))
    qualifying_questions = list(db.scalars(
        select(ClinicVoiceAgentQualifyingQuestion)
        .where(ClinicVoiceAgentQualifyingQuestion.clinic_id == clinic.clinic_id)
        .order_by(ClinicVoiceAgentQualifyingQuestion.ordinal.asc(),
                  ClinicVoiceAgentQualifyingQuestion.id.asc())
    ))

    system_prompt = build_system_prompt(
        clinic, caps, locale, script,
        persona=persona, caller_buckets=caller_buckets,
        qualifying_questions=qualifying_questions,
    )
    # Flatten: each protocol contributes 1+ VAPI tools. Single-tool ports
    # return a 1-element list; future multi-tool protocols return several.
    tools = [t for c in caps for t in c.tools()]

    _name, _title, voice_id, _override, ai_model = _persona_or_defaults(persona)

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
