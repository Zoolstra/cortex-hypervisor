"""
Canonical voice-agent defaults — single source of truth.

Used in two places:
  1. ``factory.py`` — runtime fallback when a clinic's DB row is missing or
     a column is null. Keeps unseeded clinics working.
  2. ``seed_voice_agent_defaults()`` — writes these same defaults as visible,
     editable DB rows when a clinic is provisioned (or backfilled).

Stored templates support ``{clinic_name}``, ``{agent_name}``, and
``{agent_title}`` placeholders. ``interpolate()`` does a safe ``str.replace``
substitution (not ``str.format``) so stray ``{`` / ``}`` in user-edited text
never raise.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from api.core.orm import (
    ClinicVoiceAgentCallerBucket,
    ClinicVoiceAgentPersona,
    ClinicVoiceAgentScript,
)


# ── Persona ──────────────────────────────────────────────────────────────────

DEFAULT_AGENT_NAME = "Emma"
DEFAULT_AGENT_TITLE = "virtual hearing assistant"
DEFAULT_VOICE_ID = "Emma"
DEFAULT_AI_MODEL = "gpt-4o"


# ── Script extensions ────────────────────────────────────────────────────────

# Existing-patient guidance is intentionally trimmed to the clinic-UNIQUE
# value: the list of common call reasons. The capture list (preferred
# window / urgency / aid model), the "what can I help you with" opener, and
# the callback-number confirmation all already live in the hardcoded Stage 3b
# goals/rules and Stage 4 — duplicating them here was redundant.
DEFAULT_EXISTING_PATIENT_INTRO = (
    "Common reasons existing patients call:\n"
    "- Booking a follow-up, repair, or wax-removal appointment\n"
    "- Hearing aid troubleshooting (battery, fit, connectivity)\n"
    "- Insurance, billing, or pricing question\n"
    "- Refill or supply request (batteries, domes, wax guards)"
)


# ── Caller buckets ───────────────────────────────────────────────────────────

# (label, example_phrases, canned_response)
DEFAULT_CALLER_BUCKETS: tuple[tuple[str, str, str], ...] = (
    (
        "Motivated Patient",
        "I have been struggling to follow conversations\n"
        "It is affecting my work\n"
        "My family has been frustrated for a while",
        "Reply with genuine empathy and validate their concerns.",
    ),
    (
        "Price Shopper",
        "I heard you can get them cheaper\n"
        "How much are your hearing aids\n"
        "I was at Costco looking",
        "I understand. What we do is quite different from retail. "
        "Our audiologists run a full diagnostic evaluation — not just a "
        "screening, but an assessment of how your ears and brain process "
        "sound together. Devices are then programmed to your results using "
        "real-ear measurement, calibrated to your anatomy. That precision "
        "is what delivers hearing that's genuinely clear, not just louder. "
        "If that level of clinical care is what you're looking for — we "
        "would love to have you in.",
    ),
    (
        "Test-Only / Doctor Referral",
        "My doctor told me to get a hearing test\n"
        "I just want to know if I have hearing loss\n"
        "I don't necessarily need hearing aids\n"
        "Can I just get a quick test",
        "Of course — understanding your hearing is exactly the right "
        "starting point. I want to make sure you know what our evaluation "
        "includes so you can decide if it's the right fit. Our audiologists "
        "conduct a comprehensive diagnostic assessment — typically 60 to 90 "
        "minutes. At the end you have a complete picture and a clear "
        "recommendation. \"This is a clinical appointment, not a drop-in "
        "test. We ask patients to come in committed to the full process so "
        "our audiologists can give you the attention your hearing health "
        "deserves. Does that sound like what you're looking for?\" "
        "If they want a quick screen only: \"For a brief screening your "
        "GP's office may be a better starting point — and you can always "
        "come back to us when you're ready for the full clinical picture.\"",
    ),
)


# ── Placeholder interpolation ────────────────────────────────────────────────

def interpolate(text: str, *, clinic_name: str, agent_name: str, agent_title: str) -> str:
    """Substitute the three supported placeholders into a stored template.

    Uses ``str.replace`` rather than ``str.format`` so stray braces in
    user-edited content never raise a KeyError / IndexError.
    """
    if not text:
        return text
    return (
        text.replace("{clinic_name}", clinic_name)
            .replace("{agent_name}", agent_name)
            .replace("{agent_title}", agent_title)
    )


# ── Seeding ──────────────────────────────────────────────────────────────────

def seed_voice_agent_defaults(
    db: Session,
    clinic_id: str,
    *,
    only_if_missing: bool = True,
) -> dict:
    """Insert the canonical voice-agent default rows for a clinic.

    Idempotent when ``only_if_missing=True`` (the default): existing persona /
    script row is left untouched, and caller buckets are inserted only if the
    clinic has zero rows. Safe to run against already-provisioned clinics.

    Returns a summary dict ``{"persona": "created"|"existed", "script": ...,
    "buckets": <int rows added>}`` — handy for the backfill script's log.
    """
    summary = {"persona": "existed", "script": "existed", "buckets": 0}

    # Persona (1:1)
    persona = db.get(ClinicVoiceAgentPersona, clinic_id)
    if persona is None:
        db.add(ClinicVoiceAgentPersona(
            clinic_id=clinic_id,
            agent_name=DEFAULT_AGENT_NAME,
            agent_title=DEFAULT_AGENT_TITLE,
            voice_id=DEFAULT_VOICE_ID,
            ai_model=DEFAULT_AI_MODEL,
            first_message=None,
        ))
        summary["persona"] = "created"

    # Script (1:1) — seed only the existing-patient guidance. The clinic-
    # specific fields (scope_of_practice etc.) stay null until an admin
    # fills them in.
    script = db.get(ClinicVoiceAgentScript, clinic_id)
    if script is None:
        db.add(ClinicVoiceAgentScript(
            clinic_id=clinic_id,
            existing_patient_intro=DEFAULT_EXISTING_PATIENT_INTRO,
        ))
        summary["script"] = "created"
    elif not only_if_missing:
        script.existing_patient_intro = DEFAULT_EXISTING_PATIENT_INTRO
        summary["script"] = "overwritten"
    else:
        # Backfill existing_patient_intro if it's still null on an existing row.
        if not script.existing_patient_intro:
            script.existing_patient_intro = DEFAULT_EXISTING_PATIENT_INTRO
            summary["script"] = "extensions_filled"

    # Caller buckets — only insert defaults when the clinic has zero rows,
    # to avoid duplicating after an admin has customised the list.
    existing_count = (
        db.query(ClinicVoiceAgentCallerBucket)
          .filter(ClinicVoiceAgentCallerBucket.clinic_id == clinic_id)
          .count()
    )
    if existing_count == 0:
        for idx, (label, phrases, response) in enumerate(DEFAULT_CALLER_BUCKETS):
            db.add(ClinicVoiceAgentCallerBucket(
                clinic_id=clinic_id,
                ordinal=idx,
                label=label,
                example_phrases=phrases,
                canned_response=response,
                active=True,
            ))
        summary["buckets"] = len(DEFAULT_CALLER_BUCKETS)

    db.flush()
    return summary
