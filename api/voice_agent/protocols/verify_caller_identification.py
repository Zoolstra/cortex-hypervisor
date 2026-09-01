"""VerifyCallerIdentification — confirm an existing patient by surname + last4-phone.

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
        "Confirm an existing patient by last name and the last 4 digits of the "
        "phone number on file. Requires the patient record to already exist in "
        "the PMS."
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
                "Look up an existing patient in the clinic's records by LAST NAME and "
                "the last 4 digits of the phone number on file. Those two are all you "
                "ask the caller for. Only call this after the caller confirms they are "
                "an existing patient. Returns 'matched' (identified uniquely), "
                "'ambiguous' (several people share that surname and number — retry, "
                "adding first_name if the caller already told you it, otherwise their "
                "date of birth), or 'unmatched' (treat the caller as new). The tool "
                "never reveals a patient's name, phone number, or DOB — only a status "
                "and an opaque patient identifier."
            ),
            "url": self._tool_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "last_name": {
                        "type": "string",
                        "description": (
                            "Caller's last name, spelling-confirmed with them first."
                        ),
                    },
                    "last4_phone": {
                        "type": "string",
                        "description": (
                            "Last 4 digits of the phone number the caller has on file "
                            "with the clinic. Exactly 4 digits."
                        ),
                    },
                    "first_name": {
                        "type": "string",
                        "description": (
                            "Optional tie-breaker. Do NOT ask the caller for this. "
                            "Send it only on a retry after 'ambiguous', and only if "
                            "they already volunteered it earlier in the call."
                        ),
                    },
                    "dob": {
                        "type": "string",
                        "description": (
                            "Optional tie-breaker, date of birth as YYYY-MM-DD. Ask "
                            "for this only on a retry after 'ambiguous' when you have "
                            "no first name to try, or when a first-name retry was "
                            "still ambiguous."
                        ),
                    },
                },
                "required": ["last_name", "last4_phone"],
            },
        }]

    @property
    def prompt_fragment(self) -> str:
        return """## Verify Caller Identification
1. Work out whether the caller is an existing patient — **infer it from what they're asking for; don't reflexively ask "have you been here before?"** A caller who wants to cancel, reschedule, or check an existing appointment, pick up or repair hearing aids, or refers to "my" appointment / file / account / order is self-evidently an existing patient. In those cases, skip the question entirely: acknowledge the request ("Sure, I can help you cancel that") and go straight to verifying their identity (step 2). Only ask outright when their status is genuinely unclear — e.g. a general "how much is a hearing test?" or "do you take my insurance?" that a brand-new caller could equally ask.
2. Once you know (or have inferred) they're an existing patient, you need
   exactly TWO things from them: their **last name** and the **last four
   digits** of the phone number on file. Not their first name. Not anything
   else.
   a. **Ask for both in ONE question, then stop and let them answer.**
      "Could I get your last name, and the last four digits of the phone
      number we have on file?" This is a deliberate exception to asking one
      thing per turn — they are a single identity check, and splitting them
      costs the caller an extra turn for nothing.
   b. **Confirm both back in ONE turn and wait for a single yes.** Spell the
      surname letter-by-letter and read the four digits in the same breath:
        - "Let me just check I've got that: S-M-Y-T-H-E, and four-two-six-one.
          Is that right?"
      Voice transcription mangles surnames and the lookup is exact-match, so a
      single wrong letter returns `unmatched`. If they correct either part,
      repeat the combined readback until they confirm. Do NOT call
      `verify_caller_identification` before that yes.
   c. Call `verify_caller_identification` with the confirmed `last_name` and
      `last4_phone`. Do not send `first_name` — you did not ask for it.
   d. If the result is `matched`, note the returned `patient_id` — you'll
      need it for every downstream tool that acts on this patient (looking
      up, deciding, or booking appointments) and for the ticket.
   e. If the result is `ambiguous`, more than one person shares that surname
      and number — usually a family on one line. Break the tie with the
      CHEAPEST thing you have:
        - If they already told you their first name earlier in the call, retry
          with `first_name`. That costs them nothing, so try it first.
        - Otherwise ask for their date of birth and retry with `dob`.
        - Still ambiguous after that? Treat it as unmatched and take a message.
   f. If the result is `unmatched` after your best effort, treat the caller
      as new and ask for a callback phone number.
3. If they're a new patient (their request implies it, or they say they
   haven't been in before): collect full name (spell-confirm it so the ticket
   is accurate) and callback phone number directly.

You never learn the patient's full record — only a yes/no/ambiguous status and an opaque patient_id. Never pretend you know details about a patient beyond what the caller has told you directly."""
