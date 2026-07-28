"""ACNA appointment-type decision — clinic-scoped protocol wrapping the
deterministic eligibility engine (``api/voice_agent/appointment_decision.py``).

The agent calls ONE tool, ``determine_appointment_type``, after identifying an
existing patient. The server loads the patient's care plan / payer / last-test /
last-clinician-visit from Blueprint_PHI, runs the clinic's declarative decision
table in code, and returns the outcome plus a speakable reason. The LLM never
evaluates a rule.

When the patient has MULTIPLE funded payers on file (e.g. WCB + Veterans
Affairs), the tool returns ``status="need_payer"`` with the options — the agent
asks the caller which program the visit falls under and calls again with
``payer_type`` (clinic decision: ask, don't guess precedence).

Config carries the clinic tunables: the clinician name list (distinguishes
clinician visits from technician C&C in Appointments.practitioner), plan/payer
rules, and the outcome→real_event_type_id booking map. Outcomes with no mapped
type are reported with ``bookable=false`` — the agent takes a message carrying
the recommendation instead of booking (phase-1 posture until the remaining
placeholder pairs are confirmed).
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")

ACNA_CLINIC_ID = "0b5f0929-31fb-4e21-9dd4-030bd040335d"


class AppointmentDecisionConfig(BaseModel):
    """Per-clinic tunables for the decision engine.

    Defaults mirror the confirmed ACNA spec; everything is overridable per
    clinic without code. ``clinician_names`` must match
    ``Blueprint_PHI.Appointments.practitioner`` strings exactly.
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
        default_factory=lambda: [
            "Complete Care Plan",
            "CCP LACE",
            "Care Plan, No Batts",
        ]
    )
    non_qualifying_plan_names: list[str] = Field(default_factory=lambda: ["Pre-Plan"])
    unknown_plan_allows_annual: bool = True
    payer_min_years: dict[str, float] = Field(
        default_factory=lambda: {"WCB": 1.0, "Veterans Affairs": 2.0, "Other": 1.0}
    )
    clinician_visit_threshold_years: float = 1.0
    # outcome → real_event_type_id to book (must have a placeholder pair in the
    # acna_search_availability config). None/missing = not bookable yet → the
    # agent takes a message carrying the recommendation.
    outcome_booking: dict[str, int | None] = Field(
        default_factory=lambda: {
            "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN": 207,
            "BOOK_CLINICIAN_SERVICE_VISIT": None,
            "BOOK_TECHNICIAN_CLEAN_AND_CHECK": None,
            "BOOK_NEW_PATIENT_INTAKE_WITH_CLINICIAN": None,
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
                "based on their care plan, payer, and visit history. Call this "
                "right after verify_caller_identification returns 'matched', "
                "BEFORE searching availability. Returns either "
                "{status:'decided', outcome, reason, real_event_type_id, bookable, "
                "patient_context} or {status:'need_payer', options:[...]} — in the "
                "need_payer case, ask the caller which program the visit falls "
                "under (e.g. WCB claim or Veterans Affairs) and call this tool "
                "again with payer_type set to their answer. patient_context holds "
                "the verified caller's own facts (care plan, last test date, next "
                "funded annual due date, payer program) — use it to answer their "
                "eligibility questions."
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
                        "enum": ["WCB", "Veterans Affairs", "Other"],
                        "description": (
                            "Only set on a SECOND call, after the tool returned "
                            "need_payer and the caller told you which program this "
                            "visit falls under. Omit on the first call."
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

- If it returns `status: "decided"`: relay the `reason` in your own warm words, then:
  - `bookable: true` → proceed to `find_available_slots` using the returned `real_event_type_id`, and book as usual.
  - `bookable: false` → the right appointment type can't be self-booked yet; tell the caller a team member will arrange it, and capture the recommendation in the ticket (`suggested_followup` = the returned `outcome`).
- If it returns `status: "need_payer"`: the patient has more than one funding program on file. Ask which program this visit falls under (offer the returned `options`, e.g. "Is this under your WCB claim, or Veterans Affairs?"), then call the tool again with `payer_type` set to their answer.

**Answering the caller's eligibility questions.** The response's `patient_context` holds facts about THIS verified caller's own record, and you may share them when asked — say dates naturally:
- "What's my care plan?" → `care_plan` (and whether it's active).
- "When was my last test?" → `last_hearing_test`.
- "When am I due?" / "Why not now?" → `next_annual_due` when set ("your next funded annual is available on June 1st, 2027"); if `annual_covered_by_plan` is false, their plan doesn't include funded annuals — suggest staff review options.
- "How often am I allowed?" → `funded_test_interval_years` under their `payer_program` ("under WCB it's every year").
Only share fields from `patient_context` — nothing beyond them — and only with the caller whose identity you verified this call. If a field is null, say the team can check and note the question in the ticket.

Never promise that a test is "covered" beyond what the returned fields say, and never override the outcome — if the caller disagrees or still wants the annual anyway, capture that preference in the ticket for staff to review."""
