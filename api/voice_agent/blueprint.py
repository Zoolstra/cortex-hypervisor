"""
Blueprint OMS API proxy — called by VAPI tool definitions.

These endpoints are NOT for end users. They are called by VAPI during live
calls when the voice agent needs to trigger Blueprint, check availability,
or create an appointment. Auth is via X-Vapi-Secret header.

The /clinic-config endpoint IS for end users (Firebase auth) and lets admins
see Blueprint appointment types, providers, and locations before activating
the voice agent.

Blueprint credentials per request:
  - Non-secret config (clinic_code, api_url, aws_url) → Cloud SQL
    `clinic_blueprint_config`.
  - Secrets (api_key) → Secret Manager, keyed by clinic_id:
    ``clinic_{clinic_id}_blueprint_api_key``.
  - Time zone → Cloud SQL `clinic_location_details.time_zone`.

The patient/match endpoint queries `Blueprint_PHI.ClientDemographics` in
BigQuery directly — that PHI table stays in BQ.

Blueprint API base URL: https://{server}/{clinic_slug}/rest/
"""
import json
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.audit import log_phi_access
from api.deps import require_read_access, require_write_access, verify_token
from api.core.db import get_session
from api.core.orm import ClinicBlueprintEntityNote
from api.core.secrets import get_secret
from api.voice_agent.pms.blueprint import (
    BlueprintAdapter,
    _blueprint_base,
    _get_blueprint_config,
    _int_field,
)

router = APIRouter(prefix="/blueprint")


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_vapi_secret(x_vapi_secret: str = Header(None)) -> None:
    expected = get_secret("vapi-webhook-secret")
    if not expected or x_vapi_secret != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing Vapi secret")


# ── Blueprint credentials ─────────────────────────────────────────────────────
#
# Config + URL helpers (`_get_blueprint_config`, `_blueprint_base`,
# `_int_field`) and the BQ/HTTP plumbing for voice-agent operations now live
# in `api/voice_agent/pms/blueprint.py`. They're re-imported above so existing
# call sites in this router keep working.


# ── Request models ────────────────────────────────────────────────────────────

class LookupPatientRequest(BaseModel):
    caller_phone: str


class AvailabilityRequest(BaseModel):
    event_type_id: int
    start_date: str   # YYYY-MM-DD
    end_date: str     # YYYY-MM-DD


class AvailabilitySearchRequest(BaseModel):
    start_date: str                              # YYYY-MM-DD (clinic local time)
    end_date: str                                # YYYY-MM-DD (clinic local time, inclusive)
    locations: list[int] | None = None           # defaults to the clinic's configured location
    available_for_online_booking_only: bool | None = None


class FindAvailableSlotsRequest(BaseModel):
    event_type_id: int
    start_date: str   # YYYY-MM-DD (clinic local time)
    end_date: str     # YYYY-MM-DD (clinic local time, inclusive)
    providers: list[int] | None = None
    locations: list[int] | None = None


# ── Voice agent v2: locate / book / cancel / reschedule ────────────────────────

class LocateAppointmentRequest(BaseModel):
    patient_id: str
    days_ahead: int = 60
    days_back: int = 0


class LocateAppointmentItem(BaseModel):
    appointment_id: str
    event_type_name: str | None = None
    summary: str | None = None
    start_time: str  # ISO-8601 in clinic-local time
    end_time: str
    provider_name: str | None = None
    location_name: str | None = None
    status: str


class LocateAppointmentResponse(BaseModel):
    appointments: list[LocateAppointmentItem]


class BookAppointmentRequest(BaseModel):
    event_type_id: int
    start_date: str          # YYYY-MM-DD (clinic-local)
    start_time: str          # HH:MM (end_time derived from event type duration)
    location_id: int | None = None  # caller-chosen; omit for single-location clinics
    patient_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    notes: str | None = None


class CancelAppointmentRequest(BaseModel):
    appointment_id: str


class RescheduleAppointmentRequest(BaseModel):
    appointment_id: str
    new_start_date: str   # YYYY-MM-DD
    new_start_time: str   # HH:MM


class BookingResultResponse(BaseModel):
    """Echo of an act-on-appointment operation. ``warning`` is set only
    for partial-success rescheduling — see BlueprintAdapter.reschedule."""

    status: Literal["booked", "cancelled", "rescheduled", "partial"]
    appointment_id: str | None = None
    summary: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    warning: str | None = None


# ── Admin endpoint ────────────────────────────────────────────────────────────

def _entity_notes_map(db: Session, clinic_id: str) -> dict[tuple[str, int], str]:
    """Return {(entity_kind, entity_id): note} for a clinic's saved annotations
    on Blueprint appointment types / providers."""
    rows = db.scalars(
        select(ClinicBlueprintEntityNote).where(
            ClinicBlueprintEntityNote.clinic_id == clinic_id
        )
    ).all()
    return {(r.entity_kind, r.entity_id): r.note for r in rows}


@router.get("/{clinic_id}/clinic-config")
def get_clinic_config(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    Fetch Blueprint clinic configuration: appointment types, providers, locations.

    Each appointment type and provider carries a ``note`` — the admin-attached
    free-text annotation (empty string when none), so the dashboard can render
    and edit it inline.
    """
    clinic = _get_blueprint_config(db, clinic_id)
    require_read_access(clinic["instance_id"], caller)

    base = _blueprint_base(clinic)
    resp = httpx.get(
        f"{base}/clinicConfiguration/",
        params={"apiKey": clinic["api_key"]},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    notes = _entity_notes_map(db, clinic_id)

    return {
        "appointment_types": [
            {
                "id": t["id"],
                "name": t.get("name"),
                "duration_minutes": t.get("duration"),
                "description": t.get("description", ""),
                "note": notes.get(("appointment_type", t["id"]), ""),
            }
            for t in data.get("appointmentTypes", [])
        ],
        "providers": [
            {
                "id": p["id"],
                "name": (p.get("onlineDisplayName") or
                         f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()),
                "job_title": p.get("jobTitle"),
                "qualifications": p.get("qualifications"),
                "location_ids": p.get("locations", []),
                "note": notes.get(("provider", p["id"]), ""),
            }
            for p in data.get("providers", [])
        ],
        "locations": [
            {
                "id": loc["id"],
                "name": loc.get("name"),
                "address": loc.get("formattedAddress") or loc.get("street"),
                "timezone": loc.get("timeZone"),
            }
            for loc in data.get("locations", [])
        ],
    }


class EntityNoteRequest(BaseModel):
    entity_kind: Literal["appointment_type", "provider"]
    entity_id: int
    note: str   # blank/whitespace clears (deletes) the note


class EntityNoteResponse(BaseModel):
    entity_kind: Literal["appointment_type", "provider"]
    entity_id: int
    note: str


@router.put("/{clinic_id}/notes", response_model=EntityNoteResponse)
def upsert_entity_note(
    clinic_id: str,
    body: EntityNoteRequest,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    Attach / update / clear an admin note on a Blueprint appointment type or
    provider. A blank note deletes the row. The note is merged into the voice
    agent's Clinic Reference prompt on the clinic's next sync.
    """
    clinic = _get_blueprint_config(db, clinic_id)
    require_write_access(clinic["instance_id"], caller)

    note = body.note.strip()
    row = db.scalar(
        select(ClinicBlueprintEntityNote).where(
            ClinicBlueprintEntityNote.clinic_id == clinic_id,
            ClinicBlueprintEntityNote.entity_kind == body.entity_kind,
            ClinicBlueprintEntityNote.entity_id == body.entity_id,
        )
    )
    if not note:
        if row is not None:
            db.delete(row)
    elif row is not None:
        row.note = note
    else:
        db.add(ClinicBlueprintEntityNote(
            clinic_id=clinic_id,
            entity_kind=body.entity_kind,
            entity_id=body.entity_id,
            note=note,
        ))
    # get_session commits on return.
    return EntityNoteResponse(
        entity_kind=body.entity_kind, entity_id=body.entity_id, note=note,
    )


# ── VAPI tool endpoints ───────────────────────────────────────────────────────

@router.post("/{clinic_id}/patient/lookup")
def lookup_patient(
    clinic_id: str,
    body: LookupPatientRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """CTI trigger: opens the patient's file in Blueprint for the receptionist."""
    config = _get_blueprint_config(db, clinic_id)
    base = _blueprint_base(config)
    user_id = _int_field(config, "user_id", default=1)
    callerid = "".join(c for c in body.caller_phone if c.isdigit())

    try:
        httpx.get(
            f"{base}/client/show",
            params={
                "apiKey": config["api_key"],
                "event": "ringing",
                "user": str(user_id),
                "callerid": callerid,
            },
            timeout=10,
        )
    except httpx.RequestError:
        pass  # Best-effort — the UI trigger failing shouldn't block the call

    return {"triggered": True}


@router.post("/{clinic_id}/availability")
def check_availability(
    clinic_id: str,
    body: AvailabilityRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """Return available appointment slots for a date range and event type."""
    config = _get_blueprint_config(db, clinic_id)
    base = _blueprint_base(config)
    location_id = _int_field(config, "location_id")

    tz = ZoneInfo(config.get("timezone") or "America/Vancouver")
    start_dt = datetime.strptime(body.start_date, "%Y-%m-%d").replace(tzinfo=tz)
    end_dt = (datetime.strptime(body.end_date, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=tz)

    params: dict = {
        "apiKey": config["api_key"],
        "startTime": int(start_dt.timestamp()),
        "endTime": int(end_dt.timestamp()),
        "eventTypeId": body.event_type_id,
        "bookingTimeSlotInterval": "30",
        "minimumAdvanceBookingTime": "60",
    }
    if location_id:
        params["locations"] = location_id

    resp = httpx.get(f"{base}/availability/", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


@router.post("/{clinic_id}/availability/search")
def search_availability(
    clinic_id: str,
    body: AvailabilitySearchRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """
    Search scheduled provider availability blocks in a date range.

    Proxies Blueprint: POST /rest/availability/search. Returns summary info
    about availability blocks (when providers are scheduled to work) — NOT
    bookable appointment slots. Use `check_availability` for bookable slots
    tied to a specific event type; use this endpoint for broad "when does the
    clinic have capacity next week?" questions.
    """
    config = _get_blueprint_config(db, clinic_id)
    base = _blueprint_base(config)

    tz = ZoneInfo(config.get("timezone") or "America/Vancouver")
    start_dt = datetime.strptime(body.start_date, "%Y-%m-%d").replace(tzinfo=tz)
    end_dt = (datetime.strptime(body.end_date, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=tz)

    payload: dict = {
        "apiKey": config["api_key"],
        "startTime": int(start_dt.timestamp()),
        "endTime": int(end_dt.timestamp()),
    }

    # If the caller didn't specify locations, fall back to the clinic's
    # configured location_id. Matches check_availability's behaviour.
    if body.locations is not None:
        payload["locations"] = body.locations
    else:
        location_id = _int_field(config, "location_id")
        if location_id:
            payload["locations"] = [location_id]

    if body.available_for_online_booking_only is not None:
        payload["availableForOnlineBookingOnly"] = body.available_for_online_booking_only

    resp = httpx.post(f"{base}/availability/search", json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Voice agent v1: list_appointment_types + find_available_slots ─────────────


@router.post("/{clinic_id}/appointment-types")
def list_appointment_types(
    clinic_id: str,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """
    Voice-agent capability: list the clinic's bookable appointment types.

    Returns a stripped list — id, name, duration_minutes — that the agent
    uses to map a caller's stated need to an event_type_id before calling
    `find_available_slots`. The PMS call lives in `BlueprintAdapter`; this
    endpoint is the HTTP boundary.
    """
    adapter = BlueprintAdapter(clinic_id=clinic_id)
    adapter.load_http_config(db)
    types = adapter.list_appointment_types()
    return {
        "appointment_types": [
            {"id": t.id, "name": t.name, "duration_minutes": t.duration_minutes}
            for t in types
        ],
    }


class LocationItem(BaseModel):
    id: int
    name: str | None = None
    address: str | None = None


class ListLocationsResponse(BaseModel):
    locations: list[LocationItem]
    prompt_for_location: bool   # whether the agent should ask the caller to choose


@router.post("/{clinic_id}/locations", response_model=ListLocationsResponse)
def list_locations(
    clinic_id: str,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """
    Voice-agent capability: list the clinic's bookable locations.

    ``prompt_for_location`` tells the agent whether this clinic wants the
    caller asked which location to book into before searching availability.
    Single-location clinics still return their one location so the agent
    can resolve it silently.
    """
    config = _get_blueprint_config(db, clinic_id)
    adapter = BlueprintAdapter(clinic_id=clinic_id, http_config=config)
    locations = adapter.list_locations()
    return ListLocationsResponse(
        locations=[
            LocationItem(id=loc.id, name=loc.name, address=loc.address)
            for loc in locations
        ],
        prompt_for_location=bool(config.get("prompt_for_location")),
    )


@router.post("/{clinic_id}/availability/find")
def find_available_slots(
    clinic_id: str,
    body: FindAvailableSlotsRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """
    Voice-agent capability: find concrete bookable time slots in a date range
    for a specific appointment type.

    Response is aggressively stripped — only date + bookable times reach the
    agent. The PMS call (and the strip) live in `BlueprintAdapter`.

    Every provider's scheduled availability is directly bookable; the slot
    search hits Blueprint's GET ``/rest/availability/`` over the requested
    window with no online-booking pre-filter.
    """
    adapter = BlueprintAdapter(clinic_id=clinic_id)
    adapter.load_http_config(db)
    result = adapter.find_available_slots(
        event_type_id=body.event_type_id,
        start_date=body.start_date,
        end_date=body.end_date,
        providers=body.providers,
        locations=body.locations,
    )
    return {
        "days": [
            {"date": d.date, "available_times": d.available_times}
            for d in result.days
        ],
    }


# ── ACNA placeholder-grid availability + booking ──────────────────────────────
#
# For clinics whose bookable capacity is encoded as placeholder appointments in
# the schedule grid (see api/voice_agent/pms/blueprint.py). The placeholder↔real
# type pairing is per-clinic config on the acna_search_availability protocol; we
# resolve it server-side so the agent only ever deals with real_event_type_ids.


def _placeholder_pairs(db: Session, clinic_id: str) -> dict[int, "TypePair"]:
    """Map real_event_type_id → TypePair from the clinic's protocol config.

    Reads the config on the SEARCH protocol (the book protocol shares it).
    Falls back to the config model's defaults (seeded with the Annual pair)
    when no row exists yet.
    """
    from api.voice_agent.protocols import load_protocol_config
    from api.voice_agent.protocols.acna_placeholder import (
        ACNASearchAvailabilityProtocol,
        TypePair,
    )

    cfg = load_protocol_config(db, clinic_id, ACNASearchAvailabilityProtocol.id)
    return {p.real_event_type_id: p for p in cfg.type_pairs}


def _resolve_pair(db: Session, clinic_id: str, real_event_type_id: int) -> "TypePair":
    pairs = _placeholder_pairs(db, clinic_id)
    pair = pairs.get(real_event_type_id)
    if pair is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"real_event_type_id={real_event_type_id} is not a configured "
                "placeholder-grid appointment type for this clinic"
            ),
        )
    return pair


def _excluded_providers(db: Session, clinic_id: str) -> list[str]:
    """Pseudo-providers whose placeholder slots can't be booked via the API
    (no availability schedule → BOOKING_OUTSIDE_AVAILABILITY). From the search
    protocol's config."""
    from api.voice_agent.protocols import load_protocol_config
    from api.voice_agent.protocols.acna_placeholder import (
        ACNASearchAvailabilityProtocol,
    )

    cfg = load_protocol_config(db, clinic_id, ACNASearchAvailabilityProtocol.id)
    return list(cfg.excluded_placeholder_providers or [])


class PlaceholderFindRequest(BaseModel):
    real_event_type_id: int
    start_date: str          # YYYY-MM-DD (clinic-local)
    end_date: str            # YYYY-MM-DD (clinic-local, inclusive)
    provider_name: str | None = None


class PlaceholderBookRequest(BaseModel):
    real_event_type_id: int
    start_date: str          # YYYY-MM-DD (clinic-local)
    start_time: str          # HH:MM (clinic-local)
    provider_name: str | None = None
    patient_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    notes: str | None = None


@router.post("/{clinic_id}/placeholder/appointment-types")
def placeholder_appointment_types(
    clinic_id: str,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """List the clinic's placeholder-grid bookable types (config-driven).

    Returns {real_event_type_id, name, duration_minutes}. ``name`` is the
    config display name (what the agent matches against); duration comes from
    Blueprint when the real type is known there.
    """
    pairs = _placeholder_pairs(db, clinic_id)
    adapter = BlueprintAdapter(clinic_id=clinic_id)
    adapter.load_http_config(db)
    durations = {t.id: t.duration_minutes for t in adapter.list_appointment_types()}
    return {
        "appointment_types": [
            {
                "real_event_type_id": p.real_event_type_id,
                "name": p.display_name,
                "duration_minutes": durations.get(p.real_event_type_id),
            }
            for p in pairs.values()
        ],
    }


@router.post("/{clinic_id}/placeholder/availability/find")
def placeholder_find_availability(
    clinic_id: str,
    body: PlaceholderFindRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """Grid-derived bookable slots for a placeholder/real type pair.

    Availability is computed from the schedule grid's placeholder appointments
    (not Blueprint online-booking availability). Response is
    {days: [{date, slots: [{time, providers: [...]}]}]}.
    """
    pair = _resolve_pair(db, clinic_id, body.real_event_type_id)
    adapter = BlueprintAdapter(clinic_id=clinic_id)
    adapter.load_http_config(db)
    result = adapter.find_placeholder_availability(
        placeholder_event_type_id=pair.placeholder_event_type_id,
        real_event_type_id=pair.real_event_type_id,
        start_date=body.start_date,
        end_date=body.end_date,
        provider_name=body.provider_name,
        excluded_providers=_excluded_providers(db, clinic_id),
    )
    return {
        "days": [
            {
                "date": d.date,
                "slots": [
                    {"time": s.time, "providers": s.providers} for s in d.slots
                ],
            }
            for d in result.days
        ],
    }


@router.post(
    "/{clinic_id}/placeholder/appointments/book",
    response_model=BookingResultResponse,
)
def placeholder_book_appointment(
    clinic_id: str,
    body: PlaceholderBookRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """Book a real appointment into a free placeholder space.

    Resolves the provider + room from the grid; end time is derived from the
    real type's duration. Returns 409 (surfaced to the agent) when no free
    placeholder matches so the agent can fall back to a ticket.
    """
    pair = _resolve_pair(db, clinic_id, body.real_event_type_id)
    adapter = BlueprintAdapter(clinic_id=clinic_id)
    adapter.load_http_config(db)
    result = adapter.book_into_placeholder(
        placeholder_event_type_id=pair.placeholder_event_type_id,
        real_event_type_id=pair.real_event_type_id,
        start_date=body.start_date,
        start_time=body.start_time,
        provider_name=body.provider_name,
        patient_id=body.patient_id,
        first_name=body.first_name,
        last_name=body.last_name,
        phone=body.phone,
        notes=body.notes,
        excluded_providers=_excluded_providers(db, clinic_id),
    )
    if body.patient_id:
        log_phi_access(
            clinic_id=clinic_id,
            action="appointment_book",
            patient_id=body.patient_id,
            outcome=result.status,
            detail=f"event_type={pair.real_event_type_id}",
        )
    return BookingResultResponse(
        status=result.status,
        appointment_id=result.appointment_id,
        summary=result.summary,
        start_time=result.start_time,
        end_time=result.end_time,
        warning=result.warning,
    )


# ── ACNA appointment-type decision ─────────────────────────────────────────────


class AppointmentDecisionRequest(BaseModel):
    patient_id: str
    # Caller's answer after a need_payer round-trip; omit on the first call.
    payer_type: Literal["WCB", "Veterans Affairs", "Other"] | None = None


@router.post("/{clinic_id}/appointment-decision")
def appointment_decision(
    clinic_id: str,
    body: AppointmentDecisionRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """Run the clinic's deterministic appointment-type decision for an existing
    patient. See ``api/voice_agent/appointment_decision.py`` for the rule spec.

    Responses:
      {status:"need_payer", options:[...]} — multiple payer programs on file;
        the agent asks the caller and retries with ``payer_type``.
      {status:"decided", outcome, reason, real_event_type_id, bookable,
       earliest_bookable_date, self_pay_price, patient_context}
        — ``bookable`` is true only when the outcome maps to a
        real_event_type_id that also has a placeholder pair configured. The
        three ``REFER_TO_STAFF_*`` outcomes are never bookable by design (not a
        phase-1 gap): prior authorization, payer review, and under-age callers
        all require a person. ``earliest_bookable_date`` is set when the
        clean-and-check gap pushes the window out; ``self_pay_price`` only on
        the self-pay offer.
    """
    from api.voice_agent.appointment_decision import (
        DecisionInputs, DecisionRules, decide, resolve_payer,
    )
    from api.voice_agent.protocols import load_protocol_config
    from api.voice_agent.protocols.acna_appointment_decision import (
        ACNADetermineAppointmentProtocol,
    )

    if not (body.patient_id or "").strip():
        raise HTTPException(status_code=400, detail="patient_id is required")

    cfg = load_protocol_config(db, clinic_id, ACNADetermineAppointmentProtocol.id)

    adapter = BlueprintAdapter(clinic_id=clinic_id)
    adapter.load_http_config(db)
    inputs_raw = adapter.get_decision_inputs(
        patient_id=body.patient_id.strip(),
        clinician_names=cfg.clinician_names,
    )

    # An unknown patient must NOT fall through as "no history → due for an
    # annual" — empty subquery results would look exactly like a never-tested
    # patient. Fail loudly; the agent's error handling routes to a message.
    if not inputs_raw["patient_exists"]:
        log_phi_access(
            clinic_id=clinic_id,
            action="appointment_decision",
            patient_id=body.patient_id,
            outcome="patient_not_found",
            detail="",
        )
        raise HTTPException(
            status_code=404,
            detail="Patient not found for this clinic — re-verify the caller's identity.",
        )

    payer, options = resolve_payer(
        inputs_raw["active_insurer_names"], stated_payer=body.payer_type,
    )
    if payer is None:
        log_phi_access(
            clinic_id=clinic_id,
            action="appointment_decision",
            patient_id=body.patient_id,
            outcome="need_payer",
            detail=f"options={options}",
        )
        return {
            "status": "need_payer",
            "options": options,
            "question_hint": (
                "Ask which program this visit falls under, e.g. "
                f"{' or '.join(options)}, otherwise 'Other'."
            ),
        }

    tz = ZoneInfo(adapter._require_http_config().get("timezone") or "America/Vancouver")
    today = datetime.now(tz).date()
    expiry = inputs_raw["care_plan_expiry"]

    decision = decide(
        DecisionInputs(
            today=today,
            patient_is_existing=True,  # endpoint requires a matched patient_id
            care_plan_name=inputs_raw["care_plan_name"],
            care_plan_active=bool(expiry and expiry >= today),
            payer_type=payer,
            last_hearing_test_date=inputs_raw["last_hearing_test_date"],
            last_clinician_visit_date=inputs_raw["last_clinician_visit_date"],
            last_clean_check_date=inputs_raw["last_clean_check_date"],
            date_of_birth=inputs_raw["date_of_birth"],
            warranty_expiry_date=inputs_raw["warranty_expiry_date"],
        ),
        DecisionRules(
            qualifying_plan_names=tuple(cfg.qualifying_plan_names),
            non_qualifying_plan_names=tuple(cfg.non_qualifying_plan_names),
            unknown_plan_allows_annual=cfg.unknown_plan_allows_annual,
            expired_qualifying_allows_annual=cfg.expired_qualifying_allows_annual,
            payer_min_years=dict(cfg.payer_min_years),
            payer_actions=dict(cfg.payer_actions),
            clinician_visit_threshold_years=cfg.clinician_visit_threshold_years,
            self_pay_annual_price=cfg.self_pay_annual_price,
            minor_age_threshold=cfg.minor_age_threshold,
            min_months_after_clean_check=cfg.min_months_after_clean_check,
            warranty_bundle_trigger_months=cfg.warranty_bundle_trigger_months,
            warranty_bundle_window_months=cfg.warranty_bundle_window_months,
        ),
    )

    real_id = cfg.outcome_booking.get(decision.outcome)
    bookable = real_id is not None and real_id in _placeholder_pairs(db, clinic_id)

    # The full rule trace goes to the audit log, NOT the tool response — the
    # response lands in the LLM transcript, which should carry only what the
    # agent needs. ``patient_context`` is the deliberate exception: a curated,
    # patient-facing set of facts about the VERIFIED caller's own record (plan
    # name, last test, next funded due date) so the agent can answer "when am
    # I due?" / "what's my plan?" instead of stonewalling. Rule internals and
    # anything beyond these fields stay out.
    log_phi_access(
        clinic_id=clinic_id,
        action="appointment_decision",
        patient_id=body.patient_id,
        outcome=decision.outcome,
        detail=f"payer={payer} bookable={bookable} trace={json.dumps(decision.trace)}",
    )
    expiry = inputs_raw["care_plan_expiry"]
    last_test = inputs_raw["last_hearing_test_date"]
    return {
        "status": "decided",
        "outcome": decision.outcome,
        "reason": decision.reason,
        "real_event_type_id": real_id if bookable else None,
        "bookable": bookable,
        # HARD floor (clean-and-check gap) — the agent may never offer earlier.
        "earliest_bookable_date": (
            decision.earliest_bookable_date.isoformat()
            if decision.earliest_bookable_date else None
        ),
        # SOFT warranty-bundling target. The agent offers inside this window
        # first and explains why, but books outside it if the caller can't make
        # it — deliberately not merged with the hard floor above, because the
        # two must be negotiable to different degrees.
        "preferred_window": (
            {
                "start": decision.preferred_earliest_date.isoformat(),
                "end": decision.preferred_latest_date.isoformat(),
                "reason": "warranty_bundling",
                "warranty_expiry": (
                    decision.warranty_expiry_date.isoformat()
                    if decision.warranty_expiry_date else None
                ),
            }
            if decision.preferred_earliest_date and decision.preferred_latest_date
            else None
        ),
        # Only present on the self-pay offer — the price the agent must quote
        # and get agreement on before booking.
        "self_pay_price": decision.self_pay_price,
        "patient_context": {
            "care_plan": inputs_raw["care_plan_name"],
            "care_plan_active": bool(expiry and expiry >= today),
            "annual_covered_by_plan": decision.annual_covered_by_plan,
            "last_hearing_test": last_test.isoformat() if last_test else None,
            "next_annual_due": (
                decision.next_annual_due.isoformat()
                if decision.next_annual_due else None
            ),
            "payer_program": payer,
            "funded_test_interval_years": (
                cfg.payer_min_years.get(payer)
                or cfg.payer_min_years.get("Other", 1.0)
            ),
        },
    }


@router.post(
    "/{clinic_id}/appointments/locate",
    response_model=LocateAppointmentResponse,
)
def locate_appointment(
    clinic_id: str,
    body: LocateAppointmentRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """Return an existing patient's upcoming appointments.

    Used by the Cancel / Reschedule protocols (and standalone, for callers
    asking "do I have anything booked?"). The agent never sees the
    Blueprint ``onlineBookingSecret`` — it's recovered server-side when a
    cancel/reschedule is issued.
    """
    adapter = BlueprintAdapter(clinic_id=clinic_id)
    adapter.load_http_config(db)
    appts = adapter.list_patient_appointments(
        patient_id=body.patient_id,
        days_back=body.days_back,
        days_ahead=body.days_ahead,
    )
    log_phi_access(
        clinic_id=clinic_id,
        action="appointment_locate",
        patient_id=body.patient_id,
        outcome="ok",
        detail=f"appointments={len(appts)}",
    )
    return LocateAppointmentResponse(
        appointments=[
            LocateAppointmentItem(
                appointment_id=a.appointment_id,
                event_type_name=a.event_type_name,
                summary=a.summary,
                start_time=a.start_time,
                end_time=a.end_time,
                provider_name=a.provider_name,
                location_name=a.location_name,
                status=a.status,
            )
            for a in appts
        ]
    )


@router.post(
    "/{clinic_id}/appointments/book",
    response_model=BookingResultResponse,
)
def book_appointment(
    clinic_id: str,
    body: BookAppointmentRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """Create an appointment in Blueprint OMS."""
    adapter = BlueprintAdapter(clinic_id=clinic_id)
    adapter.load_http_config(db)
    result = adapter.book(
        event_type_id=body.event_type_id,
        start_date=body.start_date,
        start_time=body.start_time,
        location_id=body.location_id,
        patient_id=body.patient_id,
        first_name=body.first_name,
        last_name=body.last_name,
        phone=body.phone,
        notes=body.notes,
    )
    return BookingResultResponse(
        status=result.status,
        appointment_id=result.appointment_id,
        summary=result.summary,
        start_time=result.start_time,
        end_time=result.end_time,
        warning=result.warning,
    )


@router.post(
    "/{clinic_id}/appointments/cancel",
    response_model=BookingResultResponse,
)
def cancel_appointment(
    clinic_id: str,
    body: CancelAppointmentRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """Cancel an appointment via Blueprint Edit Appointment (PUT, status=3).

    Internally re-fetches the appointment via Search to recover the
    ``onlineBookingSecret`` — that credential never crosses the agent's
    view.
    """
    adapter = BlueprintAdapter(clinic_id=clinic_id)
    adapter.load_http_config(db)
    result = adapter.cancel(appointment_id=body.appointment_id)
    return BookingResultResponse(
        status=result.status,
        appointment_id=result.appointment_id,
        summary=result.summary,
        start_time=result.start_time,
        end_time=result.end_time,
        warning=result.warning,
    )


@router.post(
    "/{clinic_id}/appointments/reschedule",
    response_model=BookingResultResponse,
)
def reschedule_appointment(
    clinic_id: str,
    body: RescheduleAppointmentRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """Move an appointment to a new slot.

    Blueprint's Edit endpoint cannot change start/end time, so this is a
    book-new-then-cancel-old flow. If the new booking succeeds but the
    old cancel fails, returns ``status="partial"`` with a warning so the
    agent can flag cleanup to the caller and the ticket can capture it.
    """
    adapter = BlueprintAdapter(clinic_id=clinic_id)
    adapter.load_http_config(db)
    result = adapter.reschedule(
        appointment_id=body.appointment_id,
        new_start_date=body.new_start_date,
        new_start_time=body.new_start_time,
    )
    return BookingResultResponse(
        status=result.status,
        appointment_id=result.appointment_id,
        summary=result.summary,
        start_time=result.start_time,
        end_time=result.end_time,
        warning=result.warning,
    )


# ── Patient name match (voice agent v1) ───────────────────────────────────────


class PatientMatchRequest(BaseModel):
    first_name: str
    last_name: str
    last4_phone: str
    dob: str | None = None  # YYYY-MM-DD; optional tie-breaker when ambiguous


class PatientMatchResponse(BaseModel):
    status: Literal["matched", "ambiguous", "unmatched"]
    patient_id: str | None = None
    candidates_count: int


class PatientJournalRequest(BaseModel):
    patient_id: str


class JournalEntryItem(BaseModel):
    entry_time: str
    entry_type: str | None = None
    text: str


class PatientJournalResponse(BaseModel):
    entries: list[JournalEntryItem]


@router.post("/{clinic_id}/patient/journal", response_model=PatientJournalResponse)
def get_patient_journal(
    clinic_id: str,
    body: PatientJournalRequest,
    _: None = Depends(verify_vapi_secret),
):
    """
    Recent patient journal entries for an identified caller — background
    context for the voice agent.

    **The _clinic_id filter is mandatory and non-negotiable** — same PHI
    isolation rule as patient/match. Only call this after
    verify_caller_identification returned 'matched'; ``patient_id`` is the
    opaque id it returned. The BQ query (bounds: non-deleted, last 24 months,
    10 most recent) lives in ``BlueprintAdapter.get_patient_journal``; this
    endpoint is the HTTP boundary and (like patient/match) needs no Cloud SQL.
    """
    adapter = BlueprintAdapter(clinic_id=clinic_id)
    entries = adapter.get_patient_journal(patient_id=body.patient_id)
    log_phi_access(
        clinic_id=clinic_id,
        action="patient_journal",
        patient_id=body.patient_id,
        outcome="ok",
        detail=f"entries={len(entries)}",
    )
    return PatientJournalResponse(
        entries=[
            JournalEntryItem(
                entry_time=e.entry_time,
                entry_type=e.entry_type,
                text=e.text,
            )
            for e in entries
        ]
    )


@router.post("/{clinic_id}/patient/match", response_model=PatientMatchResponse)
def match_patient_by_name(
    clinic_id: str,
    body: PatientMatchRequest,
    _: None = Depends(verify_vapi_secret),
):
    """
    Server-side patient match against Blueprint_PHI.ClientDemographics.

    **The _clinic_id filter is mandatory and non-negotiable** — a match for a
    patient belonging to clinic A must never be returnable when querying
    clinic B's endpoint. This is a PHI isolation requirement, not a style
    choice.

    The BQ query lives in `BlueprintAdapter.find_patient`. This endpoint is
    the HTTP boundary — it does not touch Cloud SQL (preserves prior behavior:
    the adapter constructor takes only `clinic_id` and the BQ query keys off
    `_clinic_id` directly).
    """
    adapter = BlueprintAdapter(clinic_id=clinic_id)
    result = adapter.find_patient(
        first_name=body.first_name,
        last_name=body.last_name,
        last4_phone=body.last4_phone,
        dob=body.dob,
    )
    log_phi_access(
        clinic_id=clinic_id,
        action="patient_match",
        patient_id=result.patient_id,
        outcome=result.status,
        detail=f"candidates={result.candidates_count}",
    )
    return PatientMatchResponse(
        status=result.status,
        patient_id=result.patient_id,
        candidates_count=result.candidates_count,
    )
