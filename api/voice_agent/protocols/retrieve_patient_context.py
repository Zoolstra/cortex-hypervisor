"""RetrievePatientContext — pull a verified patient's recent journal entries.

After Verify Caller Identification returns ``matched``, this lets the agent
fetch the patient's recent journal/history entries (clinical PHI) as silent
background context — so it doesn't re-ask things the clinic already knows and
can tailor the conversation.

Opt-in per clinic: enabling this exposes clinical journal text to the LLM, so
it's a deliberate toggle distinct from identity verification. Depends on
Verify Caller Identification — the tool input ``patient_id`` is the opaque id
that protocol returns. Server-side the query is hard-filtered by clinic +
patient and bounded (non-deleted, last 24 months, 10 most recent).
"""
from __future__ import annotations

import os

from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")


class RetrievePatientContextProtocol(Protocol):
    id = "retrieve_patient_context"
    display_name = "Retrieve Patient Context"
    description = (
        "After an existing patient is identified, fetch their recent journal "
        "entries as background context for the agent. Surfaces clinical PHI to "
        "the assistant, so it's opt-in and separate from identity verification."
    )
    agent_tool_name = "retrieve_patient_context"
    supported_pms = ("blueprint",)
    depends_on = ("verify_caller_identification",)

    def _tool_url(self) -> str:
        if self.pms_type == "blueprint":
            return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/patient/journal"
        raise NotImplementedError(
            f"retrieve_patient_context not routed for pms={self.pms_type}"
        )

    def tools(self) -> list[dict]:
        return [{
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Fetch a verified patient's recent journal entries for background "
                "context. Returns {entries: [{entry_time, entry_type, text}]} — the "
                "clinic's most recent notes (up to 10, last 24 months). Call this "
                "ONCE, right after verify_caller_identification returns "
                "status='matched', passing the patient_id it returned. Use what you "
                "read only as silent background — do NOT recite notes to the caller "
                "or volunteer clinical details; let it inform your questions."
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
                            "The opaque patient_id returned by "
                            "verify_caller_identification (status='matched'). "
                            "Required."
                        ),
                    },
                },
                "required": ["patient_id"],
            },
        }]

    @property
    def prompt_fragment(self) -> str:
        return """## Retrieve Patient Context
Right after `verify_caller_identification` returns `matched`, call `retrieve_patient_context` ONCE with that `patient_id`. It returns the patient's recent journal entries: `{entries: [{entry_time, entry_type, text}]}`.

Use these as **silent background only**:
- Let them sharpen your questions and help you avoid re-asking things already on file (e.g. you can see they were recently fitted, so you ask about that rather than starting cold).
- Do NOT read entries aloud, summarize the chart back, or volunteer clinical details the caller didn't raise. If the caller asks about specifics in their record, tell them a clinician will go over the details — you're a receptionist, not their provider.
- Never repeat sensitive history unprompted. Treat it like notes you glanced at, not a script to recite.

If the tool returns no entries (new-ish patient or nothing on file), just proceed normally — it's not an error."""
