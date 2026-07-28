"""ACNA hearing-aid troubleshooting — a prompt-only, clinic-scoped protocol.

Phase-1 after-hours scope lets the agent walk a caller through a small, defined
set of hearing-aid self-checks. This is deliberately shallow: the agent triages
(1–2 quick checks), and if the issue isn't resolved it routes to booking a
service appointment or taking a message. It never diagnoses and never promises a
fix.

The protocol contributes NO VAPI tool — its behavior is entirely a prompt
fragment. The actual steps are per-clinic config (``ACNATroubleshootingConfig``),
so clinic staff can edit them without a code change. The steps are supplied by
the clinic; until then the config is empty and the fragment renders a safe
generic stance (offer a service appointment or take a message).

Clinic-scoped to ACNA via ``supported_clinics``. The exits it references —
booking a service appointment, and taking a message — are provided by the ACNA
booking protocols and the always-on SubmitTicket protocol respectively; it does
not hard-depend on them so it degrades gracefully to "take a message" when
booking isn't enabled.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from api.voice_agent.protocols.base import Protocol


ACNA_CLINIC_ID = "0b5f0929-31fb-4e21-9dd4-030bd040335d"


class TroubleshootingEntry(BaseModel):
    """One symptom the agent can triage.

    ``quick_checks`` are the 1–2 self-checks the agent walks the caller through
    (kept short — this is after-hours triage, not a repair manual).
    ``if_unresolved`` decides the exit when the checks don't fix it.
    """

    symptom: str
    quick_checks: list[str] = Field(default_factory=list)
    if_unresolved: Literal["book_service", "take_message"] = "book_service"

    model_config = {"extra": "forbid"}


class ACNATroubleshootingConfig(BaseModel):
    """Per-clinic troubleshooting steps. Empty until the clinic supplies them."""

    entries: list[TroubleshootingEntry] = Field(default_factory=list)

    model_config = {"extra": "ignore"}


class ACNATroubleshootingProtocol(Protocol):
    id = "acna_troubleshooting"
    display_name = "Hearing-Aid Troubleshooting (ACNA)"
    description = (
        "Walk a caller through a small, defined set of hearing-aid self-checks. "
        "If the issue isn't resolved, route to booking a service appointment or "
        "taking a message. Triage only — never diagnoses or promises a fix."
    )
    supported_pms = None  # PMS-agnostic — purely conversational
    supported_clinics = (ACNA_CLINIC_ID,)
    config_model = ACNATroubleshootingConfig

    def tools(self) -> list[dict]:
        # Prompt-only: no VAPI tool. Exits reuse the booking + submit_ticket
        # tools contributed by other protocols.
        return []

    @property
    def prompt_fragment(self) -> str:
        cfg = self.config  # ACNATroubleshootingConfig instance
        entries = getattr(cfg, "entries", []) or []

        out = [
            "## Hearing-Aid Troubleshooting",
            "If the caller reports a problem with their hearing aids, you may walk "
            "them through the quick self-checks below. Stay strictly within these "
            "steps — do NOT diagnose, and never promise that a step will fix the "
            "problem.",
        ]

        if entries:
            out.append("")
            for e in entries:
                checks = [c for c in (e.quick_checks or []) if c and c.strip()]
                block = [f"**{e.symptom.strip()}**"]
                if checks:
                    block.append("Quick checks (one at a time, in your own words):")
                    block.extend(f"{i}. {c.strip()}" for i, c in enumerate(checks, 1))
                exit_txt = (
                    "offer to book a service appointment"
                    if e.if_unresolved == "book_service"
                    else "take a message for the team"
                )
                block.append(f"If that doesn't resolve it, {exit_txt}.")
                out.append("\n".join(block))
        else:
            out.append(
                "No specific troubleshooting steps are configured yet. If a caller "
                "has a hearing-aid problem, don't attempt to walk through steps — go "
                "straight to offering a service appointment or taking a message."
            )

        out.extend([
            "",
            "**Exit rules**",
            "- If a check resolves the issue, confirm it's working and ask if there's "
            "anything else you can help with.",
            "- If the issue isn't one you have steps for, or the steps don't resolve "
            "it: if you can book a service appointment, offer that; otherwise take a "
            "message (name + callback number + a short description) so a team member "
            "can follow up on the next business day.",
            "- Never promise a repair, a part, or a specific outcome — clinic staff "
            "decide what the aid needs.",
        ])
        return "\n".join(out)
