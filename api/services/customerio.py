"""
Customer.io integration — database-reactivation outbound (Phase B, LIVE).

The reactivation worklists (``api/worklists.py`` cohorts, e.g. tested-not-sold)
feed a Customer.io campaign. Decision update (2026-08-04, Alto pilot): the
campaign — content, timing, channel sequencing — lives in OUR Customer.io
workspace and is **event-triggered**; CORTEX owns the audience. So this client
does exactly two things per enrolled patient via the Track API:

  1. ``identify`` — upsert the person (id = ``{clinic_id}:{client_id}``) with
     contact attributes + per-channel consent flags, and
  2. ``track`` — fire the enrollment event (default
     ``tested_not_sold_lead``) that starts the campaign.

Campaign-side channel gating: ``do_not_email`` / ``do_not_text`` ride along as
person attributes so the Customer.io workflow can branch per channel, but the
authoritative gate is ``consent_blocks_send`` here — a patient opted out of
every channel is never identified or evented at all.

Workspace model: **one Customer.io workspace per clinic** — each workspace has
its own Track API credentials, so secrets are per-clinic, keyed by clinic_id
(the same convention as ``datafeed-api-key-<instance_id>``; creating the
secrets IS enabling the sync for that clinic):
  - ``customerio-site-id-<clinic_id>``
  - ``customerio-track-api-key-<clinic_id>``
  - ``customerio-region-<clinic_id>`` (optional, ``eu`` if that workspace is
    EU-hosted; default US)
A clinic with no secrets fails the sync with a clear error before any patient
is touched. Workspace-per-clinic also means hard tenant isolation on the
Customer.io side: one clinic's people/campaigns are invisible to another's.

What must stay non-bypassable is ``consent_blocks_send()``: the legal gate
(CASL for Canadian clinics, TCPA for US). Do not add a send path that skips it.

PHI minimization: contact detail is resolved server-side from Blueprint by
(clinic_id, client_id) in the sync path (``api/worklists.py``) — PII never
transits the browser. Consent flags are read fresh from the cohort query at
sync time, not cached.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from api.core.secrets import get_secret

log = logging.getLogger(__name__)

_TRACK_HOSTS = {
    "us": "https://track.customer.io",
    "eu": "https://track-eu.customer.io",
}
_TIMEOUT = 15.0


@dataclass(frozen=True)
class PatientRef:
    """Stable key for a patient within a clinic (matches segment row output)."""
    clinic_id: str
    client_id: str

    @property
    def person_id(self) -> str:
        """The Customer.io person identifier — stable across email changes."""
        return f"{self.clinic_id}:{self.client_id}"


@dataclass(frozen=True)
class Consent:
    """Consent flags as surfaced by the cohort queries (already normalized)."""
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


def fully_opted_out(consent: Consent) -> bool:
    """True when no commercial channel remains — the patient must not be
    pushed to Customer.io at all (not even as a suppressed profile)."""
    return all(consent_blocks_send(consent, ch) for ch in ("sms", "email"))


class CustomerIOError(RuntimeError):
    """A Track API call failed after the response was received."""


class CustomerIONotConfigured(RuntimeError):
    """The clinic has no Customer.io workspace credentials in Secret Manager."""


class CustomerIOClient:
    """Thin client over one CLINIC's Customer.io workspace (Track API,
    Basic auth site_id:api_key). Workspaces are per-clinic, so a client is
    always constructed for a specific ``clinic_id`` and can only ever write
    into that clinic's workspace.

    Both calls are idempotent from our side: ``identify`` is an upsert, and
    duplicate ``track`` events are prevented upstream by the
    ``customerio_enrollments`` log — this client never decides WHO to send to,
    only performs the send it is handed.
    """

    def __init__(self, clinic_id: str, *, site_id: str | None = None,
                 api_key: str | None = None, region: str | None = None):
        try:
            self._site_id = site_id or get_secret(f"customerio-site-id-{clinic_id}")
            self._api_key = api_key or get_secret(
                f"customerio-track-api-key-{clinic_id}")
        except Exception as exc:
            raise CustomerIONotConfigured(
                f"No Customer.io workspace credentials for clinic {clinic_id} — "
                f"create customerio-site-id-{clinic_id} and "
                f"customerio-track-api-key-{clinic_id} in Secret Manager."
            ) from exc
        if region is None:
            try:
                region = (get_secret(f"customerio-region-{clinic_id}")
                          or "us").strip().lower()
            except Exception:
                region = "us"
        self._base = _TRACK_HOSTS.get(region, _TRACK_HOSTS["us"])

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> None:
        url = f"{self._base}{path}"
        resp = httpx.request(
            method, url, json=payload,
            auth=(self._site_id, self._api_key), timeout=_TIMEOUT,
        )
        if resp.status_code >= 300:
            # Track API errors carry a JSON body with per-field detail — log it,
            # but never the payload (it contains PII).
            raise CustomerIOError(
                f"{method} {path} → {resp.status_code}: {resp.text[:500]}")

    def identify(self, person_id: str, attributes: dict[str, Any]) -> None:
        """Create or update a person. ``attributes`` replaces listed keys only."""
        self._request("PUT", f"/api/v1/customers/{person_id}", attributes)

    def track(self, person_id: str, event_name: str,
              data: dict[str, Any] | None = None) -> None:
        """Fire an event on a person — this is what triggers the campaign."""
        self._request("POST", f"/api/v1/customers/{person_id}/events",
                      {"name": event_name, "data": data or {}})


def enroll_patient(
    client: CustomerIOClient,
    patient: PatientRef,
    consent: Consent,
    *,
    event_name: str,
    attributes: dict[str, Any],
    event_data: dict[str, Any] | None = None,
) -> bool:
    """Enroll one patient: identify + fire the campaign-trigger event.

    Returns True when the event was sent, False when consent blocked it.
    The consent gate lives IN this call path so no future caller can forget it.
    Caller is responsible for de-duplication (``customerio_enrollments``).
    """
    if fully_opted_out(consent):
        return False  # fully opted out — never reaches Customer.io
    attrs = dict(attributes)
    # Per-channel flags for campaign-side branching; the workflow must check
    # these before each channel step (defense in depth on top of this gate).
    attrs["do_not_email"] = consent_blocks_send(consent, "email")
    attrs["do_not_text"] = consent_blocks_send(consent, "sms")
    client.identify(patient.person_id, attrs)
    client.track(patient.person_id, event_name, event_data)
    return True
