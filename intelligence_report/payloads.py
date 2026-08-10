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
    pms_type: str = "none",
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
        "call_funnel": lambda: q.call_outcomes_funnel(clinic_id, invoca_campaign_ids, window=w),
        "call_funnel_matthew": lambda: q.call_funnel_matthew_split(clinic_id, invoca_campaign_ids, window=w),
        "form_submissions": lambda: q.form_submission_outcomes(clinic_id, window=w),
        "webforms": lambda: q.webform_appointments(clinic_id, window=w),
        "call_outcomes_monthly": lambda: q.connected_outcomes_by_month(clinic_id, invoca_campaign_ids, window=w),
        "channel_mix": lambda: q.channel_mix(clinic_id, invoca_campaign_ids, window=w),
        "matthew": lambda: q.matthew_outcomes(clinic_id, invoca_campaign_ids, window=w),
        "matthew_monthly": lambda: q.matthew_outcomes_by_month([clinic_id], window=w),
        "pipeline_revenue_monthly": lambda: q.pipeline_revenue_by_month(clinic_id, invoca_campaign_ids, window=w),
        "ad_campaigns": lambda: q.google_ads_roi(clinic_id, ga_campaign_ids, invoca_campaign_ids, window=w),
        "paid_attribution": lambda: q.paid_call_revenue(clinic_id, invoca_campaign_ids, window=w),
        "ad_click_attribution": lambda: q.ad_click_attribution(
            clinic_id, invoca_campaign_ids, ga_campaign_ids, window=w),
    }
    for a in anchors:
        mw = _month_window(a)
        tasks[f"m:{a.isoformat()}"] = (
            lambda mw=mw: _month_metrics(clinic_id, invoca_campaign_ids, ga_campaign_ids, mw, location_hours))
    sections = _parallel(tasks)

    # Tag the call funnel with the clinic's PMS so the UI can name the system the
    # booking was reconciled against (CounselEar / Blueprint / …), and attach the
    # per-bucket Matthew (AI receptionist) split when there is one — the tree view
    # shows how many of each connected outcome Matthew answered vs clinic staff.
    if sections.get("call_funnel") is not None:
        sections["call_funnel"]["pms_type"] = pms_type
        ms = sections.get("call_funnel_matthew")
        if ms:
            sections["call_funnel"]["matthew_split"] = ms

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
        # Lifecycle Performance section removed from the Overview 2026-07-23
        # (worklist sizes live on the Leads page); key kept for payload-shape
        # stability, no longer computed.
        "lifecycle": None,
        "call_funnel": sections.get("call_funnel"),
        # Online form submissions (CounselEar portal) — null when none, so the
        # section only shows for clinics that use it (Virsono).
        "form_submissions": (lambda fs: fs if fs and fs.get("submissions") else None)(sections.get("form_submissions")),
        # Website/Jotform web-form submissions reconciled to PMS appointments —
        # null when the clinic had none in the window, so the section hides for
        # clinics without webforms set up.
        "webforms": (lambda wf: wf if wf and wf.get("submissions") else None)(sections.get("webforms")),
        "call_outcomes_monthly": sections.get("call_outcomes_monthly"),
        "channel_mix": sections.get("channel_mix"),
        # Matthew (AI receptionist) outcomes — only for clinics that have Matthew
        # calls (answered>0); null otherwise so the UI hides the section.
        "matthew": (lambda mo: mo if mo and mo.get("answered") else None)(sections.get("matthew")),
        "matthew_monthly": sections.get("matthew_monthly"),
        "pipeline_revenue_monthly": sections.get("pipeline_revenue_monthly"),
        "ad_campaigns": sections.get("ad_campaigns"),
        # §04 headline attribution: all Paid calls (same classifier as
        # traffic_drivers) → invoices on/after each matched patient's first
        # paid call. The per-campaign rows in ad_campaigns keep the looser
        # name-match convention — they're a split, not the headline.
        "paid_attribution": sections.get("paid_attribution"),
        # Ad clicks → calls: Paid calls split by how far each traces back to a
        # Google Ads campaign via gclid. null when the clinic has no Paid calls,
        # so the section hides for clinics with no paid search.
        "ad_click_attribution": (
            lambda a: a if a and a.get("paid_calls") else None
        )(sections.get("ad_click_attribution")),
        "placeholders": ["cortex_intercept", "review_velocity"],
    }

    # The single "one thing that matters" headline sentence + recommendations
    # both read from the assembled metrics, so run them last. Never let a failure
    # here 500 the whole payload — the page renders fine without them.
    if with_recommendations:
        try:
            payload["headline"]["one_thing"] = _one_thing_sentence(clinic_name, payload)
        except Exception as exc:                       # pragma: no cover - defensive
            log.warning("one_thing failed clinic=%s: %s", clinic_name, exc)
            payload["headline"]["one_thing"] = None
        try:
            payload["recommendations"] = forward_recommendations(clinic_name, payload)
        except Exception as exc:                       # pragma: no cover - defensive
            log.warning("recommendations failed clinic=%s: %s", clinic_name, exc)
            payload["recommendations"] = []
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
    clinic_specs: list[dict[str, Any]],
    window: Window,
    with_recommendations: bool = True,
) -> dict[str, Any]:
    """Multi-location "Group Intelligence" payload — the per-clinic Overview,
    rolled up over the instance's clinics.

    Returns the SAME shape as :func:`build_overview` so the group route renders
    through the same section components; the merge rules (and why summing is
    wrong for rates and for distinct-people counts) live in
    ``intelligence_report.group_aggregate``.

    ``clinic_specs`` is one dict per clinic:
    ``{clinic_id, clinic_name, invoca_ids, ga_ids, hours, pms_type}``.

    Superseded the per-location leaderboard payload (revenue/avg-invoice/booked
    rankings, Zoolstra attribution, product + referral rollups). That answered
    "which location is behind?"; this answers "how is the group doing?".
    """
    from intelligence_report.group_aggregate import build_group_aggregate

    payload = build_group_aggregate(
        instance_id=instance_id,
        instance_name=instance_name,
        clinic_specs=clinic_specs,
        window=window,
        parallel=_parallel,
    )

    # Same contract as build_overview: the LLM copy reads the assembled metrics,
    # so it runs last and can never 500 the payload.
    if with_recommendations:
        try:
            payload["headline"]["one_thing"] = _one_thing_sentence(instance_name, payload)
        except Exception as exc:                       # pragma: no cover - defensive
            log.warning("group one_thing failed instance=%s: %s", instance_name, exc)
            payload["headline"]["one_thing"] = None
        try:
            payload["recommendations"] = forward_recommendations(instance_name, payload)
        except Exception as exc:                       # pragma: no cover - defensive
            log.warning("group recommendations failed instance=%s: %s", instance_name, exc)
            payload["recommendations"] = []
    else:
        payload["recommendations"] = []
    return payload


def build_biweekly(
    *,
    clinic_id: str,
    clinic_name: str,
    invoca_campaign_ids: list[str],
    ga_campaign_ids: list[str] | None = None,
    window: Window,
    pms_type: str = "none",
) -> dict[str, Any]:
    """Condensed biweekly report payload.

    Reuses the SAME readers as the Overview (every metric here reconciles with
    its Overview counterpart for the same window) — only the shape is smaller.
    v1 blocks:

    * ``appointments`` — grand-total PMS appointments OCCURRING in the window
      (``start_time``, all statuses; not attribution-gated).
    * ``calls`` — call-traffic totals + ``booked`` (PMS-reconciled §01 funnel:
      appointment created within ``match_days`` of a genuine connected call).
    * ``webforms`` — submissions reconciled to appointments created on/after
      each submission (null when the clinic has no webforms in the window).
    * ``ad_clicks`` — click volume + keyword distribution for the linked
      Google Ads campaigns (null when no campaigns / no clicks).

    More blocks land as the biweekly requirements firm up.
    """
    w = window
    tasks: dict[str, Any] = {
        "appointments": lambda: q.appointment_outcomes(clinic_id, window=w),
        "call_funnel": lambda: q.call_outcomes_funnel(clinic_id, invoca_campaign_ids, window=w),
        "webforms": lambda: q.webform_appointments(clinic_id, window=w),
        "ad_clicks": lambda: q.ad_clicks_keywords(
            ga_campaign_ids or [], invoca_campaign_ids, window=w),
        # Paid-classifier call count ("ad-driven calls") for the Ad clicks
        # section — same population as §04's Paid calls KPI.
        "paid": lambda: q.paid_call_revenue(clinic_id, invoca_campaign_ids, window=w),
    }
    sections = _parallel(tasks)
    appts = sections.get("appointments") or {}
    cf = sections.get("call_funnel") or {}
    # Tag the funnel with the clinic's PMS (same as build_overview) so the
    # shared CallFunnel component can name the reconciliation system.
    if cf:
        cf["pms_type"] = pms_type
    return {
        "clinic_id": clinic_id,
        "clinic_name": clinic_name,
        "pms_type": pms_type,
        "window": {"start": w.start_date, "end": (w.end_excl - _one_day()).isoformat()},
        "appointments": {
            "total": appts.get("total", 0),
            "by_status": appts.get("by_status", {}),
        },
        "calls": {
            "total": cf.get("total", 0),
            "genuine": cf.get("genuine", 0),
            "connected": cf.get("connected", 0),
            "booked": cf.get("booked", 0),
            "booked_method": cf.get("booked_method", "pms_reconciled"),
            "match_days": cf.get("match_days", q.CALL_BOOKING_MATCH_DAYS),
        },
        # Full §01 funnel (total → spam/wrong/no-transcript filtered → genuine
        # → missed vs connected → scoring outcomes) for the funnel visual.
        "call_funnel": cf or None,
        # Ad-click volume + keyword distribution — null when the clinic has no
        # linked Google Ads campaigns or no clicks in the window. Carries the
        # paid-call count ("ad-driven calls") alongside the click volume.
        "ad_clicks": (lambda ac: (
            {**ac, "paid_calls": (sections.get("paid") or {}).get("paid_calls", 0)}
            if ac and ac.get("clicks") else None
        ))(sections.get("ad_clicks")),
        "webforms": (lambda wf: wf if wf and wf.get("submissions") else None)(sections.get("webforms")),
    }
