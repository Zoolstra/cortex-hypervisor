"""
Per-clinic intelligence report endpoint.

``GET /intelligence/{clinic_id}/report.html`` — admin / super_admin only —
returns a fully-rendered HTML document built from Blueprint_PHI + ClinicData.
The report module is stdlib-only (no pandas/plotly) so it can live inside the
hypervisor container without bloating the image.
"""
import logging
import os
import time
import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.audit import log_phi_access
from api.core.db import get_session
from api.core.orm import Clinic, ClinicLocationDetails, GoogleAdsCampaign, Instance, InvocaCampaign
from api.deps import require_read_access, require_write_access, verify_token

log = logging.getLogger(__name__)

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

def _group_clinic_specs(db: Session, instance_id: str) -> list[dict]:
    """Everything the group rollup needs per clinic, in one place.

    The aggregate reads the same per-clinic sources the clinic page does, so it
    needs each clinic's OWN campaign ids and opening hours — not just the ids
    the old leaderboard payload took. Hours in particular are per-location and
    feed the revenue-per-clinic-hour denominator; taking one clinic's hours for
    the group would silently mis-scale that KPI.
    """
    clinics = db.execute(
        select(Clinic).where(Clinic.instance_id == instance_id,
                             Clinic.deleted_at.is_(None))
    ).scalars().all()
    specs = []
    for c in clinics:
        invoca_ids, ga_ids = _active_campaign_ids(db, c.clinic_id)
        specs.append({
            "clinic_id": c.clinic_id,
            "clinic_name": c.clinic_name,
            "invoca_ids": invoca_ids,
            "ga_ids": ga_ids,
            "hours": _location_hours(c),
            "pms_type": getattr(c, "pms_type", None) or "none",
        })
    return specs


# Bump this whenever a reader's METHODOLOGY changes — a different figure for the
# same clinic, window and data.
#
# It exists because `_data_version` (below) is the PMS snapshot date, so it tracks
# when the DATA changed and is blind to when the CODE did. The shared cache tier
# is a GCS object whose existence IS its validity, with no TTL, so without this a
# deployed methodology fix keeps serving the pre-fix number until the next PMS
# sync happens to rotate the snapshot — up to a day of a corrected reader looking
# broken, and no way to tell that from a real regression.
#
# 2026-08-17: pipeline_revenue_by_source gained the web-form channel and
#             cross-channel first touch (methodology-contract §14b).
# 2026-08-18: added `by_medium` (a third partition of the same total). MUST bump:
#             payloads cached by the previous revision have no `by_medium` key at
#             all, and serving one would leave the dashboard's medium view empty
#             with nothing to indicate why.
# 2026-08-19: `unconfirmed_booking` carved out of qualified_not_booked / other
#             (contract §4a). This CHANGES a parity-frozen funnel bucket, so the
#             bump is mandatory — a cached payload would still report the old
#             qualified_not_booked and the Recoverable tile would price a cohort
#             the funnel no longer reports.
_METHODOLOGY_VERSION = "2026-08-20.6"


def _group_data_version(clinic_ids: list[str]) -> str:
    """Cache version for a group payload: every member clinic's version.

    The aggregate is only as fresh as its stalest input, so keying on the
    instance id alone (which has no PMS snapshot of its own, and so degrades to
    daily rotation) would keep serving a stale rollup for up to a day after new
    data lands for one of its clinics.
    """
    return "|".join(_data_version(cid) for cid in sorted(clinic_ids))


# In-process cache for the (expensive, LLM-bearing) JSON payloads.
#
# The TTL used to be 300s, which was the wrong instrument: these payloads only
# change when new data lands (hourly ETL / daily PHI sync), so a 5-minute expiry
# threw away valid results and left the first visitor after any idle gap paying
# the full cold cost — measured at ~39s against ~0.45s warm.
#
# Now keyed on `data_version` instead, exactly as the HTML report cache is
# (report.py:2078-2080): a new ETL load or a date rollover mints a new key, so
# stale entries can never be served and no short TTL is needed. The remaining
# TTL is only a memory bound, and is long because correctness comes from the key
# — the same reasoning as the report cache's 6h L1.
_JSON_TTL = 21_600.0  # 6h; safe because the key is data-versioned
_json_cache: dict[tuple, tuple[float, dict]] = {}

# data_version is per-clinic and needs a BigQuery read, so memoize it rather than
# paying for it on every request. Staleness is bounded by this memo plus the UTC
# date component of the version string — the same trade report.py makes.
_DATA_VERSION_TTL = 600.0
_data_version_cache: dict[str, tuple[float, str]] = {}


def _data_version(clinic_id: str) -> str:
    """The cache-invalidation key: the PMS snapshot date when we have one.

    Deliberately NOT `<snapshot>|<today>` (which report.py uses). Including the
    date makes every clinic's cache expire simultaneously at 00:00 UTC, turning a
    smooth miss rate into a daily cliff where the first visitors all pay the full
    ~40-60s cold cost at once. Keying on the snapshot alone means a cache entry
    lives until new data actually lands, which is the real invalidation signal.

    BUT a clinic with no PMS feed has no snapshot, and a constant version would
    then never rotate — the shared GCS tier has no TTL (existence == validity), so
    it would serve indefinitely. Those clinics therefore keep a dated version, so
    they still invalidate daily. Measured: 6 of 13 clinics have no PMS booking
    data, and they are the cheap ones to rebuild.
    """
    now = time.monotonic()
    hit = _data_version_cache.get(clinic_id)
    if hit and (now - hit[0]) < _DATA_VERSION_TTL:
        return hit[1]
    today = _dt.date.today().isoformat()
    try:
        from intelligence_report import queries as _q
        snapshot = _q.blueprint_snapshot_date(clinic_id)
    except Exception:  # noqa: BLE001 — degrade to daily invalidation
        snapshot = None
    # Snapshot present -> rotate only when new data lands. Absent -> fall back to
    # daily rotation so the untimed shared tier cannot serve forever.
    version = str(snapshot) if snapshot else f"nodata|{today}"
    _data_version_cache[clinic_id] = (now, version)
    return version


def _cache_get(key: tuple):
    hit = _json_cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _JSON_TTL:
        return hit[1]
    return None


def _cache_put(key: tuple, value: dict):
    _json_cache[key] = (time.monotonic(), value)

# ── Shared (cross-instance) cache layer ──────────────────────────────────────
# The in-process dict above only helps the instance that populated it, and
# Cloud Run runs --workers 1 per instance and scales to zero — so a cold start
# or a second instance pays full price, and an external prewarm job could only
# ever warm whichever instance happened to serve it.
#
# This adds a GCS-backed layer with the same design the HTML report cache
# already uses (report.py:1984-2045): the object name embeds a hash of the
# data-versioned key, so an object's existence IS its validity — no TTL. The
# bucket's lifecycle rule sweeps orphans from rotated versions.
#
# Every operation is fail-safe: a cache problem must never fail a request.
_SHARED_CACHE_BUCKET = "project-demo-2-482101-report-cache"  # same bucket, own prefix
_SHARED_CACHE_PREFIX = "json-cache"
_storage_client = None

# Kill switch, checked per call so it can be monkeypatched.
#
# Two purposes. Operationally: disable the shared tier without a redeploy if GCS
# misbehaves, falling back to in-process only. For tests: a unit test must not
# reach GCS — objects written by an earlier run would otherwise leak in as cache
# hits and make results order- and environment-dependent (which is exactly how
# this was found).
_SHARED_CACHE_ENABLED = os.environ.get("PAYLOAD_SHARED_CACHE", "1") != "0"


def _shared_blob(key: tuple, clinic_id: str):
    global _storage_client
    import hashlib
    from google.cloud import storage
    if _storage_client is None:
        _storage_client = storage.Client()
    digest = hashlib.sha256(repr(key).encode()).hexdigest()
    return (_storage_client.bucket(_SHARED_CACHE_BUCKET)
            .blob(f"{_SHARED_CACHE_PREFIX}/{clinic_id}/{digest}.json"))


def _shared_get(key: tuple, clinic_id: str) -> dict | None:
    if not _SHARED_CACHE_ENABLED:
        return None
    try:
        blob = _shared_blob(key, clinic_id)
        if not blob.exists():
            return None
        import json as _json
        return _json.loads(blob.download_as_bytes())
    except Exception as exc:  # noqa: BLE001 — cache miss, never an error
        log.warning("shared json-cache read failed: %s", exc)
        return None


def _shared_put(key: tuple, clinic_id: str, payload: dict) -> None:
    if not _SHARED_CACHE_ENABLED:
        return
    try:
        import json as _json
        _shared_blob(key, clinic_id).upload_from_string(
            _json.dumps(payload, default=str), content_type="application/json")
    except Exception as exc:  # noqa: BLE001
        log.warning("shared json-cache write failed: %s", exc)


def _cache_lookup(key: tuple, clinic_id: str, *,
                  use_cache: bool = True) -> dict | None:
    """L1 in-process, then L2 shared. A shared hit is promoted into L1 so the
    next request on this instance skips the GCS round trip.

    `use_cache=False` bypasses BOTH tiers. Callers must pass one flag covering
    every bypass reason (`nocache`, `skip_llm`): gating only L1 let the shared
    layer serve a payload the caller had explicitly opted out of.
    """
    if not use_cache:
        return None
    hit = _cache_get(key)
    if hit is not None:
        return hit
    shared = _shared_get(key, clinic_id)
    if shared is not None:
        _cache_put(key, shared)
    return shared


def _cache_store(key: tuple, clinic_id: str, payload: dict, *,
                 use_cache: bool = True) -> None:
    """Write through to both tiers, or neither."""
    if not use_cache:
        return
    _cache_put(key, payload)
    _shared_put(key, clinic_id, payload)



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
    skip_llm: bool = False,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """5-section Intelligence Overview payload (JSON), driven by the global
    date range (``?start=&end=`` inclusive ``YYYY-MM-DD``; falls back to
    ``?days=``).

    ``?skip_llm=1`` skips the two Claude calls (``one_thing`` → null,
    ``recommendations`` → []) and bypasses the JSON cache in BOTH directions,
    so a copy-less payload is never cached for (or served to) real users.
    Added for the dashboard-rework parity harness (see
    resources/dashboard-rework-plan.md §1.3); no behavior change unless
    passed."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    window = _resolve_window(start, end, days)
    invoca_ids, gads_ids = _active_campaign_ids(db, clinic_id)
    tier = getattr(clinic, "tier", "none") or "none"
    hours = _location_hours(clinic)
    pms_type = getattr(clinic, "pms_type", None) or "none"

    # pms_type is in the key for the same reason tier is: it changes the payload
    # (the "no PMS integration" disclosure and the LLM copy written under it),
    # and connecting a clinic's PMS does not rotate its data_version, so without
    # this the report would keep claiming the integration is missing.
    key = ("overview", clinic_id, window.start_date, window.end_date_excl, tier,
           pms_type, _data_version(clinic_id), _METHODOLOGY_VERSION)
    use_cache = not nocache and not skip_llm
    if use_cache:
        cached = _cache_lookup(key, clinic_id, use_cache=use_cache)
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
        pms_type=pms_type,
        with_recommendations=not skip_llm,
    )
    # So the clinic page can link up to instance-wide Group Intelligence when
    # this clinic belongs to a multi-location group.
    payload["instance_id"] = clinic.instance_id
    payload["group_intelligence"] = bool(
        getattr(db.get(Instance, clinic.instance_id), "multi_location_group", False))
    if not skip_llm:
        _cache_store(key, clinic_id, payload, use_cache=use_cache)
    return payload


@router.get("/intelligence/{clinic_id}/pipeline-revenue")
def get_pipeline_revenue(
    clinic_id: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 365,
    nocache: bool = False,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Window-exact attributed-revenue total for the dashboard KPI tile, plus
    its breakdown by ``utm_source`` for the chart drawn under it.

    **Why this is its own route rather than a field on ``/overview``.** The
    overview payload is expensive (two Claude calls plus every section reader),
    which is why it is cached on a data-versioned key and warmed hourly by
    `payload-prewarm`. This total is window-exact, whereas the payload's
    existing revenue reader is month-grained and pinned to Dec 2025, so it
    cannot simply be read off that. Keeping it separate means the tile can be
    recomputed — or its methodology changed — without touching, invalidating,
    or waiting on the expensive payload.

    ``by_source`` is a genuine partition: every patient is attributed to their
    FIRST touch only, so the slices sum exactly to ``revenue`` and can honestly
    be drawn as parts of a whole. See
    :func:`~intelligence_report.queries.pipeline_revenue_by_source`.

    ``by_channel_month`` feeds the "Revenue trend" card and is a **wider
    population than ``revenue`` above it** — it adds the web-form channel, which
    neither the tile nor ``by_source`` counts. So
    ``by_channel_month.totals.revenue >= revenue``, and the difference is
    form-acquired patients. This is deliberately additive rather than a
    correction to the headline: ``paid_attribution`` and ``webform_revenue`` are
    parity-frozen (methodology-contract §1.2), and
    ``resources/form-revenue-attribution-plan.md`` §6 requires the new figures to
    ship alongside the old ones and be quantified with ``parity_harness.py``
    BEFORE anything decides to switch. Both numbers are correct for what they
    each claim; the card names the difference rather than hiding it.
    """
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    window = _resolve_window(start, end, days)
    invoca_ids, _ = _active_campaign_ids(db, clinic_id)

    from intelligence_report.queries import average_invoice, pipeline_revenue_by_source

    key = ("pipeline-revenue", clinic_id, window.start_date, window.end_date_excl,
           getattr(clinic, "pms_type", None) or "none", _data_version(clinic_id),
           _METHODOLOGY_VERSION)
    use_cache = not nocache
    cached = _cache_lookup(key, clinic_id, use_cache=use_cache)
    if cached is not None:
        return cached

    # NOT passing customerio_touches yet — see the reader's docstring. Wiring it
    # is a Cloud SQL read here (customerio_enrollments → [(client_id, sent_at)]),
    # not a change to the SQL.
    out = {
        **pipeline_revenue_by_source(clinic_id, invoca_ids, window=window),
        # Deliberately NOT window-scoped — see the reader's docstring. The
        # Recoverable-revenue tile needs a per-patient valuation, and a short
        # window (or an early-in-the-month one) legitimately holds no invoices
        # while the clinic obviously still has an average invoice value.
        "avg_invoice": average_invoice(clinic_id),
        "window": {"start": window.start_date, "end": window.end_date_excl},
    }
    _cache_store(key, clinic_id, out, use_cache=use_cache)
    return out


@router.get("/intelligence/{clinic_id}/biweekly")
def get_intelligence_biweekly(
    clinic_id: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 14,
    nocache: bool = False,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Condensed biweekly report payload (JSON). Same date-range contract as
    the Overview (``?start=&end=`` inclusive, falling back to ``?days=`` —
    default a trailing fortnight); reuses the Overview's readers so every
    metric reconciles with the Overview for the same window."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    window = _resolve_window(start, end, days)
    invoca_ids, gads_ids = _active_campaign_ids(db, clinic_id)
    pms_type = getattr(clinic, "pms_type", None) or "none"

    # In the key for the same reason as on the Overview: it changes what the
    # report is allowed to claim, and it doesn't rotate the data_version.
    key = ("biweekly", clinic_id, window.start_date, window.end_date_excl,
           pms_type, _data_version(clinic_id), _METHODOLOGY_VERSION)
    use_cache = not nocache
    if use_cache:
        cached = _cache_lookup(key, clinic_id, use_cache=use_cache)
        if cached is not None:
            return cached

    from intelligence_report.payloads import build_biweekly

    payload = build_biweekly(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=invoca_ids,
        ga_campaign_ids=gads_ids,
        window=window,
        pms_type=pms_type,
    )
    _cache_store(key, clinic_id, payload, use_cache=use_cache)
    return payload


@router.get("/intelligence/group/{instance_id}/overview")
def get_group_overview(
    instance_id: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 365,
    nocache: bool = False,
    skip_llm: bool = False,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Multi-location "Group Intelligence" payload (JSON) — the per-clinic
    Overview aggregated across all of an instance's clinics, driven by the
    global date range.

    Same payload SHAPE as ``/intelligence/{clinic_id}/overview``, so the group
    route renders through the same components and every clinic-page metric is
    present here. Merge rules live in ``intelligence_report.group_aggregate``.

    Cost note: this fans the per-clinic readers across every clinic, so it is
    roughly Nx a single clinic page. It is cached on the composite data version
    of its member clinics and is a prewarm target.

    ``?skip_llm=1`` skips the recommendations Claude call and bypasses the
    JSON cache both ways (parity-harness use; no behavior change unless
    passed — see the overview endpoint's docstring).

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
    clinic_specs = _group_clinic_specs(db, instance_id)

    # The member clinics' PMS types are in the key for the same reason the
    # clinic page's is: they decide what the rollup may claim about revenue and
    # bookings, and connecting one location's PMS rotates no data version.
    key = ("group-overview", instance_id, window.start_date, window.end_date_excl,
           "|".join(f"{c['clinic_id']}:{c['pms_type']}"
                    for c in sorted(clinic_specs, key=lambda c: c["clinic_id"])),
           _group_data_version([c["clinic_id"] for c in clinic_specs]),
           _METHODOLOGY_VERSION)
    use_cache = not nocache and not skip_llm
    if use_cache:
        cached = _cache_lookup(key, instance_id, use_cache=use_cache)
        if cached is not None:
            return cached

    from intelligence_report.payloads import build_group_overview

    payload = build_group_overview(
        instance_id=instance_id,
        instance_name=instance.instance_name,
        clinic_specs=clinic_specs,
        window=window,
        with_recommendations=not skip_llm,
    )
    if not skip_llm:
        _cache_store(key, instance_id, payload, use_cache=use_cache)
    return payload


@router.get("/intelligence/group/{instance_id}/pipeline-revenue")
def get_group_pipeline_revenue(
    instance_id: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 365,
    nocache: bool = False,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Instance-wide "Revenue attributed" — the same shape as the per-clinic
    route, so the rollup renders the dashboard through the same components.

    **Fans the per-clinic reader and merges in Python**, rather than a new
    cross-clinic SQL. That is the rule for this whole section (see
    `build_group_overview`): one implementation of the methodology, exercised N
    times, so the rollup cannot drift from the clinic pages it sums. It also
    reuses BigQuery's results cache warmed by those pages.

    Merge rules, following the three kinds in cortex-hypervisor/CLAUDE.md:

    * **Additive** — `revenue`, `invoices`, and every `by_source` / `by_medium` /
      `by_channel` slice. An invoice belongs to exactly one clinic, so summing
      cannot double-count it, and each patient's first touch is resolved inside
      their own clinic.
    * **Derived** — `avg_invoice` is recomputed as total revenue ÷ total invoice
      count. NEVER an average of per-clinic averages, which would weight a
      12-invoice location like a 400-invoice one.
    * **Distinct people** — `patients` is NOT deduplicated. `client_id` is
      clinic-scoped, so the same person at two locations is two ids and there is
      no cross-clinic key here. It is therefore a count of patient RECORDS, and
      the payload says so in `aggregation_notes` rather than quietly overstating
      distinct people.
    """
    instance = db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    require_read_access(instance_id, caller)
    if not getattr(instance, "multi_location_group", False):
        raise HTTPException(status_code=404, detail="Not found")

    window = _resolve_window(start, end, days)
    clinic_specs = _group_clinic_specs(db, instance_id)

    key = ("group-pipeline-revenue", instance_id, window.start_date, window.end_date_excl,
           "|".join(f"{c['clinic_id']}:{c['pms_type']}"
                    for c in sorted(clinic_specs, key=lambda c: c["clinic_id"])),
           _group_data_version([c["clinic_id"] for c in clinic_specs]),
           _METHODOLOGY_VERSION)
    use_cache = not nocache
    cached = _cache_lookup(key, instance_id, use_cache=use_cache)
    if cached is not None:
        return cached

    from intelligence_report.queries import average_invoice, pipeline_revenue_by_source

    revenue, invoices, patients = 0.0, 0, 0
    by_source: dict[str, dict] = {}
    by_medium: dict[str, dict] = {}
    by_channel: dict[str, dict] = {}
    inv_total, inv_count = 0.0, 0

    def _merge(acc: dict, rows: list[dict], key_name: str) -> None:
        for r in rows:
            slot = acc.setdefault(r[key_name], {key_name: r[key_name],
                                                "revenue": 0.0, "invoices": 0, "patients": 0})
            slot["revenue"] += r.get("revenue", 0.0)
            slot["invoices"] += r.get("invoices", 0)
            slot["patients"] += r.get("patients", 0)

    for spec in clinic_specs:
        cid = spec["clinic_id"]
        # Fail-safe per clinic, matching group_queries._safe: one clinic without a
        # PMS feed must not blank the whole rollup.
        try:
            part = pipeline_revenue_by_source(cid, spec["invoca_ids"], window=window)
        except Exception as exc:  # noqa: BLE001
            log.warning("group pipeline-revenue: clinic %s failed: %s", cid, exc)
            continue
        revenue += part["revenue"]
        invoices += part["invoices"]
        patients += part["patients"]
        _merge(by_source, part["by_source"], "source")
        _merge(by_medium, part["by_medium"], "medium")
        _merge(by_channel, part["by_channel"], "channel")
        try:
            ai = average_invoice(cid)
        except Exception:  # noqa: BLE001
            ai = None
        if ai and ai["invoice_count"]:
            # Recompose the mean from its components, per the derived-metric rule.
            inv_total += ai["avg_invoice"] * ai["invoice_count"]
            inv_count += ai["invoice_count"]

    desc = lambda acc, k: sorted(acc.values(), key=lambda d: (-d["revenue"], d[k]))  # noqa: E731
    out = {
        "revenue": revenue, "invoices": invoices, "patients": patients,
        "by_source": desc(by_source, "source"),
        "by_medium": desc(by_medium, "medium"),
        "by_channel": desc(by_channel, "channel"),
        "avg_invoice": {
            "avg_invoice": (inv_total / inv_count) if inv_count else 0.0,
            "invoice_count": inv_count, "first_invoice": None, "last_invoice": None,
        },
        "window": {"start": window.start_date, "end": window.end_date_excl},
        "aggregation_notes": [
            "Revenue and invoices are summed across locations; an invoice belongs "
            "to one clinic, so nothing is double-counted.",
            "Patient counts are RECORDS, not distinct people — client_id is "
            "clinic-scoped, so someone seen at two locations counts twice.",
        ],
    }
    _cache_store(key, instance_id, out, use_cache=use_cache)
    return out


@router.get("/intelligence/group/{instance_id}/calls")
def get_group_line_item_calls(
    instance_id: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 90,
    limit: int = 20000,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Instance-wide line-item calls — every clinic's calls in one table, each
    reconciled against ITS OWN clinic's PMS patients (so a caller isn't matched
    across locations) and tagged with the clinic. Gated by the instance
    ``multi_location_group`` flag (404 when off) and PHI (admin/super_admin),
    audited."""
    instance = db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    require_write_access(instance_id, caller)
    if not getattr(instance, "multi_location_group", False):
        raise HTTPException(status_code=404, detail="Not found")

    window = _resolve_window(start, end, days)
    limit = max(1, min(int(limit), 50000))
    clinics = db.execute(
        select(Clinic.clinic_id, Clinic.clinic_name, ClinicLocationDetails.time_zone)
        .outerjoin(ClinicLocationDetails, ClinicLocationDetails.clinic_id == Clinic.clinic_id)
        .where(Clinic.instance_id == instance_id, Clinic.deleted_at.is_(None))
    ).all()

    from intelligence_report.queries import line_item_calls

    calls: list[dict] = []
    for clinic_id, clinic_name, tz in clinics:
        invoca_ids, _ = _active_campaign_ids(db, clinic_id)
        if not invoca_ids:
            continue
        rows = line_item_calls(clinic_id, invoca_ids, window, limit=limit, clinic_tz=tz)
        for r in rows:
            r["clinic_id"] = clinic_id
            r["clinic_name"] = clinic_name
        calls.extend(rows)
    calls.sort(key=lambda r: r.get("datetime") or "", reverse=True)
    calls = calls[:limit]
    log_phi_access(
        clinic_id=instance_id,
        action="group_line_item_calls",
        actor=caller.get("email") or caller.get("uid") or "unknown",
        outcome="ok",
        detail=f"clinics={len(clinics)} n={len(calls)}",
    )
    # False only when NO location has a feed — with a partial group the columns
    # are populated for some rows, which the table's own coverage note explains.
    from intelligence_report.payloads import pms_integrated
    pms_types = list(db.scalars(
        select(Clinic.pms_type).where(Clinic.instance_id == instance_id,
                                      Clinic.deleted_at.is_(None))))
    return {"calls": calls,
            "pms_integrated": any(pms_integrated(p) for p in pms_types)}


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
    use_cache = not nocache
    if use_cache:
        cached = _cache_lookup(key, clinic_id, use_cache=use_cache)
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
    _cache_store(key, clinic_id, payload, use_cache=use_cache)
    return payload


@router.get("/intelligence/group/{instance_id}/active-leads")
def get_group_active_leads(
    instance_id: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 90,
    nocache: bool = False,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Instance-wide active-leads inbox — every location's open leads in one list.

    Fans :func:`build_active_leads` per clinic and merges, same rule as the rest of
    this section: one implementation of the scoring, exercised N times.

    **Merged on the lead key, which is phone/email — so a person who contacted two
    locations is ONE lead, not two.** That is the cross-clinic identity rule from
    cortex-hypervisor/CLAUDE.md: `client_id` is clinic-scoped and cannot be
    compared across locations, but a phone number can. When the same person shows
    up twice, the higher expected-recoverable-revenue row wins and the touch counts
    add, because the two contacts are one person's history.

    `clinic_name` is carried on every lead so a caller can tell which location to
    ring — without it an instance-wide list is unactionable.
    """
    instance = db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    require_read_access(instance_id, caller)
    if not getattr(instance, "multi_location_group", False):
        raise HTTPException(status_code=404, detail="Not found")

    window = _resolve_window(start, end, days)
    clinics = db.execute(
        select(Clinic).where(Clinic.instance_id == instance_id,
                             Clinic.deleted_at.is_(None))
    ).scalars().all()

    key = ("group-active-leads", instance_id, window.start_date, window.end_date_excl,
           _group_data_version([c.clinic_id for c in clinics]), _METHODOLOGY_VERSION)
    use_cache = not nocache
    cached = _cache_lookup(key, instance_id, use_cache=use_cache)
    if cached is not None:
        return cached

    from intelligence_report.active_leads import build_active_leads

    merged: dict[str, dict] = {}
    counts = {"qualified_call": 0, "missed_call": 0, "form": 0}
    for clinic in clinics:
        invoca_ids, _ = _active_campaign_ids(db, clinic.clinic_id)
        try:
            part = build_active_leads(
                clinic_id=clinic.clinic_id,
                clinic_name=clinic.clinic_name,
                invoca_campaign_ids=invoca_ids,
                window=window,
                location_hours=_location_hours(clinic),
            )
        except Exception as exc:  # noqa: BLE001 — one location must not blank the list
            log.warning("group active-leads: clinic %s failed: %s", clinic.clinic_id, exc)
            continue
        for lead in part.get("leads", []):
            lead = {**lead, "clinic_name": clinic.clinic_name,
                    "clinic_id": clinic.clinic_id}
            prev = merged.get(lead["key"])
            if prev is None:
                merged[lead["key"]] = lead
                continue
            # Same person at two locations. Keep the more valuable row, but add the
            # touches — the contacts happened, whichever site took them.
            keep, drop = (
                (lead, prev)
                if (lead.get("expected_recoverable_revenue") or 0)
                   > (prev.get("expected_recoverable_revenue") or 0)
                else (prev, lead)
            )
            keep = {**keep, "touches": (keep.get("touches") or 0) + (drop.get("touches") or 0)}
            merged[lead["key"]] = keep

    leads = sorted(merged.values(),
                   key=lambda l: -(l.get("expected_recoverable_revenue") or 0))
    for l in leads:
        counts[l["subtype"]] = counts.get(l["subtype"], 0) + 1

    out = {
        "instance_id": instance_id,
        "instance_name": instance.instance_name,
        "is_group": True,
        "lead_count": len(leads),
        "source_counts": counts,
        "leads": leads,
        "window": {"start": window.start_date, "end": window.end_date_excl},
        "aggregation_notes": [
            "Leads are deduplicated across locations on phone/email, so someone "
            "who contacted two sites appears once with their touches combined.",
        ],
    }
    _cache_store(key, instance_id, out, use_cache=use_cache)
    return out


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


@router.get("/intelligence/{clinic_id}/leak-calls")
def get_leak_calls(
    clinic_id: str,
    bucket: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 90,
    limit: int = 500,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Per-call sample rows behind one call-funnel outcome bucket — ``booked``,
    ``existing_customer``, ``never_connected`` / ``qualified_no_conversion``, or
    Matthew's non-converted calls (``matthew_no_conversion``) — for the Overview
    appendix drill-down dropdowns. ``limit`` (1–500) caps the sample size (the
    dropdowns request a handful). PHI (caller phone + reasoning) —
    admin/super_admin only, audited."""
    if bucket not in ("booked", "existing_customer", "never_connected",
                      "qualified_no_conversion", "matthew_no_conversion"):
        raise HTTPException(status_code=422, detail="unknown bucket")
    _phi_clinic(db, clinic_id, caller)
    window = _resolve_window(start, end, days)
    limit = max(1, min(int(limit), 500))

    from intelligence_report.queries import booked_sample_calls, leak_calls, matthew_leak_calls

    if bucket == "matthew_no_conversion":
        invoca_ids, _ = _active_campaign_ids(db, clinic_id)
        calls = matthew_leak_calls(clinic_id, invoca_ids, window, limit=limit)
    elif bucket == "booked":
        # "Booked" rows carry the appointment each call reconciled to.
        invoca_ids, _ = _active_campaign_ids(db, clinic_id)
        calls = booked_sample_calls(clinic_id, invoca_ids, window, limit=limit)
    else:
        invoca_ids, _ = _active_campaign_ids(db, clinic_id)
        calls = leak_calls(clinic_id, invoca_ids, bucket, window, limit=limit)
    log_phi_access(
        clinic_id=clinic_id,
        action=f"leak_calls:{bucket}",
        actor=caller.get("email") or caller.get("uid") or "unknown",
        outcome="ok",
        detail=f"n={len(calls)}",
    )
    return {"bucket": bucket, "calls": calls}


@router.get("/intelligence/{clinic_id}/calls")
def get_line_item_calls(
    clinic_id: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 90,
    limit: int = 5000,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Full per-call line-item table for the clinic over the window — one row per
    call with caller, mutually-exclusive outcome (reconciles with the funnel),
    Matthew handling, and associated PMS appointments + attributed revenue. PHI
    (caller phone/name, patient id, appointments, invoices) — admin/super_admin
    only, audited."""
    clinic = _phi_clinic(db, clinic_id, caller)
    window = _resolve_window(start, end, days)
    limit = max(1, min(int(limit), 20000))
    invoca_ids, _ = _active_campaign_ids(db, clinic_id)

    from intelligence_report.queries import line_item_calls

    _loc = getattr(clinic, "location", None)
    calls = line_item_calls(clinic_id, invoca_ids, window, limit=limit,
                            clinic_tz=getattr(_loc, "time_zone", None))
    log_phi_access(
        clinic_id=clinic_id,
        action="line_item_calls",
        actor=caller.get("email") or caller.get("uid") or "unknown",
        outcome="ok",
        detail=f"n={len(calls)}",
    )
    # Carried so the table can say why every appointment/revenue cell is empty:
    # with no PMS feed there is nothing to reconcile a call against, and the
    # booked/led-to-booking outcomes can never be assigned.
    from intelligence_report.payloads import pms_integrated
    return {"calls": calls,
            "pms_integrated": pms_integrated(getattr(clinic, "pms_type", None))}


class _OutcomeOverrideBody(BaseModel):
    outcome: str | None = None   # None clears the override (revert to AI label)


@router.put("/intelligence/{clinic_id}/calls/{call_id}/outcome")
def set_call_outcome(
    clinic_id: str,
    call_id: str,
    body: _OutcomeOverrideBody,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Manually relabel one call's outcome (or clear a prior relabel with
    ``outcome: null``). The override is stored append-only in
    ``ClinicData.call_outcome_overrides`` and joined into the shared call-tagging
    CTE, so the calls table, funnel, and drill-downs all reflect it. Only
    scoring-derived labels are assignable — ``booked``/``led_to_booking`` remain
    PMS-reconciliation facts. admin/super_admin only, audited."""
    from intelligence_report.queries import (
        RELABEL_OUTCOMES, call_belongs_to_clinic, set_call_outcome_override)

    if body.outcome is not None and body.outcome not in RELABEL_OUTCOMES:
        raise HTTPException(
            status_code=422,
            detail=f"outcome must be one of {sorted(RELABEL_OUTCOMES)} or null")
    _phi_clinic(db, clinic_id, caller)
    invoca_ids, _ = _active_campaign_ids(db, clinic_id)
    if not call_belongs_to_clinic(invoca_ids, call_id):
        raise HTTPException(status_code=404, detail="Call not found for this clinic")

    actor = caller.get("email") or caller.get("uid") or "unknown"
    set_call_outcome_override(clinic_id, call_id, body.outcome, actor)
    log_phi_access(
        clinic_id=clinic_id,
        action="call_outcome_relabel",
        actor=actor,
        patient_id=call_id,
        outcome="ok",
        detail=body.outcome or "cleared",
    )
    # Cached JSON payloads (overview funnels etc.) now embed a stale outcome —
    # drop the whole in-process cache rather than reverse-engineering which
    # clinic/instance keys are affected (relabels are rare; TTL is 5 min).
    _json_cache.clear()
    return {"call_id": call_id, "outcome": body.outcome}


@router.get("/intelligence/{clinic_id}/calls/{call_id}/transcript")
def get_call_transcript_view(
    clinic_id: str,
    call_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """The full transcript of one call ("what was said") for the leads
    drill-down. PHI — admin/super_admin only, audited. The query verifies the
    call belongs to this clinic before reading it from GCS."""
    _phi_clinic(db, clinic_id, caller)

    from intelligence_report.queries import get_call_transcript

    result = get_call_transcript(clinic_id, call_id)
    log_phi_access(
        clinic_id=clinic_id,
        action="call_transcript",
        actor=caller.get("email") or caller.get("uid") or "unknown",
        patient_id=call_id,
        outcome="ok" if result else "not_found",
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Transcript not available")
    return result


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
