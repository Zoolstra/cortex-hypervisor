"""Group Intelligence — the per-clinic Overview payload, rolled up over an
instance's clinics.

The output is DELIBERATELY the same dict shape as
``payloads.build_overview``. That is the whole design: the group route renders
through the same React section components as the per-clinic tab, so "expose all
the same metrics" is structural rather than a list someone has to keep in sync.
``clinic_id``/``clinic_name`` carry the instance id/name, and three extra keys
(``clinic_count``, ``clinic_names``, ``aggregation_notes``) let the UI say what
it is looking at.

PMS COVERAGE. A rollup can be PARTLY measurable: revenue and booked-appointment
totals only cover the locations with a PMS feed, and summing over the rest is
exactly what makes an incomplete total look complete. ``pms_coverage`` /
``pms_caveat`` carry that split (and are folded into ``aggregation_notes``), so
the page discloses it and the LLM copy is held to it.

WHY THIS IS NOT ``sum()``
-------------------------
Three different merge rules, and using the wrong one silently produces a
plausible number:

* **Additive** — counts of things that belong to exactly one clinic (calls,
  submissions, clicks, spend, revenue). Sum them.
* **Derived** — every rate, average and delta. These MUST be recomputed from the
  summed components, never averaged. Averaging ``booked_rate`` across a
  400-call clinic and a 12-call clinic weights them equally and is simply wrong;
  the same trap applies to ``capture_rate``, ``avg_invoice``,
  ``revenue_per_clinic_hour``, ``cost_per_call`` and ``roas``.
* **Distinct people** — ``client_id`` is scoped to a clinic, so neither summing
  nor a naive DISTINCT works. These come from dedicated multi-clinic SQL
  (``queries.paid_call_revenue_group`` / ``webform_appointments_group``) which
  keys on the caller's phone/email instead. See those docstrings.

Cost: this fans the per-clinic readers across every clinic in the instance, so a
13-clinic group issues roughly 13x the queries of one clinic page. It is cached
by the caller on the same data-version key as every other payload and is a
prewarm target; do not call it uncached in a request path that a human is
waiting on without one.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from intelligence_report import clinic_hours
from intelligence_report import queries as q
from intelligence_report.queries import Window

log = logging.getLogger(__name__)


# ── merge primitives ─────────────────────────────────────────────────────────

def _sum_ints(rows: list[dict], key: str) -> int:
    return int(sum(int(r.get(key) or 0) for r in rows))


def _sum_floats(rows: list[dict], key: str) -> float:
    return round(float(sum(float(r.get(key) or 0.0) for r in rows)), 2)


def _ratio(num: float, den: float) -> float | None:
    """Rate from summed components. None (not 0.0) on an empty denominator, so
    'no data' stays distinguishable from 'genuinely zero'."""
    return (num / den) if den else None


def _merge_by_key(
    per_clinic: list[list[dict]],
    key_field: str,
    int_fields: tuple[str, ...] = (),
    float_fields: tuple[str, ...] = (),
    carry: tuple[str, ...] = (),
) -> list[dict]:
    """Merge N per-clinic row lists into one, grouping on ``key_field``.

    ``carry`` fields are taken from the first row seen for a key (labels like
    campaign_name that are identical across clinics by construction).
    """
    out: dict[Any, dict] = {}
    for rows in per_clinic:
        for r in rows or []:
            k = r.get(key_field)
            slot = out.get(k)
            if slot is None:
                slot = {key_field: k}
                for c in carry:
                    slot[c] = r.get(c)
                for f in int_fields:
                    slot[f] = 0
                for f in float_fields:
                    slot[f] = 0.0
                out[k] = slot
            for f in int_fields:
                slot[f] += int(r.get(f) or 0)
            for f in float_fields:
                slot[f] += float(r.get(f) or 0.0)
    for slot in out.values():
        for f in float_fields:
            slot[f] = round(slot[f], 2)
    return list(out.values())


def _merge_monthly(
    per_clinic: list[list[dict]],
    int_fields: tuple[str, ...] = (),
    float_fields: tuple[str, ...] = (),
) -> list[dict]:
    """Merge per-month series across clinics, keyed on ``month``.

    Clinics do NOT all cover the same months — a location onboarded in March has
    no January row. Grouping on the month key (rather than zipping by index)
    keeps those aligned; a clinic missing a month simply contributes nothing to
    it instead of shifting the whole series.
    """
    merged = _merge_by_key(per_clinic, "month", int_fields, float_fields)
    return sorted(merged, key=lambda r: r.get("month") or "")


def _merge_trend(per_clinic: list[list[dict]]) -> list[dict]:
    """Headline trend: sum the counts, then RE-DERIVE ``capture_rate`` per month.

    Averaging the per-clinic capture rates would weight a 12-call location the
    same as a 400-call one. Recomputed from the merged booked/connected instead.
    """
    rows = _merge_monthly(
        per_clinic,
        int_fields=("calls", "forms", "contacts", "connected", "booked"),
        float_fields=("revenue",))
    for r in rows:
        r["capture_rate"] = _ratio(r["booked"], r["connected"])
    return rows


# ── section mergers ──────────────────────────────────────────────────────────

_FUNNEL_COUNTS = (
    "total", "no_transcript", "spam", "wrong_number", "genuine", "missed",
    "voicemail", "hangup", "connected", "connected_new", "connected_existing",
    "existing_patient", "existing_active", "existing_lapsed", "existing_lapsing",
    "existing_deep_dormant", "existing_dormant_never", "booked", "booked_new",
    "booked_existing", "booked_existing_active", "booked_existing_lapsing",
    "booked_existing_deep_dormant", "booked_existing_never",
    "qualified_not_booked", "other",
)
_MATTHEW_SPLIT_COUNTS = (
    "connected", "booked", "qualified_not_booked", "existing_customer", "other",
)
# Every counter on queries.matthew_outcomes. All additive — a Matthew call
# belongs to exactly one clinic. `not_booked` is answered-minus-booked and stays
# consistent under summation, so it is summed rather than re-derived.
_MATTHEW_OUTCOME_COUNTS = (
    "answered", "booked", "not_booked", "booked_on_call", "booked_in_window",
    "connected", "connected_new", "connected_existing", "booked_new_patient",
    "booked_existing_patient", "hung_up_immediately", "engaged_no_conversion",
    "confirmed", "confirmed_not_landed",
)
# queries.ad_click_attribution scalars; `campaigns` is a nested list merged
# separately. has_gclid/no_gclid are sums of their own parts, so summing them
# keeps the documented identities intact.
_AD_CLICK_COUNTS = (
    "paid_calls", "matched", "gclid_unmatched", "no_gclid_call_extension",
    "no_gclid_web", "has_gclid", "no_gclid",
)


def _merge_call_funnel(funnels: list[dict], pms_types: list[str]) -> dict | None:
    """Sum every bucket; recompute ``booked_rate`` from the sums.

    ``pms_type`` becomes "mixed" when the group's clinics run different systems —
    the per-clinic UI names the system a booking was reconciled against, and
    naming just one of several would be a lie.
    """
    funnels = [f for f in funnels if f]
    if not funnels:
        return None
    out: dict[str, Any] = {f: _sum_ints(funnels, f) for f in _FUNNEL_COUNTS}
    out["booked_rate"] = _ratio(out["booked"], out["connected"])
    # Identical across clinics by construction (module constants), so the first
    # non-null is representative.
    out["booked_method"] = next(
        (f.get("booked_method") for f in funnels if f.get("booked_method")),
        "pms_reconciled")
    out["match_days"] = next(
        (f.get("match_days") for f in funnels if f.get("match_days") is not None),
        q.CALL_BOOKING_MATCH_DAYS)
    distinct_pms = sorted({p for p in pms_types if p and p != "none"})
    out["pms_type"] = (distinct_pms[0] if len(distinct_pms) == 1
                       else ("mixed" if distinct_pms else "none"))

    splits = [f["matthew_split"] for f in funnels if f.get("matthew_split")]
    if splits:
        out["matthew_split"] = {k: _sum_ints(splits, k) for k in _MATTHEW_SPLIT_COUNTS}
    return out


def _merge_ad_campaigns(per_clinic: list[list[dict]]) -> list[dict]:
    """Merge the per-campaign cascade on ``campaign_id``.

    Two clinics CAN share a Google Ads campaign (one account, several
    locations), so this groups rather than concatenates. ``cost_per_call`` and
    ``roas`` are recomputed from the merged spend/calls/revenue — carrying either
    through from a per-clinic row would describe a slice, not the merged row.
    The ``unattributed`` remainder rows merge into one, keeping the invariant the
    UI footnote promises: Calls and Booked sum to the headline.
    """
    merged = _merge_by_key(
        per_clinic, "campaign_id",
        int_fields=("clicks", "calls", "booked", "invoice_count"),
        float_fields=("spend", "revenue"),
        carry=("campaign_name", "unattributed"),
    )
    # EVERY derived field is recomputed. Carrying any of them through from a
    # per-clinic row would describe that clinic's slice while the counts beside
    # it describe the whole group.
    for r in merged:
        spend, clicks, calls = r["spend"], r["clicks"], r["calls"]
        booked, revenue = r["booked"], r["revenue"]
        r["cpc"] = _ratio(spend, clicks) or 0.0
        r["cost_per_call"] = _ratio(spend, calls) or 0.0
        r["cost_per_booking"] = _ratio(spend, booked) or 0.0
        r["roas"] = _ratio(revenue, spend) or 0.0
        r["revenue_per_booking"] = _ratio(revenue, booked) or 0.0
        r["click_to_call_pct"] = _ratio(calls, clicks) or 0.0
        r["call_to_book_pct"] = _ratio(booked, calls) or 0.0
    return sorted(merged, key=lambda r: (bool(r.get("unattributed")), -r["spend"]))


def _merge_front_desk(rows: list[dict]) -> dict:
    """Capture rate recomputed from summed counts, never averaged."""
    rows = [r for r in rows if r]
    out = {k: _sum_ints(rows, k) for k in ("total", "connected", "returned", "captured")}
    out["capture_rate"] = _ratio(out["captured"], out["total"])
    return out


_LEAK_COMPONENTS = ("missed_calls", "no_shows", "tested_not_sold", "slow_form_followup")


def _merge_leakage(leaks: list[dict], revenue: float, invoice_count: int) -> dict:
    """Group leakage = group lost contacts x the group's BLENDED average invoice.

    Deliberately not the sum of per-clinic estimates. Both are defensible —
    summing values each clinic's losses at its own average and is marginally more
    accurate — but the page displays ``lost_contacts`` and ``avg_invoice`` beside
    the total, and a figure a reader cannot reproduce by multiplying the two
    numbers in front of them reads as a bug. Reconcilability wins.
    """
    leaks = [l for l in leaks if l]
    comps = [l.get("components") or {} for l in leaks]
    components = {k: _sum_ints(comps, k) for k in _LEAK_COMPONENTS}
    lost = sum(components.values())
    avg_invoice = (revenue / invoice_count) if invoice_count else 0.0
    missed = components["missed_calls"]
    return {
        "avg_invoice": avg_invoice,
        "components": components,
        "lost_contacts": lost,
        "estimated_leakage": lost * avg_invoice,
        "intercept_missed": missed,
        "intercept_recovered": None,
    }


def _merge_period(periods: list[dict]) -> dict:
    """One side (current/prior) of the YoY headline, summed then re-derived.

    ``capture_rate`` and ``form_rate`` are rates and are recomputed from the
    summed numerators/denominators — booked/connected and forms respectively.
    """
    periods = [p for p in periods if p]
    out: dict[str, Any] = {
        k: _sum_ints(periods, k)
        for k in ("contacts", "calls", "forms", "connected", "booked")
    }
    out["revenue"] = _sum_floats(periods, "revenue")
    out["capture_rate"] = _ratio(out["booked"], out["connected"])
    # form_rate's denominator isn't in this block (it's forms->booking on the
    # clinic side), so recover it per clinic and re-derive from the totals.
    form_bookings = sum(
        round((p.get("form_rate") or 0.0) * int(p.get("forms") or 0))
        for p in periods)
    out["form_rate"] = _ratio(form_bookings, out["forms"])
    return out


def _merge_headline_yoy(yoys: list[dict]) -> dict | None:
    """Sum both period blocks, then recompute every delta from the group totals.

    Deltas are NEVER carried or averaged: a per-clinic percentage change
    describes that clinic, and the mean of several percentage changes is not the
    group's percentage change. ``basis`` and the window spans are derived from
    the requested window and so are identical across clinics — first non-null
    wins.
    """
    yoys = [y for y in yoys if y]
    if not yoys:
        return None
    cur = _merge_period([y.get("current") for y in yoys])
    prev = _merge_period([y.get("prior") for y in yoys])

    def _pct(c, p):
        return ((c - p) / p) if p else None

    def _pp(c, p):
        return (c - p) if (c is not None and p is not None) else None

    return {
        # If clinics disagree on basis (one has a year of history, another
        # doesn't) the comparison isn't uniform — say so rather than picking one.
        "basis": (lambda bs: bs[0] if len(bs) == 1 else "mixed")(
            sorted({y.get("basis") for y in yoys if y.get("basis")}) or ["none"]),
        "current": cur,
        "prior": prev,
        "deltas": {
            "contacts": _pct(cur["contacts"], prev["contacts"]),
            "revenue": _pct(cur["revenue"], prev["revenue"]),
            "capture_rate": _pp(cur["capture_rate"], prev["capture_rate"]),
        },
        "window": next((y.get("window") for y in yoys if y.get("window")), None),
        "prior_window": next(
            (y.get("prior_window") for y in yoys if y.get("prior_window")), None),
    }


# ── month rollup ─────────────────────────────────────────────────────────────

def _group_month_metrics(clinic_specs: list[dict], mw: Window | None) -> dict | None:
    """One month's system-performance + operational-health KPIs, group-wide.

    Mirrors ``payloads._month_metrics`` but sums the inputs first. Note
    ``revenue_per_clinic_hour`` is SIGMA-revenue / SIGMA-open-hours: the group's
    revenue per hour of clinic capacity, not the mean of each clinic's ratio.
    Every clinic's own opening hours feed the denominator, so a location that is
    closed on Mondays lowers the group's available hours rather than being
    averaged away.
    """
    if mw is None:
        return None
    end_incl = (mw.end_excl - _dt.timedelta(days=1)).isoformat()
    revs, fds, leaks = [], [], []
    open_hours = 0.0
    for spec in clinic_specs:
        cid, invoca, hours = spec["clinic_id"], spec["invoca_ids"], spec["hours"]
        revs.append(_safe(lambda: q.invoice_revenue(cid, window=mw),
                          {"revenue": 0.0, "invoice_count": 0}))
        fds.append(_safe(lambda: q.front_desk_capture(cid, invoca, window=mw), None))
        leaks.append(_safe(lambda: q.revenue_leakage(cid, invoca, window=mw), None))
        if hours:
            open_hours += _safe(
                lambda: clinic_hours.open_hours_in_window(hours, mw.start_date, end_incl),
                0.0) or 0.0
    revenue = _sum_floats(revs, "revenue")
    invoice_count = _sum_ints(revs, "invoice_count")
    front_desk = _merge_front_desk(fds)
    return {
        "month": mw.start_date[:7],
        "revenue": revenue,
        "open_hours": open_hours,
        "revenue_per_clinic_hour": _ratio(revenue, open_hours),
        "answer_rate": front_desk.get("capture_rate"),
        "front_desk": front_desk,
        "leakage": _merge_leakage(leaks, revenue, invoice_count),
    }


def _safe(thunk, default):
    try:
        return thunk()
    except Exception as exc:                           # pragma: no cover - defensive
        log.warning("group aggregate read failed: %s", exc)
        return default


# ── the builder ──────────────────────────────────────────────────────────────

def build_group_aggregate(
    *,
    instance_id: str,
    instance_name: str,
    clinic_specs: list[dict],
    window: Window,
    parallel,
) -> dict[str, Any]:
    """Roll the per-clinic Overview up over ``clinic_specs``.

    ``clinic_specs`` is one dict per clinic:
    ``{clinic_id, clinic_name, invoca_ids, ga_ids, hours, pms_type}``.
    ``parallel`` is injected (``payloads._parallel``) rather than imported, to
    keep this module free of a circular import back into payloads.

    Returns the SAME keys as ``payloads.build_overview`` minus the LLM copy,
    which the caller fills — see the module docstring.
    """
    clinic_ids = [c["clinic_id"] for c in clinic_specs]
    all_invoca = sorted({i for c in clinic_specs for i in (c.get("invoca_ids") or [])})
    win_end_incl = (window.end_excl - _dt.timedelta(days=1)).isoformat()

    # Fan the additive per-clinic readers out. Keys are namespaced by clinic so
    # one flat parallel pass covers every clinic x every reader.
    tasks: dict[str, Any] = {}
    for spec in clinic_specs:
        cid, iv, ga = spec["clinic_id"], spec["invoca_ids"], spec["ga_ids"]
        tasks[f"headline|{cid}"] = lambda cid=cid, iv=iv: q.headline_yoy(cid, iv, window=window)
        tasks[f"trend|{cid}"] = lambda cid=cid, iv=iv: q.monthly_contact_trend(cid, iv, months=13)
        tasks[f"funnel|{cid}"] = lambda cid=cid, iv=iv: q.call_outcomes_funnel(cid, iv, window=window)
        tasks[f"funnel_matthew|{cid}"] = lambda cid=cid, iv=iv: q.call_funnel_matthew_split(cid, iv, window=window)
        tasks[f"forms|{cid}"] = lambda cid=cid: q.form_submission_outcomes(cid, window=window)
        tasks[f"outcomes_monthly|{cid}"] = lambda cid=cid, iv=iv: q.connected_outcomes_by_month(cid, iv, window=window)
        tasks[f"channel_mix|{cid}"] = lambda cid=cid, iv=iv: q.channel_mix(cid, iv, window=window)
        tasks[f"matthew|{cid}"] = lambda cid=cid, iv=iv: q.matthew_outcomes(cid, iv, window=window)
        tasks[f"pipeline_monthly|{cid}"] = lambda cid=cid, iv=iv: q.pipeline_revenue_by_month(cid, iv, window=window)
        tasks[f"ads|{cid}"] = lambda cid=cid, iv=iv, ga=ga: q.google_ads_roi(cid, ga, iv, window=window)
        tasks[f"ad_clicks|{cid}"] = lambda cid=cid, iv=iv, ga=ga: q.ad_click_attribution(cid, iv, ga, window=window)

    # Group-scoped readers: distinct-people metrics that cannot be summed, plus
    # matthew_monthly which already takes a clinic list.
    tasks["g_paid"] = lambda: q.paid_call_revenue_group(clinic_ids, all_invoca, window=window)
    tasks["g_webforms"] = lambda: q.webform_appointments_group(clinic_ids, window=window)
    tasks["g_matthew_monthly"] = lambda: q.matthew_outcomes_by_month(clinic_ids, window=window)

    # Month-over-month KPI trend, one task per month (each fans internally).
    # Imported here, not at module scope, for the same reason ``parallel`` is
    # injected: this module must not import back into payloads on import.
    from intelligence_report.payloads import (
        _month_anchors, _month_window, pms_integrated)
    anchors = _month_anchors(window)
    for a in anchors:
        mw = _month_window(a)
        tasks[f"month|{a.isoformat()}"] = (
            lambda mw=mw: _group_month_metrics(clinic_specs, mw))

    res = parallel(tasks)

    def per_clinic(prefix: str) -> list:
        return [res.get(f"{prefix}|{c}") for c in clinic_ids]

    def per_clinic_lists(prefix: str) -> list[list[dict]]:
        return [r or [] for r in per_clinic(prefix)]

    # ── merge ────────────────────────────────────────────────────────────────
    call_funnel = _merge_call_funnel(
        per_clinic("funnel"), [c.get("pms_type") or "none" for c in clinic_specs])
    if call_funnel is not None:
        splits = [s for s in per_clinic("funnel_matthew") if s]
        if splits:
            call_funnel["matthew_split"] = {
                k: _sum_ints(splits, k) for k in _MATTHEW_SPLIT_COUNTS}

    months = sorted([v for k, v in res.items() if k.startswith("month|") and v],
                    key=lambda x: x["month"])
    cur = months[-1] if months else {}
    prev = months[-2] if len(months) >= 2 else {}
    cur_leak = cur.get("leakage") or {}

    def _series(getter):
        return [{"month": m["month"], "value": getter(m)} for m in months]

    def _pct_delta(c, p):
        return ((c - p) / p) if (c is not None and p) else None

    forms = [f for f in per_clinic("forms") if f]
    form_submissions = {
        k: _sum_ints(forms, k) for k in
        ("submissions", "patients", "attended", "upcoming", "cancelled",
         "no_show", "converted_patients")
    } if forms else None
    if form_submissions is not None:
        form_submissions["revenue"] = _sum_floats(forms, "revenue")

    matthews = [m for m in per_clinic("matthew") if m]
    matthew = None
    if matthews:
        matthew = {k: _sum_ints(matthews, k) for k in _MATTHEW_OUTCOME_COUNTS}
        # Same rule as the per-clinic payload: no answered calls -> hide the
        # section rather than render a wall of zeros.
        if not matthew.get("answered"):
            matthew = None

    ad_clicks = [a for a in per_clinic("ad_clicks") if a]
    ad_click_attribution = None
    if ad_clicks:
        ad_click_attribution = {
            k: _sum_ints(ad_clicks, k) for k in _AD_CLICK_COUNTS}
        # The nested per-campaign rows merge on campaign_id like the main
        # cascade; concatenating would list a shared campaign once per clinic.
        ad_click_attribution["campaigns"] = sorted(
            _merge_by_key([a.get("campaigns") or [] for a in ad_clicks],
                          "campaign_id", int_fields=("calls", "clicks"),
                          carry=("campaign_name",)),
            key=lambda r: -r["calls"])
        if not ad_click_attribution.get("paid_calls"):
            ad_click_attribution = None

    webforms = res.get("g_webforms")
    if webforms and not webforms.get("submissions"):
        webforms = None

    # PMS coverage. A rollup is the one place a partial answer is possible: the
    # revenue and booked-appointment totals below cover only the locations with
    # a PMS feed, and summing across the rest is what makes them look complete.
    # State the split so nobody reads a group total as an all-locations figure.
    pms_missing = [c["clinic_name"] for c in clinic_specs
                   if not pms_integrated(c.get("pms_type"))]
    pms_covered = len(clinic_specs) - len(pms_missing)
    # Same rule the funnel's label follows: name the system only when there is
    # exactly one to name (see _merge_call_funnel).
    distinct_pms = sorted({c.get("pms_type") for c in clinic_specs
                           if pms_integrated(c.get("pms_type"))})
    group_pms_type = (distinct_pms[0] if len(distinct_pms) == 1
                      else ("mixed" if distinct_pms else "none"))
    pms_caveat = None
    if pms_missing:
        names = ", ".join(pms_missing)
        pms_caveat = (
            f"Only {pms_covered} of {len(clinic_specs)} locations have a "
            f"practice-management (PMS) integration. Revenue and booked-"
            f"appointment figures cover those locations only — {names} "
            f"contribute call and form traffic but no revenue or bookings, so "
            f"their absence is not a zero."
            if pms_covered else
            f"No location in this group has a practice-management (PMS) "
            f"integration, so revenue and the link from call/form traffic to "
            f"booked appointments cannot be calculated. Every appointment, "
            f"booking and revenue figure is absent — not zero."
        )

    payload: dict[str, Any] = {
        # The per-clinic contract keys, carrying instance identity — this is what
        # lets the group route render through the same components.
        "clinic_id": instance_id,
        "clinic_name": instance_name,
        "instance_id": instance_id,
        "group_intelligence": True,
        # Group-only additions, so the UI can label what it is showing.
        "is_group": True,
        "clinic_count": len(clinic_specs),
        "clinic_names": {c["clinic_id"]: c["clinic_name"] for c in clinic_specs},
        "aggregation_notes": (_AGGREGATION_NOTES + [pms_caveat] if pms_caveat
                              else _AGGREGATION_NOTES),
        # Same keys the clinic payload carries, so the shared components need
        # one branch, not two. `pms_integrated` is False only when NO location
        # has a feed; a partial group is integrated-but-caveated.
        "pms_type": group_pms_type,
        "pms_integrated": pms_covered > 0,
        "pms_coverage": {
            "clinics_total": len(clinic_specs),
            "clinics_integrated": pms_covered,
            "missing": pms_missing,
        },
        "pms_caveat": pms_caveat,
        "tier": "group",
        "window": {"start": window.start_date, "end": win_end_incl},
        "mom": {"month": cur.get("month"), "prior_month": prev.get("month")},
        "headline": {
            "yoy": _merge_headline_yoy(per_clinic("headline")),
            "trend": _merge_trend(per_clinic_lists("trend")),
            "one_thing": None,
        },
        "system_performance": {
            "revenue_per_clinic_hour": {
                "value": cur.get("revenue_per_clinic_hour"),
                "prior": prev.get("revenue_per_clinic_hour"),
                "delta": _pct_delta(cur.get("revenue_per_clinic_hour"),
                                    prev.get("revenue_per_clinic_hour")),
                "revenue": cur.get("revenue", 0.0),
                "open_hours": cur.get("open_hours", 0.0),
                "series": _series(lambda m: m.get("revenue_per_clinic_hour")),
            },
        },
        "operational_health": {
            "call_answer_rate": {
                "value": cur.get("answer_rate"),
                "prior": prev.get("answer_rate"),
                "delta": ((cur.get("answer_rate") - prev.get("answer_rate"))
                          if (cur.get("answer_rate") is not None
                              and prev.get("answer_rate") is not None) else None),
                "front_desk": cur.get("front_desk"),
                "series": _series(lambda m: m.get("answer_rate")),
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
                "series": _series(lambda m: (m.get("leakage") or {}).get("estimated_leakage")),
            },
        },
        "lifecycle": None,
        "call_funnel": call_funnel,
        "form_submissions": (form_submissions
                             if form_submissions and form_submissions.get("submissions")
                             else None),
        "webforms": webforms,
        "call_outcomes_monthly": _merge_monthly(
            per_clinic_lists("outcomes_monthly"),
            int_fields=("booked", "qualified_not_booked", "existing_customer",
                        "other", "connected")),
        "channel_mix": sorted(
            _merge_by_key(per_clinic_lists("channel_mix"), "channel",
                          int_fields=("count",)),
            key=lambda r: -r["count"]),
        "matthew": matthew,
        "matthew_monthly": res.get("g_matthew_monthly") or [],
        "pipeline_revenue_monthly": _merge_monthly(
            per_clinic_lists("pipeline_monthly"),
            int_fields=("invoices",), float_fields=("revenue",)),
        "ad_campaigns": _merge_ad_campaigns(per_clinic_lists("ads")),
        "paid_attribution": res.get("g_paid"),
        "ad_click_attribution": ad_click_attribution,
        "placeholders": ["cortex_intercept", "review_velocity"],
        "recommendations": [],
    }
    return payload


# Surfaced on the payload so the UI can disclose the two places a group number
# does not mean exactly what the same tile means on a clinic page.
_AGGREGATION_NOTES = [
    "Rates, averages and deltas are recomputed from group totals, not averaged "
    "across locations.",
    "Patient counts under Paid attribution and Web forms are deduplicated by "
    "contact across locations, so someone on file at two clinics counts once. "
    "Portal-booking patient counts are per-location records and are summed.",
]
