"""ACNA appointment-type decision — clinic-scoped protocol wrapping the
deterministic eligibility engine (``api/voice_agent/appointment_decision.py``).

The agent calls ONE tool, ``determine_appointment_type``, after identifying an
existing patient. The server loads the patient's care plan / payer / last-test /
last-clinician-visit from Blueprint_PHI, runs the clinic's declarative decision
table in code, and returns the outcome plus a speakable reason. The LLM never
evaluates a rule.

When the patient has MULTIPLE payer programs on file (e.g. WCB + Alberta Blue
Cross), the tool returns ``status="need_payer"`` with the options — the agent
asks the caller which program the visit falls under and calls again with
``payer_type`` (clinic decision: ask, don't guess precedence). That matters more
now than it did: the programs no longer differ only in interval, they differ in
whether the call is bookable at all.

Config carries the clinic tunables: the clinician name list (distinguishes
clinician visits from technician C&C in Appointments.practitioner), plan/payer
rules, the self-pay price, the minor threshold, the clean-and-check gap, and the
outcome→real_event_type_id booking map. Outcomes with no mapped type are
reported with ``bookable=false`` — the agent takes a message carrying the
recommendation instead of booking. Three outcomes are referrals by design
(prior-authorization, payer review, minor) and are never bookable; the rest are
phase-1 gaps awaiting confirmed placeholder pairs.
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from api.voice_agent.appointment_decision import (
    DEFAULT_CLINICIAN_VISIT_THRESHOLD_YEARS,
    DEFAULT_MIN_MONTHS_AFTER_CLEAN_CHECK,
    DEFAULT_MINOR_AGE_THRESHOLD,
    DEFAULT_NON_QUALIFYING_PLANS,
    DEFAULT_PAYER_ACTIONS,
    DEFAULT_PAYER_MIN_YEARS,
    DEFAULT_QUALIFYING_PLANS,
    DEFAULT_SELF_PAY_ANNUAL_PRICE,
    DEFAULT_WARRANTY_BUNDLE_TRIGGER_MONTHS,
    DEFAULT_WARRANTY_BUNDLE_WINDOW_MONTHS,
)
from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")

ACNA_CLINIC_ID = "0b5f0929-31fb-4e21-9dd4-030bd040335d"


class AppointmentDecisionConfig(BaseModel):
    """Per-clinic tunables for the decision engine.

    Defaults are imported from ``appointment_decision`` rather than re-listed —
    the plan names, payer intervals and payer actions had already drifted into
    two copies, and a config default that disagrees with the engine's is a
    silent rule change. Everything stays overridable per clinic without code.
    ``clinician_names`` must match ``Blueprint_PHI.Appointments.practitioner``
    strings exactly; it doubles as the clinician/technician discriminator, so a
    name missing from it turns that provider's visits into clean-and-checks.
    """

    clinician_names: list[str] = Field(
        default_factory=lambda: [
            "Palmer, Essie",
            "Ohlin, Lisa",
            "Lewchuk, Larena",
            "Roy, Natalie",
            "Andres, Ashlea",
        ]
    )
    qualifying_plan_names: list[str] = Field(
        default_factory=lambda: list(DEFAULT_QUALIFYING_PLANS)
    )
    non_qualifying_plan_names: list[str] = Field(
        default_factory=lambda: list(DEFAULT_NON_QUALIFYING_PLANS)
    )
    unknown_plan_allows_annual: bool = True
    # 2026-08-10 clinic revision: a qualifying plan still covers a funded annual
    # after it expires. False restores expiry-gated coverage.
    expired_qualifying_allows_annual: bool = True
    payer_min_years: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_PAYER_MIN_YEARS)
    )
    # payer bucket → 'fund' | 'prior_auth' | 'human_review'. NIHB/Bigstone need
    # authorization before a test; Blue Cross and AADL rarely cover the care
    # plan, so those callers go to a person.
    payer_actions: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_PAYER_ACTIONS)
    )
    clinician_visit_threshold_years: float = DEFAULT_CLINICIAN_VISIT_THRESHOLD_YEARS
    # Quoted when a due caller's plan doesn't fund the test.
    self_pay_annual_price: float = DEFAULT_SELF_PAY_ANNUAL_PRICE
    minor_age_threshold: int = DEFAULT_MINOR_AGE_THRESHOLD
    min_months_after_clean_check: int = DEFAULT_MIN_MONTHS_AFTER_CLEAN_CHECK
    # Warranty bundling: inside `trigger` months of expiry, delay the annual so
    # it lands within `window` months of expiry and both happen in one visit.
    # Set trigger to 0 to disable.
    warranty_bundle_trigger_months: int = DEFAULT_WARRANTY_BUNDLE_TRIGGER_MONTHS
    warranty_bundle_window_months: int = DEFAULT_WARRANTY_BUNDLE_WINDOW_MONTHS
    # outcome → real_event_type_id to book (must have a placeholder pair in the
    # acna_search_availability config). None/missing = not bookable → the
    # agent takes a message carrying the recommendation.
    outcome_booking: dict[str, int | None] = Field(
        default_factory=lambda: {
            "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN": 207,
            # Self-pay books the same Annual type — the difference is the price
            # conversation, not the appointment.
            "OFFER_SELF_PAY_ANNUAL_HEARING_TEST": 207,
            "BOOK_CLINICIAN_SERVICE_VISIT": None,
            "BOOK_TECHNICIAN_CLEAN_AND_CHECK": None,
            "BOOK_NEW_PATIENT_INTAKE_WITH_CLINICIAN": None,
            # Referral outcomes are never bookable by the agent, by definition.
            "REFER_TO_STAFF_PRIOR_AUTHORIZATION": None,
            "REFER_TO_STAFF_PAYER_REVIEW": None,
            "REFER_TO_STAFF_MINOR": None,
        }
    )

    model_config = {"extra": "ignore"}


class ACNADetermineAppointmentProtocol(Protocol):
    id = "acna_determine_appointment"
    display_name = "Determine Appointment Type (ACNA)"
    description = (
        "Server-side eligibility decision for existing patients: runs the "
        "clinic's care-plan/payer/test-history rules and returns which "
        "appointment type to book (annual, clinician service, technician "
        "clean-and-check, or new-patient intake) with a speakable reason. "
        "The agent narrates the result — it never evaluates the rules itself."
    )
    agent_tool_name = "determine_appointment_type"
    supported_pms = ("blueprint",)
    supported_clinics = (ACNA_CLINIC_ID,)
    depends_on = ("verify_caller_identification",)
    config_model = AppointmentDecisionConfig

    def _tool_url(self) -> str:
        return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/appointment-decision"

    def tools(self) -> list[dict]:
        return [{
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Decide which appointment type this existing patient should book, "
                "based on their care plan, payer, age, and visit history. Call this "
                "right after verify_caller_identification returns 'matched', "
                "BEFORE searching availability. Returns either "
                "{status:'decided', outcome, reason, real_event_type_id, bookable, "
                "earliest_bookable_date, self_pay_price, patient_context} or "
                "{status:'need_payer', options:[...]} — in the need_payer case, ask "
                "the caller which program the visit falls under (e.g. WCB claim or "
                "Blue Cross) and call this tool again with payer_type set to their "
                "answer. Some outcomes are referrals that must NOT be booked; some "
                "are self-pay offers that need the caller's agreement on price "
                "first. earliest_bookable_date, when present, is the earliest date "
                "a booking may be placed. patient_context holds the verified "
                "caller's own facts (care plan, last test date, next covered test "
                "date, payer program, how often they're covered) — use it to answer "
                "their questions about their plan."
            ),
            "url": self._tool_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "patient_id": {
                        "type": "string",
                        "description": (
                            "The opaque patient_id from verify_caller_identification "
                            "(status='matched'). Required."
                        ),
                    },
                    "payer_type": {
                        "type": "string",
                        "enum": [
                            "WCB", "Veterans Affairs", "Alberta", "NIHB",
                            "Blue Cross", "AADL", "Other",
                        ],
                        "description": (
                            "Only set on a SECOND call, after the tool returned "
                            "need_payer and the caller told you which program this "
                            "visit falls under. Must be one of the values the tool "
                            "listed in `options` — map the caller's words onto them "
                            "(\"my Blue Cross\" → 'Blue Cross', \"Bigstone\" → "
                            "'NIHB'). Omit on the first call."
                        ),
                    },
                },
                "required": ["patient_id"],
            },
        }]

    @property
    def prompt_fragment(self) -> str:
        return """## Determine Appointment Type
For an EXISTING patient who wants to book (an annual, a service visit, or is unsure what they need), call `determine_appointment_type` with their `patient_id` right after identity is verified — BEFORE searching availability. The clinic's own rules decide the right appointment; never guess or reason about eligibility yourself.

- If it returns `status: "need_payer"`: the patient has more than one program on file. Ask which one this visit falls under (offer the returned `options`, e.g. "Is this under your WCB claim, or your Blue Cross?"), then call the tool again with `payer_type` set to their answer.
- If it returns `status: "decided"`: relay the `reason` in your own warm words, then follow the `outcome`:

### Outcome handling
- **`BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN`** — covered by their plan. Go to `find_available_slots` with the returned `real_event_type_id` and book as usual.
- **`OFFER_SELF_PAY_ANNUAL_HEARING_TEST`** — they're due, but no plan funds it. Tell them warmly what it costs (the `reason` already names the price) and ASK whether they'd like to go ahead: "Would you like me to get you booked in for that?"
  - If they say yes → proceed exactly as a normal booking with the returned `real_event_type_id`.
  - If they hesitate, want to think about it, or ask about other options → do NOT push and do NOT book. Take a message, and set `suggested_followup` to "Quoted self-pay hearing test — caller wants to consider it".
- **`REFER_TO_STAFF_PRIOR_AUTHORIZATION`** / **`REFER_TO_STAFF_PAYER_REVIEW`** / **`REFER_TO_STAFF_MINOR`** — this call is NOT bookable, whatever the caller asks. Relay the `reason` kindly, tell them a team member will call them back to arrange it, and take a message with `suggested_followup` set to the returned `outcome`. Never offer times, and never imply the appointment is being held.
- **Any other outcome with `bookable: false`** — the right appointment type can't be self-booked; tell the caller a team member will arrange it and put the returned `outcome` in `suggested_followup`.

### Date constraints — one is hard, one is negotiable
Don't confuse these. They come back as separate fields for exactly that reason.

**`earliest_bookable_date` — HARD.** The test cannot be scheduled before this date (the clinic requires a gap after a recent clean-and-check). Start your `find_available_slots` window ON that date, never before it, and never offer an earlier time even if one appears. If the caller pushes for sooner, explain it's a clinic requirement and offer the earliest date you legitimately can.

**`preferred_window` — SOFT.** When present, the caller's hearing aids are coming out of warranty and the clinic would rather do the test and the warranty check in ONE visit than see them twice. Search `preferred_window.start` → `preferred_window.end` FIRST and offer those times, saying why in plain language: "your hearing aids come out of warranty in October, so it's best to do your test and a device check at the same visit — could we look at early September?"
- If the caller accepts a time in the window, book it.
- If nothing in the window works — they're away, they want it sooner, none of the times suit — **book what does work.** This is a preference, never a refusal. Then set `suggested_followup` to note they'll need a separate warranty check ("Booked outside warranty window — caller unavailable; separate warranty check needed").
- Never tell the caller you can't see them until the window. You can.

**Answering questions about their plan.** The clinic's eligibility RULES are internal — never recite thresholds, intervals, or rule logic as policy. What you may do, when a verified caller asks about their own hearing plan, is tell them WHAT IS COVERED and HOW OFTEN, from `patient_context`:
- "What's my care plan?" / "What does my plan cover?" → `care_plan`, plus whether it covers a hearing test (`annual_covered_by_plan`).
- "How often am I allowed?" → `funded_test_interval_years` under their `payer_program` ("under WCB it's once a year").
- "When was my last test?" → `last_hearing_test`.
- "When am I due?" / "Why not now?" → `next_annual_due` when set ("your next covered test is available on June 1st, 2027"); if `annual_covered_by_plan` is false, their plan doesn't include a covered test — mention staff can review the options with them.
Say dates and prices naturally. Only share fields from `patient_context` — nothing beyond them — and only with the caller whose identity you verified this call. If a field is null, say the team can check and note the question in the ticket.

Never promise that a test is "covered" beyond what the returned fields say, and never override the outcome — if the caller disagrees or still wants the annual anyway, capture that preference in the ticket for staff to review."""
