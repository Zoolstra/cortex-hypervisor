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
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.deps import require_read_access, verify_token
from api.core.db import get_session
from api.core.secrets import get_secret
from api.voice_agent.pms import AvailabilityFilters
from api.voice_agent.pms.blueprint import (
    BlueprintAdapter,
    _blueprint_base,
    _get_blueprint_config,
    _int_field,
)
from api.voice_agent.protocols import load_protocol_config

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

@router.get("/{clinic_id}/clinic-config")
def get_clinic_config(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    Fetch Blueprint clinic configuration: appointment types, providers, locations.
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

    return {
        "appointment_types": [
            {
                "id": t["id"],
                "name": t.get("name"),
                "duration_minutes": t.get("duration"),
                "description": t.get("description", ""),
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

    Per-clinic policy comes from ``clinic_protocols.config`` for
    ``search_appointment_availability``. Today the only knob is
    ``online_booking_only`` (ACNA's request) — when set, the adapter
    restricts slots to providers Blueprint reports as online-bookable.
    """
    cfg = load_protocol_config(db, clinic_id, "search_appointment_availability")
    adapter = BlueprintAdapter(clinic_id=clinic_id)
    adapter.load_http_config(db)
    result = adapter.find_available_slots(
        event_type_id=body.event_type_id,
        start_date=body.start_date,
        end_date=body.end_date,
        providers=body.providers,
        locations=body.locations,
        filters=AvailabilityFilters(online_booking_only=cfg.online_booking_only),
    )
    return {
        "days": [
            {"date": d.date, "available_times": d.available_times}
            for d in result.days
        ],
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
    return PatientMatchResponse(
        status=result.status,
        patient_id=result.patient_id,
        candidates_count=result.candidates_count,
    )
