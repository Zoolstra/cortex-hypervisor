"""
Customer.io integration seam — Phase B (DESIGN ONLY, NOT WIRED).

The database-reactivation feature (api/worklists.py segments) will, in Phase B,
push dormant-patient segments into outbound SMS/email sequences. Decision:
**CORTEX triggers sends via the Customer.io API** (we own timing/content), so
this is a thin client over Customer.io's HTTP API, not their visual workflows.

NOTHING here is wired yet — no router, no scheduler, no live calls. The send
functions raise NotImplementedError. What IS real and must stay non-bypassable
is `consent_blocks_send()`: the legal gate. Do not add a send path that skips it.

## Phase-B prerequisites before wiring
  * Customer.io account + API credentials in Secret Manager:
      - `customerio-site-id`, `customerio-track-api-key` (Track API), and/or
      - `customerio-app-api-key` (transactional/messaging).
    Fetch via `api.core.secrets.get_secret(...)` (lru-cached) — never env/disk.
  * Legal sign-off on consent basis (CASL for Canadian clinics, TCPA for US),
    including a working unsubscribe, BEFORE any send code lands.

## PHI minimization + authoritative consent
  Segment rows expose name + clinical/device context + the two key consent
  flags (for staff awareness), but NOT raw email/phone. At send time this seam
  must (a) resolve contact server-side keyed by (clinic_id, client_id) so PII
  never transits the browser, and (b) RE-READ all consent flags fresh from
  Blueprint — `do_not_send_commercial_messages`, `do_not_text`, `do_not_email`
  (and `do_not_mail` if physical mail is ever added) — rather than trust the
  possibly-stale flags from the segment list. The list flags are display-only.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatientRef:
    """Stable key for a patient within a clinic (matches segment row output)."""
    clinic_id: str
    client_id: str


@dataclass(frozen=True)
class Consent:
    """Consent flags as surfaced by the segment queries (already normalized)."""
    do_not_send_commercial_messages: bool
    do_not_text: bool
    do_not_email: bool = False


def consent_blocks_send(consent: Consent, channel: str) -> bool:
    """The legal gate. Returns True if a send on ``channel`` must be blocked.

    - ``do_not_send_commercial_messages`` blocks ALL commercial channels.
    - ``do_not_text`` additionally blocks SMS.
    - ``do_not_email`` additionally blocks email.

    Channel-specific opt-outs matter: a patient may allow email but not SMS (or
    vice-versa). This MUST be checked server-side before every enqueue/send and
    MUST NOT be bypassable from the client — CASL (CA) / TCPA (US) depend on it.
    """
    if consent.do_not_send_commercial_messages:
        return True
    if channel == "sms" and consent.do_not_text:
        return True
    if channel == "email" and consent.do_not_email:
        return True
    return False


# ── Phase-B send seam (NOT IMPLEMENTED) ──────────────────────────────────────

def enroll_patient(segment_key: str, patient: PatientRef, consent: Consent) -> None:
    """Phase B: enroll a patient into the outbound sequence for ``segment_key``.

    Will resolve contact (email/phone) server-side from Blueprint by
    (clinic_id, client_id), check ``consent_blocks_send`` per channel, then call
    the Customer.io API. Not wired yet.
    """
    # Consent gate already in the call path so a Phase-B implementation can't
    # forget it: enroll only into channels the patient hasn't opted out of.
    if all(consent_blocks_send(consent, ch) for ch in ("sms", "email")):
        return  # fully opted out — nothing to enroll
    raise NotImplementedError("Customer.io enrollment is Phase B — not yet wired.")


def trigger_send(segment_key: str, patient: PatientRef, consent: Consent, channel: str) -> None:
    """Phase B: trigger a single SMS/email send via the Customer.io API.

    Refuses when ``consent_blocks_send(consent, channel)``. Not wired yet.
    """
    if consent_blocks_send(consent, channel):
        return  # blocked by consent — never send
    raise NotImplementedError("Customer.io send is Phase B — not yet wired.")
