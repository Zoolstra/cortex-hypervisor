"""
`/v2` intelligence payloads — mart-backed where validated, v1 elsewhere.

This is deliberately a HYBRID rather than a full port. The sections whose cost
dominates a cold Overview are the `_call_tagging_cte` consumers: that CTE does
the transactions x callscoring x patient_contacts x Appointments fuzzy-join fan
-out on every request. Those are exactly what `call_facts` +
`booking_candidates` precompute, and the mart version is already verified to
reproduce v1's funnel exactly (3 clinics x 5 windows, including nested windows
where a precomputed verdict column would have diverged).

Every other section still calls the v1 reader, so:
  * numbers stay identical by construction for the un-migrated sections, and
  * sections migrate one at a time, each gated by the parity harness.

`_mart_backed` in the response names which sections came from the marts, so a
drift report can attribute a difference to the right layer instead of guessing.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.core.db import get_session
from api.core.grouping import is_multi_location
from api.core.orm import Clinic, Instance
from api.deps import verify_token, require_read_access
from api.intelligence import _resolve_window, _active_campaign_ids, _location_hours
from api.v2 import marts

log = logging.getLogger(__name__)
router = APIRouter()

# Sections served from the marts. Everything else falls through to v1.
MART_SECTIONS = ("call_funnel", "call_outcomes_monthly", "call_funnel.matthew_split")


@router.get("/intelligence/{clinic_id}/overview")
def get_overview_v2(
    clinic_id: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 365,
    skip_llm: bool = False,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Overview payload, shape-identical to v1's.

    The response gains two additive keys the v1 payload does not have —
    `_mart_backed` and `_timing_ms` — so the SPA needs no changes and a client
    can see which layer served what. Nothing existing is renamed or removed.
    """
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    window = _resolve_window(start, end, days)
    invoca_ids, gads_ids = _active_campaign_ids(db, clinic_id)
    tier = getattr(clinic, "tier", "none") or "none"
    pms_type = getattr(clinic, "pms_type", None) or "none"

    timing: dict[str, float] = {}
    # Sections actually served from the marts this request. Declared up front so
    # it exists on every path, including the fail-open-to-v1 one.
    served: list[str] = []

    # ── Mart-backed section ────────────────────────────────────────────────
    t0 = time.perf_counter()
    win_end_incl = window.end_date_excl  # exclusive; convert for the mart call
    import datetime as _dt
    end_incl = (_dt.date.fromisoformat(win_end_incl) - _dt.timedelta(days=1)).isoformat()
    try:
        funnel = marts.call_outcomes_funnel(db, clinic_id, window.start_date, end_incl)
        # Pinned to MIN_WINDOW_DATE internally — takes only the window end.
        monthly = marts.connected_outcomes_by_month(db, clinic_id, end_incl)
        matthew_split = marts.call_funnel_matthew_split(
            db, clinic_id, window.start_date, end_incl)
        freshness = marts.mart_freshness(db, clinic_id)
        mart_ok = True
    except Exception as exc:  # noqa: BLE001
        # Fail OPEN to v1 rather than erroring: a marts problem must never take
        # the dashboard down. Matches the fail-safe convention in contract §18.
        log.warning("v2 marts funnel failed clinic=%s, falling back to v1: %s",
                    clinic_id, exc)
        funnel = monthly = matthew_split = None
        freshness, mart_ok = {"data_version": None, "built_at": None}, False
    timing["marts_funnel"] = round((time.perf_counter() - t0) * 1000, 1)

    # ── v1 for everything else (and for the funnel if the marts failed) ────
    t0 = time.perf_counter()
    from intelligence_report.payloads import build_overview
    payload = build_overview(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=invoca_ids,
        ga_campaign_ids=gads_ids,
        window=window,
        location_hours=_location_hours(clinic),
        tier=tier,
        pms_type=pms_type,
        with_recommendations=not skip_llm,
    )
    timing["v1_sections"] = round((time.perf_counter() - t0) * 1000, 1)

    if mart_ok and funnel is not None:
        served.append("call_funnel")
        # Preserve the two enrichments v1 attaches after assembly (payloads.py):
        # the PMS label, and the per-bucket Matthew split when present.
        prior = payload.get("call_funnel") or {}
        funnel["pms_type"] = pms_type
        # Prefer the mart-backed split; fall back to v1's if the marts had no
        # Matthew signal but v1 did (they can disagree only when matthew_calls
        # is absent from one side).
        if matthew_split is not None:
            funnel["matthew_split"] = matthew_split
            served.append("call_funnel.matthew_split")
        elif prior.get("matthew_split"):
            funnel["matthew_split"] = prior["matthew_split"]
        payload["call_funnel"] = funnel
        if monthly is not None:
            payload["call_outcomes_monthly"] = monthly
            served.append("call_outcomes_monthly")

    payload["instance_id"] = clinic.instance_id
    payload["group_intelligence"] = bool(
        is_multi_location(db, clinic.instance_id))
    # Only the sections actually served from the marts — a section that deferred
    # to v1 must not be reported as mart-backed, or a drift report would blame
    # the wrong layer.
    payload["_mart_backed"] = served
    payload["_mart_freshness"] = freshness
    payload["_timing_ms"] = timing
    return payload
