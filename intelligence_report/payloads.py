"""
JSON payload builders for the React intelligence pages.

These assemble the plain-dict responses the new React Overview and Patient
Acquisition pages consume — the React migration of the old iframed HTML report
(``report.py`` stays until those iframes are retired). Everything here is data
assembly + one optional LLM call (forward recommendations); the heavy lifting
lives in ``queries.py`` (BigQuery) and ``clinic_hours.py`` (hours parsing).

Builders take already-resolved inputs (campaign id lists, a ``Window``, the
clinic's hours, its tier) rather than a DB session, so they stay decoupled from
the ORM and are unit-testable with a stubbed ``queries`` module.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from intelligence_report import clinic_hours
from intelligence_report import queries as q
from intelligence_report.queries import Window

log = logging.getLogger(__name__)


def _parallel(tasks: dict[str, Any]) -> dict[str, Any]:
    """Run ``{key: thunk}`` concurrently, returning ``{key: result}``. A thunk
    that raises yields ``None`` for that key (a single bad section never blanks
    the page)."""
    out: dict[str, Any] = {}

    def _run(key, thunk):
        try:
            return key, thunk()
        except Exception as exc:                       # pragma: no cover - defensive
            log.warning("payload section %s failed: %s", key, exc)
            return key, None

    if not tasks:
        return out
    with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
        for key, val in pool.map(lambda kv: _run(*kv), tasks.items()):
            out[key] = val
    return out


# ── Forward recommendations (LLM) ────────────────────────────────────────────

_REC_FALLBACK: list[dict] = []


def forward_recommendations(clinic_name: str, metrics: dict[str, Any]) -> list[dict]:
    """Top-3 next moves derived from the computed Overview metrics, via Claude.

    Returns a list of ``{move, why, data, owner}``; an empty list on any failure
    (no key, timeout, bad JSON) so the section degrades gracefully.
    """
    facts = json.dumps(metrics, default=str)[:6000]
    prompt = (
        "You are advising the owner of a hearing clinic from their monthly "
        "intelligence metrics (JSON below). Output the THREE highest-impact next "
        "moves. For each: a short imperative 'move', a one-line 'why' grounded in "
        "the data, the specific 'data' figure behind it, and an 'owner' — one of "
        "'Cortex', 'Client', or 'Shared'. Be concrete and non-obvious; no "
        "preamble. Respond with ONLY a JSON array of exactly 3 objects with keys "
        "move, why, data, owner.\n\n" + facts
    )
    try:
        import anthropic
        from api.core.secrets import get_secret
        key = (get_secret("anthropic-api-key") or "").strip()
        if not key:
            return _REC_FALLBACK
        client = anthropic.Anthropic(api_key=key, timeout=20.0)
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        # Tolerate a fenced code block.
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            return _REC_FALLBACK
        data = json.loads(text[start:end + 1])
        out = []
        for item in data[:3]:
            if isinstance(item, dict):
                out.append({
                    "move": str(item.get("move", "")).strip(),
                    "why": str(item.get("why", "")).strip(),
                    "data": str(item.get("data", "")).strip(),
                    "owner": str(item.get("owner", "Shared")).strip() or "Shared",
                })
        return out
    except Exception as exc:
        log.warning("forward_recommendations failed clinic=%s: %s", clinic_name, exc)
        return _REC_FALLBACK


# ── Overview ─────────────────────────────────────────────────────────────────

def _month_window(anchor: "datetime.date") -> Window:
    """The calendar-month Window containing ``anchor`` (clamped to the cutoffs)."""
    import datetime as _dt
    start = anchor.replace(day=1)
    nxt = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    end_incl = nxt - _dt.timedelta(days=1)
    return Window(start.isoformat(), end_incl.isoformat()).floored()


def _month_anchors(w: Window) -> list:
    """First-of-month dates for every month overlapping ``w`` (clamped to the
    data cutoffs) — the months a monthly KPI trend should cover."""
    import datetime as _dt
    from intelligence_report.queries import MIN_WINDOW_DATE, max_window_date
    start = max(w.start, MIN_WINDOW_DATE).replace(day=1)
    last = min(w.end_excl - _dt.timedelta(days=1), max_window_date()).replace(day=1)
    out, y, m = [], start.year, start.month
    while (y, m) <= (last.year, last.month):
        out.append(_dt.date(y, m, 1))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _month_metrics(clinic_id, invoca_campaign_ids, ga_campaign_ids, mw, location_hours):
    """System-performance + operational-health KPIs for a single month window."""
    if mw is None:
        return None
    import datetime as _dt
    end_incl = (mw.end_excl - _dt.timedelta(days=1)).isoformat()
    rev = q.invoice_revenue(clinic_id, window=mw)
    revenue = rev.get("revenue", 0.0)
    open_hours = (clinic_hours.open_hours_in_window(location_hours, mw.start_date, end_incl)
                  if location_hours else 0.0)
    fd = q.front_desk_capture(clinic_id, invoca_campaign_ids, window=mw)
    leak = q.revenue_leakage(clinic_id, invoca_campaign_ids, window=mw)
    return {
        "month": mw.start_date[:7],
        "revenue": revenue, "open_hours": open_hours,
        "revenue_per_clinic_hour": (revenue / open_hours) if open_hours else None,
        "answer_rate": fd.get("capture_rate"), "front_desk": fd,
        "leakage": leak,
    }


def _pct_delta(cur, prev):
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / prev


def _pp_delta(cur, prev):
    return (cur - prev) if (cur is not None and prev is not None) else None


def build_overview(
    *,
    clinic_id: str,
    clinic_name: str,
    invoca_campaign_ids: list[str],
    ga_campaign_ids: list[str],
    window: Window,
    location_hours: dict | None = None,
    tier: str = "none",
    with_recommendations: bool = True,
) -> dict[str, Any]:
    """Assemble the 5-section Intelligence Overview payload.

    Headline + trend reflect the selected window; System Performance and
    Operational Health are month-over-month — the last complete month within the
    window vs the month before it."""
    import datetime as _dt
    w = window
    win_start = w.start_date
    win_end_incl = (w.end_excl - _one_day()).isoformat()

    # System-performance + operational-health KPIs are computed for EVERY month
    # in the window (the trend); the month-over-month stats are the last two.
    anchors = _month_anchors(w)
    tasks: dict[str, Any] = {
        "headline": lambda: q.headline_yoy(clinic_id, invoca_campaign_ids, window=w),
        "trend": lambda: q.monthly_contact_trend(clinic_id, invoca_campaign_ids, months=13),
        "lifecycle": lambda: q.lifecycle_summary(clinic_id, window=w),
    }
    for a in anchors:
        mw = _month_window(a)
        tasks[f"m:{a.isoformat()}"] = (
            lambda mw=mw: _month_metrics(clinic_id, invoca_campaign_ids, ga_campaign_ids, mw, location_hours))
    sections = _parallel(tasks)

    months = sorted([v for k, v in sections.items() if k.startswith("m:") and v],
                    key=lambda x: x["month"])
    cur = months[-1] if months else {}
    prev = months[-2] if len(months) >= 2 else {}
    cur_leak = (cur.get("leakage") or {})

    def _series(getter):
        return [{"month": m["month"], "value": getter(m)} for m in months]

    rph_series = _series(lambda m: m.get("revenue_per_clinic_hour"))
    answer_series = _series(lambda m: m.get("answer_rate"))
    leak_series = _series(lambda m: (m.get("leakage") or {}).get("estimated_leakage"))

    payload: dict[str, Any] = {
        "clinic_id": clinic_id,
        "clinic_name": clinic_name,
        "tier": tier,
        "window": {"start": win_start, "end": win_end_incl},
        "mom": {"month": cur.get("month"), "prior_month": prev.get("month")},
        "headline": {
            "yoy": sections.get("headline"),
            "trend": sections.get("trend") or [],
            "one_thing": None,   # filled below from the YoY block
        },
        "system_performance": {
            "revenue_per_clinic_hour": {
                "value": cur.get("revenue_per_clinic_hour"),
                "prior": prev.get("revenue_per_clinic_hour"),
                "delta": _pct_delta(cur.get("revenue_per_clinic_hour"), prev.get("revenue_per_clinic_hour")),
                "revenue": cur.get("revenue", 0.0),
                "open_hours": cur.get("open_hours", 0.0),
                "series": rph_series,
            },
        },
        "operational_health": {
            "call_answer_rate": {
                "value": cur.get("answer_rate"),
                "prior": prev.get("answer_rate"),
                "delta": _pp_delta(cur.get("answer_rate"), prev.get("answer_rate")),
                "front_desk": cur.get("front_desk"),
                "series": answer_series,
            },
            "revenue_leakage": {
                "value": cur_leak.get("estimated_leakage"),
                "prior": (prev.get("leakage") or {}).get("estimated_leakage"),
                "delta": _pct_delta(cur_leak.get("estimated_leakage"),
                                    (prev.get("leakage") or {}).get("estimated_leakage")),
                "avg_invoice": cur_leak.get("avg_invoice"),
                "components": cur_leak.get("components"),
                "lost_contacts": cur_leak.get("lost_contacts"),
                "intercept_missed": cur_leak.get("intercept_missed"),
                "intercept_recovered": cur_leak.get("intercept_recovered"),
                "series": leak_series,
            },
        },
        "lifecycle": sections.get("lifecycle"),
        "placeholders": ["cortex_intercept", "review_velocity"],
    }

    # The single "one thing that matters" headline sentence + recommendations
    # both read from the assembled metrics, so run them last.
    if with_recommendations:
        payload["headline"]["one_thing"] = _one_thing_sentence(clinic_name, payload)
        payload["recommendations"] = forward_recommendations(clinic_name, payload)
    else:
        payload["recommendations"] = []
    return payload


def _one_thing_sentence(clinic_name: str, payload: dict) -> str | None:
    """One-sentence 'the thing that matters this month', LLM-written from the
    YoY headline block. Returns None on failure (the UI hides the line)."""
    yoy = (payload.get("headline") or {}).get("yoy") or {}
    if not yoy:
        return None
    facts = json.dumps({
        "yoy": yoy,
        "leakage": (payload.get("operational_health") or {}).get("revenue_leakage"),
        "front_desk": (payload.get("operational_health") or {}).get("front_desk_capture"),
    }, default=str)[:3000]
    prompt = (
        "You are writing the single headline read for a hearing-clinic owner's "
        "intelligence dashboard. From the year-over-year figures below, write ONE "
        "sentence (max 30 words) naming the most important thing right now: lead "
        "with the direction/trend, plain language, a specific number, no preamble, "
        "no hedging. Return only the sentence.\n\n" + facts
    )
    try:
        import anthropic
        from api.core.secrets import get_secret
        key = (get_secret("anthropic-api-key") or "").strip()
        if not key:
            return None
        client = anthropic.Anthropic(api_key=key, timeout=12.0)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=90,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        return text or None
    except Exception as exc:
        log.warning("one_thing sentence failed clinic=%s: %s", clinic_name, exc)
        return None


# ── Patient Acquisition ──────────────────────────────────────────────────────

def build_acquisition(
    *,
    clinic_id: str,
    clinic_name: str,
    invoca_campaign_ids: list[str],
    ga_campaign_ids: list[str],
    window: Window,
    utm_sources: list[str] | None = None,
    utm_mediums: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble the Calling-funnel payload. Top-of-funnel breadth (call traffic,
    channel mix, regions/keywords, monthly trends, Google Ads ROI) is over the
    selected window; the **call funnel** and **web forms** are month-over-month —
    the last complete month in the window vs the month before."""
    w = window
    cur_anchor = w.end_excl - _one_day()
    cur_mw = _month_window(cur_anchor)
    prev_mw = _month_window(cur_anchor.replace(day=1) - _one_day())

    def _funnel(mw):
        return q.revenue_funnel(clinic_id, ga_campaign_ids, invoca_campaign_ids,
                                utm_sources=utm_sources, utm_mediums=utm_mediums, window=mw) if mw else None

    sections = _parallel({
        "call_traffic": lambda: q.acquisition_call_traffic(clinic_id, invoca_campaign_ids, window=w),
        "top_regions": lambda: q.top_calling_regions(clinic_id, invoca_campaign_ids, ga_campaign_ids, window=w),
        "top_keywords": lambda: q.top_keywords(clinic_id, invoca_campaign_ids, ga_campaign_ids, window=w),
        "spam": lambda: q.spam_calls_summary(clinic_id, invoca_campaign_ids, window=w),
        "channel_mix": lambda: q.channel_mix(
            clinic_id, invoca_campaign_ids, utm_sources=utm_sources,
            utm_mediums=utm_mediums, window=w),
        "monthly_trends": lambda: q.monthly_trends(clinic_id, invoca_campaign_ids, ga_campaign_ids, window=w),
        "google_ads_roi": lambda: q.google_ads_roi(clinic_id, ga_campaign_ids, window=w),
        "utm_sources": lambda: q.funnel_utm_sources(clinic_id, invoca_campaign_ids, window=w),
        "utm_mediums": lambda: q.funnel_utm_mediums(clinic_id, invoca_campaign_ids, window=w),
        # Month-over-month: call funnel + web forms (current month vs prior).
        "revenue_funnel": lambda: _funnel(cur_mw),
        "revenue_funnel_prev": lambda: _funnel(prev_mw),
        "webform_funnel": lambda: q.webform_funnel(clinic_id, window=cur_mw) if cur_mw else None,
        "webform_funnel_prev": lambda: q.webform_funnel(clinic_id, window=prev_mw) if prev_mw else None,
        "webform_revenue": lambda: q.webform_revenue(clinic_id, window=cur_mw) if cur_mw else None,
        "webform_revenue_prev": lambda: q.webform_revenue(clinic_id, window=prev_mw) if prev_mw else None,
    })

    cf, cfp = sections.get("revenue_funnel") or {}, sections.get("revenue_funnel_prev") or {}
    wf, wfp = sections.get("webform_funnel") or {}, sections.get("webform_funnel_prev") or {}
    wr, wrp = sections.get("webform_revenue") or {}, sections.get("webform_revenue_prev") or {}
    mom = {"month": cur_mw.start_date[:7] if cur_mw else None,
           "prior_month": prev_mw.start_date[:7] if prev_mw else None}

    return {
        "clinic_id": clinic_id,
        "clinic_name": clinic_name,
        "window": {"start": w.start_date, "end": (w.end_excl - _one_day()).isoformat()},
        "filters": {"utm_sources": utm_sources or [], "utm_mediums": utm_mediums or []},
        "mom": mom,
        "funnel_mom": {
            **mom,
            "deltas": {
                "calls": _pct_delta(cf.get("calls"), cfp.get("calls")),
                "booked": _pct_delta(cf.get("booked"), cfp.get("booked")),
                "invoiced": _pct_delta(cf.get("invoiced"), cfp.get("invoiced")),
                "matched_revenue": _pct_delta(cf.get("matched_revenue"), cfp.get("matched_revenue")),
            },
        },
        "webform_mom": {
            **mom,
            "deltas": {
                "submissions": _pct_delta(wf.get("total_submissions"), wfp.get("total_submissions")),
                "revenue": _pct_delta(wr.get("attributed_revenue"), wrp.get("attributed_revenue")),
            },
        },
        **sections,
    }


def _one_day():
    import datetime as _dt
    return _dt.timedelta(days=1)


# ── Group Intelligence (multi-location comparison) ───────────────────────────

def _leaderboard(locations: list[dict], key: str) -> list[str]:
    """clinic_id order ranked by ``location[key]`` descending. Clinics with no
    PMS data — or a None metric — sort last (kept, never dropped)."""
    def _sort_key(loc):
        val = loc.get(key)
        has = bool(loc.get("has_pms_data")) and val is not None
        return (0 if has else 1, -(val or 0))
    return [loc["clinic_id"] for loc in sorted(locations, key=_sort_key)]


def _rollup(locations: list[dict], list_key: str, group_field: str,
            sum_fields: tuple[str, ...]) -> list[dict]:
    """Merge a per-clinic list-of-dicts (``loc[list_key]``) across all clinics,
    grouping by ``group_field`` and summing ``sum_fields``. Sorted by the first
    sum field descending."""
    merged: dict[str, dict] = {}
    for loc in locations:
        for item in (loc.get(list_key) or []):
            gid = item.get(group_field) or "Unknown"
            row = merged.setdefault(gid, {group_field: gid, **{f: 0 for f in sum_fields}})
            for f in sum_fields:
                row[f] += (item.get(f) or 0)
    return sorted(merged.values(), key=lambda r: r.get(sum_fields[0], 0), reverse=True)


def build_group_overview(
    *,
    instance_id: str,
    instance_name: str,
    clinics: list[tuple[str, str, str]],
    ga_campaign_ids_by_clinic: dict[str, list[str]],
    window: Window,
    with_recommendations: bool = True,
) -> dict[str, Any]:
    """Multi-location "Group Intelligence" payload.

    ``clinics`` is ``[(clinic_id, clinic_name, pms_type), …]`` (the instance's
    active clinics). Fans the per-clinic readers across all of them, rolls up the
    totals, and pre-sorts the leaderboards the UI renders directly. Aggregate-
    only (no PHI). ``zoolstra_attribution`` is the marketing-ROI headline.
    """
    from intelligence_report import group_queries as gq

    w = window
    clinic_ids = [c[0] for c in clinics]
    names = {c[0]: c[1] for c in clinics}

    sections = _parallel({
        "locations": lambda: gq.group_comparison(
            clinics, window=w, ga_by_clinic=ga_campaign_ids_by_clinic),
        "zoolstra": lambda: gq.zoolstra_attribution(clinic_ids, window=w),
        "appt_referrals": lambda: gq.appointment_referral_breakdown(clinic_ids, window=w),
    })
    locations = sections.get("locations") or []
    zoolstra = sections.get("zoolstra") or {"per_location": [], "totals": {}}
    appt_referrals = sections.get("appt_referrals") or []

    totals = {
        "revenue": round(sum(l.get("revenue", 0.0) for l in locations), 2),
        "invoice_count": sum(l.get("invoice_count", 0) for l in locations),
        "booked_appts": sum(l.get("booked_appts", 0) for l in locations),
        "appointments": sum((l.get("appointments") or {}).get("total", 0) for l in locations),
        "webform_submissions": sum(l.get("webform_submissions", 0) for l in locations),
        "webform_attributed_revenue": round(
            sum(l.get("webform_attributed_revenue", 0.0) for l in locations), 2),
    }

    with_pms = [l for l in locations if l.get("has_pms_data")]
    coverage = {
        "clinics_total": len(locations),
        "clinics_with_pms": len(with_pms),
        "clinics_missing": [l["clinic_id"] for l in locations if not l.get("has_pms_data")],
    }

    # zoolstra leaderboard (by attributed revenue), names attached for the UI.
    z_by_clinic = {z["clinic_id"]: z for z in zoolstra.get("per_location", [])}
    zoolstra_leaderboard = sorted(
        zoolstra.get("per_location", []),
        key=lambda z: z.get("revenue", 0), reverse=True)

    payload: dict[str, Any] = {
        "instance_id": instance_id,
        "instance_name": instance_name,
        "window": {"start": w.start_date, "end": (w.end_excl - _one_day()).isoformat()},
        "clinic_names": names,
        "locations": locations,
        "totals": totals,
        "leaderboards": {
            "by_revenue": _leaderboard(locations, "revenue"),
            "by_avg_invoice": _leaderboard(locations, "avg_invoice"),
            "by_booked_appts": _leaderboard(locations, "booked_appts"),
            "by_zoolstra_revenue": [z["clinic_id"] for z in zoolstra_leaderboard],
        },
        "product_mix_rollup": _rollup(
            locations, "product_mix", "item_type", ("revenue", "line_count")),
        "referral_rollup": _rollup(
            locations, "referrals", "source_name", ("revenue", "invoice_count")),
        "appt_referral_rollup": appt_referrals,
        "zoolstra_attribution": zoolstra,
        "coverage": coverage,
    }

    if with_recommendations:
        rec_facts = {
            "instance_name": instance_name,
            "totals": totals,
            "coverage": coverage,
            "top_by_revenue": [
                {"clinic": names.get(cid, cid),
                 "revenue": z_by_clinic.get(cid, {}).get("revenue")}
                for cid in payload["leaderboards"]["by_revenue"][:3]
            ],
            "leaderboard_revenue": [
                {"clinic": names.get(l["clinic_id"], l["clinic_id"]),
                 "revenue": l.get("revenue"), "booked_appts": l.get("booked_appts"),
                 "avg_invoice": l.get("avg_invoice")}
                for l in sorted(locations, key=lambda x: x.get("revenue", 0), reverse=True)
            ],
            "zoolstra": zoolstra.get("totals"),
        }
        payload["recommendations"] = forward_recommendations(instance_name, rec_facts)
    else:
        payload["recommendations"] = []
    return payload
