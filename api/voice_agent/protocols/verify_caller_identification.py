"""VerifyCallerIdentification — confirm an existing patient by name + last4-phone (+ DOB).

Replaces the legacy ``PatientMatchProtocol`` (id: ``patient_match``). The
backend endpoint URL stays ``/blueprint/{clinic_id}/patient/match`` —
only the protocol id and the agent-facing tool name change, so the
hypervisor route, the BQ ticket schema field (``patient_match_status``),
and the verification logic are all unaffected.
"""
from __future__ import annotations

import os

from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")


class VerifyCallerIdentificationProtocol(Protocol):
    id = "verify_caller_identification"
    display_name = "Verify Caller Identification"
    description = (
        "Confirm an existing patient by first + last name and the last 4 digits "
        "of the phone number on file. Requires the patient record to already "
        "exist in the PMS."
    )
    agent_tool_name = "verify_caller_identification"
    supported_pms = ("blueprint", "audit_data")

    def _tool_url(self) -> str:
        if self.pms_type == "blueprint":
            return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/patient/match"
        if self.pms_type == "audit_data":
            return f"{_CORTEX_BASE}/audit_data/{self.clinic_id}/patient/match"
        raise NotImplementedError(
            f"verify_caller_identification not routed for pms={self.pms_type}"
        )

    def tools(self) -> list[dict]:
        return [{
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Look up an existing patient in the clinic's records by first name, "
                "last name, and the last 4 digits of the phone number on file. "
                "Only call this after the caller confirms they are an existing patient. "
                "Returns 'matched' (patient identified uniquely), 'ambiguous' (multiple "
                "candidates — retry with the caller's date of birth), or 'unmatched' "
                "(treat the caller as new). The tool never reveals a patient's name, "
                "phone number, or DOB — only a status and an opaque patient identifier."
            ),
            "url": self._tool_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "first_name": {
                        "type": "string",
                        "description": "Caller's first name as they gave it.",
                    },
                    "last_name": {
                        "type": "string",
                        "description": "Caller's last name as they gave it.",
                    },
                    "last4_phone": {
                        "type": "string",
                        "description": (
                            "Last 4 digits of the phone number the caller has on file "
                            "with the clinic. Exactly 4 digits."
                        ),
                    },
                    "dob": {
                        "type": "string",
                        "description": (
                            "Optional date of birth in YYYY-MM-DD format. Provide this "
                            "on a retry when the initial match returned 'ambiguous'."
                        ),
                    },
                },
                "required": ["first_name", "last_name", "last4_phone"],
            },
        }]

    @property
    def prompt_fragment(self) -> str:
        return """## Verify Caller Identification
1. Work out whether the caller is an existing patient — **infer it from what they're asking for; don't reflexively ask "have you been here before?"** A caller who wants to cancel, reschedule, or check an existing appointment, pick up or repair hearing aids, or refers to "my" appointment / file / account / order is self-evidently an existing patient. In those cases, skip the question entirely: acknowledge the request ("Sure, I can help you cancel that") and go straight to verifying their identity (step 2). Only ask outright when their status is genuinely unclear — e.g. a general "how much is a hearing test?" or "do you take my insurance?" that a brand-new caller could equally ask.
2. Once you know (or have inferred) they're an existing patient:
   a. Collect their first and last name.
   b. **Confirm the spelling of BOTH names before doing anything else.** Voice
      transcription frequently mangles names; the lookup is exact-match, so a
      single wrong letter returns `unmatched`. Spell it back letter-by-letter
      and ask them to correct any letter you got wrong. Examples:
        - "Got it — let me spell that back. First name: J-O-H-N. Last name:
          S-M-Y-T-H-E. Did I get every letter right?"
        - If they correct you, repeat the spell-back with the correction until
          they confirm every letter.
      Do NOT call `verify_caller_identification` until the caller has confirmed
      both spellings.
   c. Ask for the last 4 digits of the phone number on file. Read those four
      digits back to confirm before proceeding.
   d. Call `verify_caller_identification` with the confirmed first_name,
      last_name, and last4_phone.
   e. If the result is `matched`, note the returned `patient_id` — you'll
      need it for any downstream protocols (Locate Appointment, Book
      Appointment, Cancel Appointment, Reschedule Appointment) and for the
      ticket.
   f. If the result is `ambiguous`, ask for the caller's date of birth and
      retry with the `dob` field.
   g. If the result is `unmatched` after your best effort, treat the caller
      as new and ask for a callback phone number.
3. If they're a new patient (their request implies it, or they say they
   haven't been in before): collect full name (still spell-confirm it so the
   ticket is accurate) and callback phone number directly.

You never learn the patient's full record — only a yes/no/ambiguous status and an opaque patient_id. Never pretend you know details about a patient beyond what the caller has told you directly."""
