"""
Internal staff alerts — "a message came in" notifications to clinic staff.

Distinct from ``api/services/customerio.py`` (that seam is patient-facing
COMMERCIAL messaging, consent-gated under CASL/TCPA). This module sends an
OPERATIONAL alert to the clinic's own staff about an inbound after-hours voice
message, so a captured lead is never lost. No patient-consent gate applies — the
recipient is the clinic, about their own call.

V1 channel is SMS via Twilio (account creds already in Secret Manager, used by
``api/voice_agent/twilio.py``). The sending number comes from the
``twilio-alert-from-number`` secret. Email is a forward-compat hook.

Every send is BEST-EFFORT: failures are logged and swallowed. A notification
must NEVER fail the ticket write it accompanies — a stored-but-un-alerted ticket
is recoverable; a failed ticket write is a lost lead.
"""
from __future__ import annotations

import logging

from api.core.secrets import get_secret

log = logging.getLogger(__name__)


def _twilio_client():
    # Local import so this module (and the get_secret calls) stay lazy — the
    # ticket endpoint shouldn't pay Twilio import/SM cost unless an alert fires.
    from twilio.rest import Client  # noqa: PLC0415

    return Client(get_secret("twilio-account-sid"), get_secret("twilio-auth-token"))


def _compose_sms(
    *,
    clinic_name: str,
    caller_name: str | None,
    callback_number: str | None,
    intent_category: str | None,
    summary: str | None,
    urgency: str,
) -> str:
    """Build the staff SMS body. Kept short; staff act off name + number + need."""
    tag = "⚠ URGENT " if urgency == "urgent" else ""
    lines = [f"{tag}New {clinic_name} after-hours message"]
    lines.append(f"From: {caller_name or 'Unknown'}"
                 + (f" ({callback_number})" if callback_number else " (no number captured)"))
    if intent_category:
        lines.append(f"Re: {intent_category}")
    if summary:
        lines.append(summary.strip())
    lines.append("Call back next business day.")
    return "\n".join(lines)


def notify_new_ticket(
    *,
    clinic_name: str,
    alert_sms_to: str | None,
    alert_email_to: str | None,
    caller_name: str | None,
    callback_number: str | None,
    intent_category: str | None,
    summary: str | None,
    urgency: str = "normal",
) -> None:
    """Best-effort staff alert that a voice-agent ticket was created.

    Sends an SMS when both a destination (``alert_sms_to``) and a Twilio
    ``twilio-alert-from-number`` secret are available. Silently no-ops (with a
    log line) when unconfigured. Never raises — callers rely on that.
    """
    if not alert_sms_to:
        log.info("notify_new_ticket: no alert destination configured for %s — skipping",
                 clinic_name)
        return
    try:
        from_number = get_secret("twilio-alert-from-number")
    except Exception:
        log.warning("notify_new_ticket: twilio-alert-from-number secret missing — "
                    "cannot send SMS alert for %s", clinic_name)
        return
    if not from_number:
        return

    body = _compose_sms(
        clinic_name=clinic_name,
        caller_name=caller_name,
        callback_number=callback_number,
        intent_category=intent_category,
        summary=summary,
        urgency=urgency,
    )
    try:
        _twilio_client().messages.create(to=alert_sms_to, from_=from_number, body=body)
        log.info("notify_new_ticket: SMS alert sent for %s", clinic_name)
    except Exception:
        # Best-effort: log and swallow so the ticket write is never affected.
        log.exception("notify_new_ticket: SMS alert failed for %s", clinic_name)
    # email_to is a forward-compat hook — no provider wired in V1.
