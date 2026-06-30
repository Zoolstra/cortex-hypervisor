"""
Per-clinic intelligence report endpoint.

``GET /intelligence/{clinic_id}/report.html`` — admin / super_admin only —
returns a fully-rendered HTML document built from Blueprint_PHI + ClinicData.
The report module is stdlib-only (no pandas/plotly) so it can live inside the
hypervisor container without bloating the image.
"""
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.audit import log_phi_access
from api.core.db import get_session
from api.core.orm import Clinic, GoogleAdsCampaign, Instance, InvocaCampaign
from api.deps import require_read_access, require_write_access, verify_token

router = APIRouter()


def _resolve_window(start: str | None, end: str | None, days: int):
    """Build a queries.Window from explicit ``start``/``end`` (inclusive
    ``YYYY-MM-DD``) or fall back to the legacy ``days`` look-back. The start is
    clamped up to the hard ``MIN_WINDOW_DATE`` cutoff so no reader scans before
    it. 422 on a malformed / inverted / entirely-pre-cutoff range."""
    from intelligence_report.queries import MIN_WINDOW_DATE, Window

    if start and end:
        try:
            w = Window(start, end)
        except ValueError:
            raise HTTPException(status_code=422, detail="start/end must be YYYY-MM-DD dates")
        if w.end_excl <= w.start:
            raise HTTPException(status_code=422, detail="end must be on or after start")
        if w.span_days > 1826:
            raise HTTPException(status_code=422, detail="date range too large (max ~5 years)")
    else:
        w = Window.from_days(max(7, min(int(days), 1825)))

    floored = w.floored(MIN_WINDOW_DATE)
    if floored is None:
        raise HTTPException(
            status_code=422,
            detail=f"date range is entirely before the {MIN_WINDOW_DATE.isoformat()} minimum",
        )
    return floored


def _active_campaign_ids(db: Session, clinic_id: str) -> tuple[list[str], list[str]]:
    """(invoca_campaign_ids, google_ads_campaign_ids) — active only, as strings."""
    invoca = list(db.scalars(
        select(InvocaCampaign.invoca_campaign_id).where(
            InvocaCampaign.clinic_id == clinic_id, InvocaCampaign.active.is_(True))))
    gads = list(db.scalars(
        select(GoogleAdsCampaign.google_ads_campaign_id).where(
            GoogleAdsCampaign.clinic_id == clinic_id, GoogleAdsCampaign.active.is_(True))))
    return [str(c) for c in invoca], [str(c) for c in gads]


# ── Group Intelligence (multi-location, instance-scoped) ─────────────────────

def _group_clinics(db: Session, instance_id: str) -> list[tuple[str, str, str]]:
    """Active (non-deleted) clinics for the instance as ``(id, name, pms_type)``."""
    rows = db.execute(
        select(Clinic.clinic_id, Clinic.clinic_name, Clinic.pms_type).where(
            Clinic.instance_id == instance_id, Clinic.deleted_at.is_(None))
    ).all()
    return [(r[0], r[1], r[2] or "none") for r in rows]


def _group_ga_campaigns(db: Session, instance_id: str) -> dict[str, list[str]]:
    """{clinic_id: [active google_ads_campaign_id, …]} for every clinic in the
    instance — one query, grouped in Python."""
    rows = db.execute(
        select(GoogleAdsCampaign.clinic_id, GoogleAdsCampaign.google_ads_campaign_id)
        .join(Clinic, Clinic.clinic_id == GoogleAdsCampaign.clinic_id)
        .where(Clinic.instance_id == instance_id,
               Clinic.deleted_at.is_(None),
               GoogleAdsCampaign.active.is_(True))
    ).all()
    out: dict[str, list[str]] = {}
    for clinic_id, campaign_id in rows:
        out.setdefault(clinic_id, []).append(str(campaign_id))
    return out


# Small in-process TTL cache for the (expensive, LLM-bearing) JSON payloads.
# Keyed on everything that changes the result; mirrors the HTML report's cache.
_JSON_TTL = 300.0
_json_cache: dict[tuple, tuple[float, dict]] = {}


def _cache_get(key: tuple):
    hit = _json_cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _JSON_TTL:
        return hit[1]
    return None


def _cache_put(key: tuple, value: dict):
    _json_cache[key] = (time.monotonic(), value)


@router.get("/intelligence/{clinic_id}/report.html")
def get_intelligence_report(
    clinic_id: str,
    days: int = 365,
    nocache: bool = False,
    utm_source: list[str] = Query(default=[]),
    utm_medium: list[str] = Query(default=[]),
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Generate and return the clinic intelligence report as HTML.

    On-demand generation with an in-process TTL cache (5 minutes) so repeated
    reloads of the same ``(clinic_id, days, utm filters)`` don't re-run the
    BigQuery pipeline. Pass ``?nocache=1`` to bypass the cache for a fresh render.

    ``?utm_source=`` / ``?utm_medium=`` (repeatable) are multi-select include-lists
    that filter the §03 call funnel (source AND medium); other sections are
    unaffected.
    """
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    invoca_campaign_ids = list(
        db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    )
    google_ads_campaign_ids = list(
        db.scalars(
            select(GoogleAdsCampaign.google_ads_campaign_id).where(
                GoogleAdsCampaign.clinic_id == clinic_id,
                GoogleAdsCampaign.active.is_(True),
            )
        )
    )

    # Lazy import — keeps the report module out of the hot start path for any
    # other endpoint that doesn't need it.
    from intelligence_report.report import generate_report_with_campaigns

    html = generate_report_with_campaigns(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=[str(c) for c in invoca_campaign_ids],
        google_ads_campaign_ids=[str(c) for c in google_ads_campaign_ids],
        days=max(7, min(int(days), 1825)),
        use_cache=not nocache,
        utm_sources=utm_source,
        utm_mediums=utm_medium,
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


@router.get("/intelligence/{clinic_id}/spam-calls.html")
def get_spam_calls_report(
    clinic_id: str,
    days: int = 90,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Drill-down detail: every inbound call flagged as spam by the four
    heuristics shared with the main funnel.

    Gated by the same read access check as the main report.
    """
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    invoca_campaign_ids = list(
        db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    )

    from intelligence_report.report import generate_spam_calls_report

    html = generate_spam_calls_report(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=[str(c) for c in invoca_campaign_ids],
        days=max(7, min(int(days), 1825)),
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


@router.get("/intelligence/{clinic_id}/no-conversation-calls.html")
def get_no_conversation_report(
    clinic_id: str,
    days: int = 90,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Drill-down detail: per-call rows where the funnel ended at
    'No Conversation' (voicemail / hangup / silent autodial)."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    invoca_campaign_ids = list(
        db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    )

    from intelligence_report.report import generate_no_conversation_report

    html = generate_no_conversation_report(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=[str(c) for c in invoca_campaign_ids],
        days=max(7, min(int(days), 1825)),
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


@router.get("/intelligence/{clinic_id}/qualified-no-conv-calls.html")
def get_qualified_no_conv_report(
    clinic_id: str,
    days: int = 90,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Drill-down detail: per-call rows where a qualified lead engaged but
    didn't book. The main conversion-leak surface."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    invoca_campaign_ids = list(
        db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    )

    from intelligence_report.report import generate_qualified_no_conv_report

    html = generate_qualified_no_conv_report(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=[str(c) for c in invoca_campaign_ids],
        days=max(7, min(int(days), 1825)),
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


@router.get("/intelligence/{clinic_id}/attributed-invoices.html")
def get_attributed_invoices_report(
    clinic_id: str,
    days: int = 365,
    booking_window_hours: int = 24,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Drill-down detail: every invoice attributable to a tracked phone call,
    one row per (call × invoice).

    PHI-heavy (patient name, client_id, phone). Gated by the same read access
    check as the main report.
    """
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    invoca_campaign_ids = list(
        db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    )

    from intelligence_report.report import generate_attributed_invoices_report

    html = generate_attributed_invoices_report(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=[str(c) for c in invoca_campaign_ids],
        days=max(7, min(int(days), 1825)),
        booking_window_hours=max(1, min(int(booking_window_hours), 720)),
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


# ── JSON endpoints for the React intelligence pages ──────────────────────────

def _location_hours(clinic: Clinic) -> dict | None:
    loc = getattr(clinic, "location", None)
    if loc is None:
        return None
    from intelligence_report.clinic_hours import WEEKDAY_ATTRS
    return {attr: getattr(loc, attr, None) for attr in WEEKDAY_ATTRS}


@router.get("/intelligence/{clinic_id}/overview")
def get_intelligence_overview(
    clinic_id: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 365,
    nocache: bool = False,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """5-section Intelligence Overview payload (JSON), driven by the global
    date range (``?start=&end=`` inclusive ``YYYY-MM-DD``; falls back to
    ``?days=``)."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    window = _resolve_window(start, end, days)
    invoca_ids, gads_ids = _active_campaign_ids(db, clinic_id)
    tier = getattr(clinic, "tier", "none") or "none"
    hours = _location_hours(clinic)

    key = ("overview", clinic_id, window.start_date, window.end_date_excl, tier)
    if not nocache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    from intelligence_report.payloads import build_overview

    payload = build_overview(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=invoca_ids,
        ga_campaign_ids=gads_ids,
        window=window,
        location_hours=hours,
        tier=tier,
    )
    _cache_put(key, payload)
    return payload


@router.get("/intelligence/group/{instance_id}/overview")
def get_group_overview(
    instance_id: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 365,
    nocache: bool = False,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Multi-location "Group Intelligence" payload (JSON) — a leaderboard +
    roll-up across all of an instance's clinics, driven by the global date range.

    Gated by the instance ``multi_location_group`` capability flag: when off, the
    endpoint 404s (not 403) so the whole section is invisible to instances that
    don't have it, not merely empty. Aggregate-only (counts / sums / labels) — no
    PHI, so no audit path; patient-level drill-down stays on the per-clinic
    ``/intelligence/{clinic_id}/patients/…`` endpoints."""
    instance = db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    require_read_access(instance_id, caller)
    if not getattr(instance, "multi_location_group", False):
        raise HTTPException(status_code=404, detail="Not found")

    window = _resolve_window(start, end, days)
    clinics = _group_clinics(db, instance_id)
    ga_by_clinic = _group_ga_campaigns(db, instance_id)

    key = ("group-overview", instance_id, window.start_date, window.end_date_excl)
    if not nocache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    from intelligence_report.payloads import build_group_overview

    payload = build_group_overview(
        instance_id=instance_id,
        instance_name=instance.instance_name,
        clinics=clinics,
        ga_campaign_ids_by_clinic=ga_by_clinic,
        window=window,
    )
    _cache_put(key, payload)
    return payload


@router.get("/intelligence/{clinic_id}/acquisition")
def get_patient_acquisition(
    clinic_id: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 365,
    nocache: bool = False,
    utm_source: list[str] = Query(default=[]),
    utm_medium: list[str] = Query(default=[]),
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Patient Acquisition payload (JSON) — the React form of the acquisition /
    funnel report, driven by the global date range and optional UTM filters."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    window = _resolve_window(start, end, days)
    invoca_ids, gads_ids = _active_campaign_ids(db, clinic_id)

    key = ("acquisition", clinic_id, window.start_date, window.end_date_excl,
           tuple(sorted(utm_source)), tuple(sorted(utm_medium)))
    if not nocache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    from intelligence_report.payloads import build_acquisition

    payload = build_acquisition(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=invoca_ids,
        ga_campaign_ids=gads_ids,
        window=window,
        utm_sources=utm_source,
        utm_mediums=utm_medium,
    )
    _cache_put(key, payload)
    return payload


@router.get("/intelligence/{clinic_id}/active-leads")
def get_active_leads(
    clinic_id: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 90,
    nocache: bool = False,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Scored active-leads recovery inbox (JSON) — open call + form leads ranked
    by expected recoverable revenue, for the selected date range."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    window = _resolve_window(start, end, days)
    invoca_ids, _ = _active_campaign_ids(db, clinic_id)
    hours = _location_hours(clinic)

    key = ("active-leads", clinic_id, window.start_date, window.end_date_excl)
    if not nocache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    from intelligence_report.active_leads import build_active_leads

    payload = build_active_leads(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=invoca_ids,
        window=window,
        location_hours=hours,
    )
    _cache_put(key, payload)
    return payload


# ── Patient Journey (PHI — admin/super_admin only, audited) ──────────────────

def _phi_clinic(db: Session, clinic_id: str, caller: dict) -> Clinic:
    """Resolve a clinic and enforce admin/super_admin access for PHI reads.

    Patient-level data is gated more tightly than the aggregate intelligence
    pages: ``require_write_access`` admits only super_admins and instance admins
    (viewers are rejected)."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_write_access(clinic.instance_id, caller)
    return clinic


class _PatientSearchBody(BaseModel):
    q: str = Field(min_length=2, max_length=128)


@router.post("/intelligence/{clinic_id}/patients/search")
def search_patients(
    clinic_id: str,
    body: _PatientSearchBody,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Search a clinic's patients by name / phone / email (masked results).
    Admin/super_admin only; every search is written to the PHI access log.

    POST (not GET) so the search term — itself a HIPAA identifier (name / phone /
    email) — rides in the request body and never lands in URL/access logs. The
    term is NOT echoed back in the response for the same reason."""
    _phi_clinic(db, clinic_id, caller)

    from intelligence_report.queries import patient_search

    results = patient_search(clinic_id, body.q)
    log_phi_access(
        clinic_id=clinic_id,
        action="patient_search",
        actor=caller.get("email") or caller.get("uid") or "unknown",
        outcome="ok",
        detail=f"results={len(results)}",
    )
    return {"clinic_id": clinic_id, "results": results}


@router.get("/intelligence/{clinic_id}/patients/{patient_key}/journey")
def patient_journey_view(
    clinic_id: str,
    patient_key: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Full marketing-stream → PMS-status journey for one patient.
    Admin/super_admin only; the view is written to the PHI access log."""
    _phi_clinic(db, clinic_id, caller)

    from intelligence_report.queries import patient_journey

    invoca_ids, _ = _active_campaign_ids(db, clinic_id)
    journey = patient_journey(clinic_id, patient_key, invoca_campaign_ids=invoca_ids)
    log_phi_access(
        clinic_id=clinic_id,
        action="patient_journey",
        actor=caller.get("email") or caller.get("uid") or "unknown",
        patient_id=patient_key,
        outcome="ok" if journey.get("patient") else "unmatched",
    )
    return journey
