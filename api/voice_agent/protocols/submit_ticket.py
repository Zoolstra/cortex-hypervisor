"""SubmitTicket — every call ends with one ticket. Always-on, PMS-agnostic."""
from __future__ import annotations

import os

from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")


class SubmitTicketProtocol(Protocol):
    id = "submit_ticket"
    display_name = "Submit ticket"
    description = (
        "Foundational. Every call produces one ticket summarising the caller's "
        "need, collected info, and a suggested follow-up for clinic staff."
    )
    agent_tool_name = "submit_ticket"
    supported_pms = None  # PMS-agnostic — writes to BQ, not a PMS
    always_on = True

    def tools(self) -> list[dict]:
        return [{
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Submit a ticket summarizing this call. Call this exactly once, "
                "right before you end the conversation. The ticket is what clinic "
                "staff will see to follow up. If you do not call this, the call "
                "is lost from the clinic's point of view."
            ),
            "url": f"{_CORTEX_BASE}/clinics/{self.clinic_id}/voice_agent/tickets",
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "vapi_call_id": {
                        "type": "string",
                        "description": "The current VAPI call ID, if available.",
                    },
                    "caller_phone": {
                        "type": "string",
                        "description": "Caller's phone in E.164 format (e.g. +16045551234).",
                    },
                    "caller_name": {
                        "type": "string",
                        "description": "The caller's name as they gave it during the call.",
                    },
                    "patient_match_status": {
                        "type": "string",
                        "enum": ["matched", "unmatched", "new", "ambiguous"],
                        "description": (
                            "'matched' = verify_caller_identification returned matched. "
                            "'ambiguous' = still ambiguous after the DOB retry. "
                            "'unmatched' = verify_caller_identification returned unmatched. "
                            "'new' = the caller self-identified as a new patient."
                        ),
                    },
                    "blueprint_patient_id": {
                        "type": "string",
                        "description": (
                            "The patient_id returned by verify_caller_identification. "
                            "Omit when patient_match_status is not 'matched'."
                        ),
                    },
                    "last4_confirmed": {
                        "type": "boolean",
                        "description": (
                            "True if the caller confirmed the last 4 digits of the "
                            "phone on file during the match flow."
                        ),
                    },
                    "intent_category": {
                        "type": "string",
                        "description": (
                            "A short free-text label that best describes the "
                            "caller's need, in your own concise wording "
                            "(e.g. 'new patient booking', 'hearing aid repair', "
                            "'insurance question', 'wax removal')."
                        ),
                    },
                    "summary": {
                        "type": "string",
                        "description": "1-2 sentence recap of the call for clinic staff.",
                    },
                    "details": {
                        "type": "object",
                        "description": (
                            "Intent-specific fields collected during the call. "
                            "Free-form JSON — include whatever is relevant to the "
                            "protocol you followed. If you asked the clinic's "
                            "new-patient screening questions and the caller did NOT "
                            "book, put the answers in 'screening_answers' (a JSON "
                            "array of objects, not a single string)."
                        ),
                        "properties": {
                            "screening_answers": {
                                "type": "array",
                                "description": (
                                    "New-patient screening Q&A, only when the caller "
                                    "did NOT book. One object per question asked."
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "question": {"type": "string"},
                                        "answer": {"type": "string"},
                                    },
                                    "required": ["question", "answer"],
                                },
                            },
                        },
                    },
                    "suggested_followup": {
                        "type": "string",
                        "description": (
                            "What clinic staff should do next, based on the call. "
                            "e.g. 'Call back to book hearing test', 'Send wax-removal "
                            "referral', 'No action required'."
                        ),
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["normal", "urgent"],
                        "description": (
                            "'urgent' if the caller mentioned a time-sensitive issue "
                            "(severe distress, sudden hearing loss, etc.). Otherwise 'normal'."
                        ),
                    },
                },
                "required": ["patient_match_status"],
            },
        }]

    @property
    def prompt_fragment(self) -> str:
        return """## Closing & Ticket Submission
Before ending the call:
1. Summarize back to the caller what you've captured and confirm it's correct.
2. Let them know a team member will follow up — you cannot confirm a specific appointment time.
3. Call `submit_ticket` EXACTLY ONCE with:
   - caller_name, caller_phone (E.164), patient_match_status, blueprint_patient_id (if matched), last4_confirmed.
   - intent_category: a short free-text label for the caller's need (e.g. "new patient booking", "hearing aid repair", "insurance question").
   - summary: 1-2 sentences for clinic staff.
   - details: any intent-specific fields you collected. If you asked the new-patient screening questions and the caller did NOT book, include `details.screening_answers` as a list of `{question, answer}` objects so staff have the screening on file.
   - suggested_followup: concrete next action (e.g. "book hearing test, afternoon preference", "return wax-removal referral").
   - urgency: 'urgent' only if the caller reported a time-sensitive medical concern; otherwise 'normal'.
4. Warm goodbye, end the call.

If `submit_ticket` fails, apologize, tell the caller you'll have a team member call back, and log the failure in your final message."""
