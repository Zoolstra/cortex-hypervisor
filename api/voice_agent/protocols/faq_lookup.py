"""FaqLookup — answer general clinic questions from a curated FAQ set.

The FAQ corpus is NOT prompt content. This protocol contributes one tool and a
short usage fragment; the answers themselves are retrieved mid-call by semantic
search over ``ClinicData.faq_embeddings`` (see
``api/voice_agent/faq_retrieval.py``). Three reasons that split is deliberate:

  1. **Prompt salience.** ``roles.py`` exists because a generalist prompt with
     several competing jobs made gpt-4o take a message instead of calling its
     booking tools. A corpus that grows on every approval would re-dilute the
     booking spine it was built to protect.
  2. **No re-sync on edit.** Prompt content is baked into the VAPI assistant at
     build time, so every approved answer would need an assistant re-sync.
     Retrieval reads live.
  3. **Bounded cost.** Prompt tokens are paid on every turn of every call; a
     retrieval call is paid only when a caller actually asks something.

PMS-agnostic and clinic-agnostic — any clinic with approved FAQs can use it. It
is NOT always-on: a clinic with an empty approved set should not carry a tool
that can only ever return nothing.
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")


class FaqLookupConfig(BaseModel):
    """Per-clinic retrieval tuning.

    ``max_distance`` is the important one. It is a COSINE DISTANCE (0 =
    identical), so LOWER is stricter. Raising it makes the agent answer more
    questions and answer more of them wrongly — a loose threshold is how a
    caller asking about hearing-aid batteries gets read an answer about parking.
    Prefer returning nothing and taking a message.
    """

    top_k: int = Field(default=3, ge=1, le=10)
    max_distance: float = Field(default=0.35, ge=0.0, le=2.0)

    model_config = {"extra": "ignore"}


class FaqLookupProtocol(Protocol):
    id = "faq_lookup"
    display_name = "Answer Clinic Questions (FAQ)"
    description = (
        "Lets the agent answer general questions about the clinic — hours, "
        "parking, what to bring, pricing, insurance — from the clinic's "
        "approved FAQ set. Answers are retrieved by semantic search at call "
        "time, so approving or editing an answer takes effect immediately with "
        "no assistant rebuild, and the FAQ text never enters the system prompt."
    )
    agent_tool_name = "answer_clinic_question"
    config_model = FaqLookupConfig

    def _tool_url(self) -> str:
        return f"{_CORTEX_BASE}/clinics/{self.clinic_id}/voice_agent/faq/search"

    def tools(self) -> list[dict]:
        return [{
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Look up the clinic's approved answer to a general question "
                "(hours, location, parking, what to bring, pricing, insurance, "
                "hearing-aid care). Pass the caller's question in their own "
                "words — this is a semantic search, so it does not need to "
                "match any stored wording. Returns "
                "{matched: bool, answers: [{question, answer}]}. If matched is "
                "false the clinic has no approved answer: say you'll have "
                "someone follow up, and do NOT invent one. Use this only for "
                "general clinic information — never for anything about a "
                "specific patient's record, appointments, or eligibility."
            ),
            "url": self._tool_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "The caller's question, in their own words. Keep it "
                            "as a question rather than a keyword — 'do you have "
                            "parking?' retrieves better than 'parking'."
                        ),
                    },
                },
                "required": ["question"],
            },
        }]

    @property
    def prompt_fragment(self) -> str:
        return """## Answering General Questions
When the caller asks a general question about the clinic — hours, where to park, what to bring, what something costs, whether insurance is accepted, how to look after hearing aids — call `answer_clinic_question` with their question in their own words.

- If `matched` is true, answer using the returned `answer` text. Put it in your own warm, spoken phrasing; don't read it out like a document. If several answers come back, use the first one unless a later one clearly fits better.
- If `matched` is false, say plainly that you want to get them the right answer and a team member will follow up — then capture the question in the ticket. **Never improvise an answer to a question this tool didn't answer.** A confident wrong answer about price or coverage is worse than "let me have someone confirm that for you".

Two hard limits:
- **General information only.** Anything about THIS caller's own record — their appointments, their care plan, whether they're due, what they're covered for — comes from the patient tools, never from here.
- **It does not book anything.** If the question turns out to be a booking request, answer it and then return to the booking steps."""
