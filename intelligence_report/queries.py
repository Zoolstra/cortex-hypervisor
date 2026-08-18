"""
BigQuery readers for the per-clinic intelligence report.

All functions take a ``clinic_id`` (cortex-hypervisor's Cloud SQL UUID, which
also keys every Blueprint_PHI table via the ``_clinic_id`` column) and return
plain Python dicts / lists of dicts so the rendering layer doesn't need
pandas.

The Blueprint snapshot is replaced WRITE_TRUNCATE daily by big-query-ingestion,
so these reads see whatever the most recent ETL run produced. Each clinic's
data is fully isolated by ``_clinic_id``.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from typing import Any, Sequence

from google.cloud import bigquery

log = logging.getLogger(__name__)

# Vendor-neutral PMS view layer (Blueprint_PHI ⋃ CounselEar_PHI), created by
# big-query-ingestion/scripts/create_pms_unified_views.py. Views are named
# identically to the Blueprint_PHI tables, so every query below is unchanged —
# it now transparently covers Blueprint *and* CounselEar clinics (each clinic
# lives in one PMS; all reads filter by _clinic_id). Point back at
# "Blueprint_PHI" to bypass the unified layer.
_BP = "project-demo-2-482101.PMS_Unified"
_CLINIC_DATA = "project-demo-2-482101.ClinicData"
_PATIENT_CONTACTS = f"{_BP}.patient_contacts"

# How many days a PMS appointment may be created from a call and still count as
# "booked from that call" (the booking is entered on/just after the call).
#
# THE definition of "booked" for every call-attribution surface — the funnel,
# per-campaign ROAS, paid attribution, the marts and the client data feed all
# resolve to this. Widening it moves historical numbers: calls that previously
# read as qualified-no-conversion become bookings, so booked counts and
# attributed revenue go UP while the recoverable-leak figures go DOWN. Nothing is
# rewritten in storage — every surface derives it at read time — but cached
# payloads keep the old numbers until their data version rotates.
#
# Widened 3 -> 10 days: bookings were being entered days after the call (callback
# scheduling, staff working through a backlog), and a 3-day cut scored those as
# leaks.
#
# Declared HERE, at the top, because it is a default argument for functions
# throughout this module and defaults evaluate at definition time — a later
# definition would NameError on import.
CALL_BOOKING_MATCH_DAYS = 10

# Manual call-outcome relabels, written live by the hypervisor's relabel
# endpoint (PUT /intelligence/{clinic_id}/calls/{call_id}/outcome). Append-only:
# the latest row per (clinic_id, complete_call_id) wins; a NULL outcome row
# clears the override (revert to the model label). Joined into
# ``_call_tagging_cte`` so relabels flow through EVERY funnel/table consumer.
_OVERRIDES_TABLE_NAME = "call_outcome_overrides"
_OVERRIDES_TABLE = f"{_CLINIC_DATA}.{_OVERRIDES_TABLE_NAME}"

# The outcomes a human may relabel a call TO. These are the scoring-derived
# labels only — 'booked' / 'led_to_booking' are PMS-reconciliation facts and
# 'no_transcript' is a data condition, so none of them is assignable.
RELABEL_OUTCOMES = frozenset({
    "spam", "wrong_number", "no_conversation",
    "qualified_no_conversion", "existing_patient", "other",
})

# Raw CounselEar feed. For Virsono (CounselEar) clinics, web-form submissions are
# not in ClinicData.webforms — they book directly into CounselEar and surface as
# appointments tagged ``appt_referral_type = "Referral - Zoolstra"``. The forms
# counters below add these so the acquisition funnel reflects them. Non-CounselEar
# clinics have no such rows, so the addition is a no-op for them.
_COUNSELEAR = "project-demo-2-482101.CounselEar_PHI"
ZOOLSTRA_REFERRAL_TAG = "Referral - Zoolstra"
# CounselEar appointment statuses that count as a kept / converted visit.
_ZOOLSTRA_BOOKED_STATUSES = ("completed", "arrived")

# Sentinel campaign key used by :func:`google_ads_roi` for the remainder row —
# paid calls that neither a GCLID nor a campaign-name match could tie to one of
# the clinic's linked Google Ads campaigns. Carrying them as an explicit row is
# what makes the per-campaign table sum EXACTLY to the §04 headline
# (:func:`paid_call_revenue`) instead of silently undershooting it.
UNATTRIBUTED_CAMPAIGN_ID = "__unattributed__"
UNATTRIBUTED_CAMPAIGN_NAME = "Unattributed paid calls"


def _client() -> bigquery.Client:
    return bigquery.Client(project="project-demo-2-482101")


def _params(clinic_id: str, **extra) -> list[bigquery.ScalarQueryParameter]:
    out = [bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id)]
    for k, v in extra.items():
        out.append(bigquery.ScalarQueryParameter(k, "STRING", v))
    return out


def _truthy_flag(v: Any) -> bool:
    """Normalize Blueprint's free-text boolean columns (stored as STRING, e.g.
    'True'/'False', '1'/'0', 'Yes'/'No') to a real bool."""
    return str(v).strip().lower() in ("true", "1", "yes", "y", "t")


# ── Window boundary (snapped to the UTC day) ─────────────────────────────────
#
# Every reader filters to "the last ``days`` days". Historically that bound was
# expressed inline as ``TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL N DAY)`` /
# ``DATE_SUB(CURRENT_DATE(), INTERVAL N DAY)``. Those are *non-deterministic*
# functions, and BigQuery disables its free 24h query-results cache for any
# query that references one — so identical report requests re-scanned raw data
# every single time, even seconds apart.
#
# Instead we compute the boundary once in Python, snapped to midnight UTC, and
# embed it as a constant literal. The query text is then byte-identical for a
# given ``days`` for the whole UTC day, so BigQuery serves repeats from cache
# (0 bytes billed) until the next ETL load touches the underlying table — which
# is exactly the freshness we want (analytics ETL is hourly, Blueprint daily).
#
# Snapping to the day boundary is what makes the cache actually hit: a
# microsecond-precise "now" would make every request a unique query. The cost
# is a slightly wider window for TIMESTAMP comparisons ("since N midnights ago"
# rather than a rolling N×24h) — the DATE-granularity bounds are unchanged.


# Hard minimum data-availability cutoff. No reader scans before this date — data
# earlier than this is absent/unreliable, so every window's start is clamped up
# to it (see ``Window.from_days`` / ``Window.floored`` and the API window
# resolver). Bump this only if reliable earlier history becomes available.
MIN_WINDOW_DATE = _dt.date(2025, 12, 4)

# Upper data-availability bound. Rolls forward to *today* (UTC) so the
# in-progress month is always visible as data lands — every window's end is
# clamped DOWN to this. Computed per-call (a function, not a module constant)
# so a long-running server doesn't freeze the ceiling at process-start. The
# current day/month is necessarily partial.
def max_window_date() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


class Window:
    """A closed-open date range ``[start, end)`` snapped to UTC midnights.

    Every reader bounds its scan with this. Two ways to build one:

    * ``Window.from_days(N)`` — the last ``N`` midnights up to now (the legacy
      "last N days" behaviour; ``end`` is tomorrow-midnight so "now" is included).
    * ``Window(start_date, end_date)`` — an explicit inclusive calendar range
      (``end_date`` is the last day the caller wants *included*); internally the
      upper bound is exclusive (``end_date`` + 1 day), so a single day range
      ``2026-05-01 … 2026-05-01`` correctly includes all of May 1st.

    All bounds are rendered as constant string literals (never
    ``CURRENT_TIMESTAMP()``) so BigQuery's free 24h results cache keeps hitting
    for identical ``(start, end)`` requests — see the module note above. Adding
    the exclusive *upper* bound (historically absent) also lets BigQuery prune
    partitions on both sides, not just the lower one.
    """

    __slots__ = ("start", "end_excl")

    def __init__(self, start_date: str, end_date: str):
        self.start = _dt.date.fromisoformat(str(start_date))
        # ``end_date`` is the last day to include; store the exclusive bound.
        self.end_excl = _dt.date.fromisoformat(str(end_date)) + _dt.timedelta(days=1)

    @classmethod
    def from_days(cls, days: int) -> "Window":
        end = max_window_date()
        start = max(end - _dt.timedelta(days=int(days)), MIN_WINDOW_DATE)
        return cls(start.isoformat(), end.isoformat())

    def floored(self, floor: _dt.date = None, ceil: _dt.date = None) -> "Window | None":
        """Clamp the window into the available data range — start up to ``floor``
        (default ``MIN_WINDOW_DATE``) and end down to ``ceil`` (default
        ``max_window_date()`` = today). Returns ``None`` if the window doesn't
        overlap that range at all (nothing to query). Enforces the hard cutoffs
        so no reader scans outside the period with available data."""
        floor = floor or MIN_WINDOW_DATE
        ceil = ceil or max_window_date()
        start = max(self.start, floor)
        end_incl = min(self.end_excl - _dt.timedelta(days=1), ceil)
        if end_incl < start:
            return None
        return Window(start.isoformat(), end_incl.isoformat())

    @property
    def span_days(self) -> int:
        return (self.end_excl - self.start).days

    # ── literals for embedding ────────────────────────────────────────────────
    @property
    def start_date(self) -> str:
        return self.start.isoformat()

    @property
    def end_date_excl(self) -> str:
        return self.end_excl.isoformat()

    @property
    def start_ts(self) -> str:
        return f"{self.start.isoformat()} 00:00:00+00:00"

    @property
    def end_ts(self) -> str:
        return f"{self.end_excl.isoformat()} 00:00:00+00:00"

    @property
    def start_month_ts(self) -> str:
        """Start snapped to the *first of its month*.

        LEGACY: ``ad_groups`` used to be monthly-grained, so spend had to be
        pulled a whole month at a time. It is now ingested daily and windowed
        exactly (see ``google_ads_roi``'s spend CTE), so this month-snap is no
        longer used there. Retained only for any caller that still needs a
        month-aligned lower bound."""
        return f"{self.start.replace(day=1).isoformat()} 00:00:00+00:00"


def _win(window: "Window | None", days: int) -> "Window":
    """Resolve the effective window: an explicit ``window`` wins, else fall back
    to the legacy ``days`` look-back. Lets every reader accept a date range
    while keeping all existing ``days=`` call sites byte-for-byte equivalent."""
    return window if window is not None else Window.from_days(days)


def _ts_between(col: str, w: "Window") -> str:
    """Bounded predicate for a TIMESTAMP column/expression (closed-open)."""
    return f"{col} >= TIMESTAMP('{w.start_ts}') AND {col} < TIMESTAMP('{w.end_ts}')"


def _date_between(col: str, w: "Window") -> str:
    """Bounded predicate for a DATE column/expression (closed-open)."""
    return f"{col} >= DATE '{w.start_date}' AND {col} < DATE '{w.end_date_excl}'"


# ── Legacy thin wrappers (lower-bound only) ──────────────────────────────────
# Retained for any caller still passing a bare ``days``; new code should build a
# Window and use ``_ts_between`` / ``_date_between`` for two-sided pruning.

def _window_start_date(days: int) -> str:
    return Window.from_days(days).start_date


def _window_start_ts(days: int) -> str:
    return Window.from_days(days).start_ts


def _window_start_month_ts(days: int) -> str:
    return Window.from_days(days).start_month_ts


# ── Clinic metadata ──────────────────────────────────────────────────────────

def blueprint_snapshot_date(clinic_id: str):
    """Most recent Blueprint snapshot date for ``clinic_id`` (or ``None``).

    The clinic's human-readable name is *not* sourced here — it comes from
    Cloud SQL (``Users.clinics.clinic_name``, keyed by ``clinic_id``) at the
    API layer and is threaded through to the report builder. Sourcing the
    name from ``Blueprint_PHI`` would silently drop the report for any
    clinic without Blueprint integration; this function only answers the
    "do we have Blueprint data, and how fresh is it" question.
    """
    client = _client()
    rows = list(client.query(
        f"""
            SELECT MAX(_snapshot_date) AS snapshot_date
            FROM `{_BP}.Appointments`
            WHERE _clinic_id = @clinic_id
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
    ).result())
    if not rows:
        return None
    return rows[0].snapshot_date


# ── Appointments ─────────────────────────────────────────────────────────────

def appointment_outcomes(clinic_id: str, days: int = 365, window: "Window | None" = None) -> dict[str, Any]:
    """Roll up appointments by ``status_2`` over the last ``days``.

    Blueprint's numeric ``status`` codes map to ``status_2`` strings:
      7=Completed, 2=Tentative, 0=Confirmed, 3=Cancelled, 5=Arrived,
      9=Ready, 1=No show, 6=In progress, 4=Left message, 8=No answer.

    Returns ``{total, by_status: {label: count}, sales_opportunities, …}``.
    """
    w = _win(window, days)
    client = _client()
    rows = list(client.query(
        f"""
            SELECT
              COALESCE(status_2, 'Unknown') AS label,
              COUNT(*)                       AS n,
              COUNTIF(sales_opportunity = 'True') AS sales_opp,
              MIN(start_time)                AS first_seen,
              MAX(start_time)                AS last_seen
            FROM `{_BP}.Appointments`
            WHERE _clinic_id = @clinic_id
              AND {_ts_between("SAFE_CAST(start_time AS TIMESTAMP)", w)}
            GROUP BY label
            ORDER BY n DESC
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
    ).result())
    by_status = {r.label: int(r.n) for r in rows}
    total = sum(by_status.values())
    sales_opp_total = sum(int(r.sales_opp or 0) for r in rows)
    return {
        "total":               total,
        "by_status":           by_status,
        "sales_opportunities": sales_opp_total,
        "window_days":         w.span_days,
    }


# ── Invoices ─────────────────────────────────────────────────────────────────

def invoice_revenue(clinic_id: str, days: int = 365, window: "Window | None" = None) -> dict[str, Any]:
    """Total invoice revenue + count over the window.

    Blueprint stores ``order_total_with_tax`` as STRING — we cast to NUMERIC.
    Zero-total invoices are excluded from the count (they're typically credit
    notes / placeholders, not actual sales).
    """
    w = _win(window, days)
    client = _client()
    rows = list(client.query(
        f"""
            SELECT
              COUNT(*)                                   AS invoice_count,
              COALESCE(SUM(SAFE_CAST(order_total_with_tax AS NUMERIC)), 0) AS revenue,
              MIN(invoice_date)                          AS first_invoice,
              MAX(invoice_date)                          AS last_invoice
            FROM `{_BP}.InvoiceMaster`
            WHERE _clinic_id = @clinic_id
              AND SAFE_CAST(order_total_with_tax AS NUMERIC) > 0
              AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', invoice_date)", w)}
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
    ).result())
    if not rows:
        return {"invoice_count": 0, "revenue": 0.0, "first_invoice": None, "last_invoice": None}
    r = rows[0]
    return {
        "invoice_count": int(r.invoice_count or 0),
        "revenue":       float(r.revenue or 0),
        "first_invoice": str(r.first_invoice) if r.first_invoice else None,
        "last_invoice":  str(r.last_invoice) if r.last_invoice else None,
        "window_days":   w.span_days,
    }


# ── Referral sources ─────────────────────────────────────────────────────────

def referral_breakdown(clinic_id: str, days: int = 365, top_n: int = 10, window: "Window | None" = None) -> list[dict]:
    """Top referral sources by invoice revenue.

    Joins InvoiceMaster → ReferralSources on (type_id, source_id). Falls back
    to 'Unknown' when the referrer fields are blank. Aggregates over the
    rolling window and returns the top ``top_n`` plus an "Other" bucket.
    """
    w = _win(window, days)
    client = _client()
    rows = list(client.query(
        f"""
            WITH joined AS (
                SELECT
                  COALESCE(NULLIF(rs.source_name, ''), 'Unknown') AS source_name,
                  COALESCE(NULLIF(rs.type_desc, ''),  'Unknown')  AS source_type,
                  SAFE_CAST(im.order_total_with_tax AS NUMERIC)   AS revenue
                FROM `{_BP}.InvoiceMaster` im
                LEFT JOIN `{_BP}.ReferralSources` rs
                  ON rs._clinic_id = im._clinic_id
                 AND rs.type_id    = im.referrer_type_id
                 AND rs.source_id  = im.referral_source_id
                WHERE im._clinic_id = @clinic_id
                  AND SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
                  AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)", w)}
            )
            SELECT
              source_name,
              source_type,
              COUNT(*)        AS invoice_count,
              SUM(revenue)    AS revenue
            FROM joined
            GROUP BY source_name, source_type
            ORDER BY revenue DESC NULLS LAST
            LIMIT {int(top_n)}
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
    ).result())
    return [
        {
            "source_name":   r.source_name,
            "source_type":   r.source_type,
            "invoice_count": int(r.invoice_count or 0),
            "revenue":       float(r.revenue or 0),
        }
        for r in rows
    ]


# ── Patients ─────────────────────────────────────────────────────────────────

def patient_demographics(clinic_id: str) -> dict[str, Any]:
    """Patient counts by status + a rough age distribution.

    Status comes from ClientDemographics.status (free-text per Blueprint, e.g.
    'Active', 'Inactive', 'Deceased', etc.).
    """
    client = _client()
    rows = list(client.query(
        f"""
            SELECT
              COALESCE(NULLIF(status, ''), 'Unknown') AS status,
              COUNT(*) AS n
            FROM `{_BP}.ClientDemographics`
            WHERE _clinic_id = @clinic_id
            GROUP BY status
            ORDER BY n DESC
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
    ).result())
    return {r.status: int(r.n) for r in rows}


# ── Hearing aid sales mix (from InvoiceLineItems) ────────────────────────────

def line_item_mix(clinic_id: str, days: int = 365, window: "Window | None" = None) -> list[dict]:
    """Revenue + line-count by ``item_type`` over the window.

    Surfaces hearing-aid revenue vs accessories vs services etc.
    """
    w = _win(window, days)
    client = _client()
    rows = list(client.query(
        f"""
            SELECT
              COALESCE(NULLIF(item_type, ''), 'Unknown') AS item_type,
              COUNT(*)                                   AS line_count,
              SUM(SAFE_CAST(price AS NUMERIC))           AS revenue
            FROM `{_BP}.InvoiceLineItems`
            WHERE _clinic_id = @clinic_id
              AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', invoice_date)", w)}
            GROUP BY item_type
            ORDER BY revenue DESC NULLS LAST
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
    ).result())
    return [
        {
            "item_type":  r.item_type,
            "line_count": int(r.line_count or 0),
            "revenue":    float(r.revenue or 0),
        }
        for r in rows
    ]


# ── Google Ads ROI (per linked Google Ads campaign) ──────────────────────────

def google_ads_roi(clinic_id: str, ga_campaign_ids: list[str],
                   invoca_campaign_ids: list[str] | None = None, days: int = 90,
                   window: "Window | None" = None, match_days: int = CALL_BOOKING_MATCH_DAYS) -> list[dict]:
    """Per-campaign cascade for the clinic's linked Google Ads campaigns.

    The call population is the clinic's PAID calls — the same classifier
    (:func:`_channel_case_sql`), spam filter, and per-``complete_call_id`` dedup
    as :func:`paid_call_revenue` / :func:`traffic_drivers` — so these rows are a
    per-campaign SPLIT of the §04 headline numbers. Every paid call lands in
    exactly one row, so ``calls`` and ``booked`` sum EXACTLY to the §04 headline
    (:func:`paid_call_revenue`) — see the remainder row below.

    Paid calls are attributed to a campaign by a two-tier **cascade**:

    1. **GCLID** — the call's ``transactions.gclid`` joins
       ``ad_clicks_v2.click_view_gclid`` for one of the clinic's linked
       campaigns. Campaign-exact, hard evidence; wins whenever present.
    2. **Normalized campaign name** — each Google Ads campaign name
       (``ad_clicks_v2.campaign_name``, e.g. "Beta Princeton") is normalized
       (leading "Beta " stripped, trimmed, lower-cased) and compared to the
       Invoca campaign name on the call
       (``transactions.advertiser_campaign_name``, e.g. "Princeton").

    Neither tier alone is sufficient, which is why both are used. GCLID coverage
    is sparse (~25% of paid calls carry one; Call Extension tap-to-call never
    does), so GCLID alone collapses the per-campaign numbers toward zero. But
    name matching only works where the client names its Invoca campaigns after
    its Ads campaigns (Virsono's "Beta <Location>" ↔ "<Location>"); a client that
    names Invoca campaigns per LOCATION while its Ads campaigns are named
    anything else (e.g. Hope Hearing: Invoca "Southlake" vs Ads "Hope Hearing TX
    PPC 8000") name-matches nothing at all. Calls are scoped to the clinic's own
    Invoca campaigns first, so a name shared across clinics can't cross-attribute.

    Paid calls that neither tier resolves are returned as a single **remainder
    row** (``campaign_id = UNATTRIBUTED_CAMPAIGN_ID``, ``unattributed=True``,
    zero clicks/spend) rather than dropped. That is what makes the table
    reconcile with the headline: the tracking data can't say WHICH campaign
    drove those calls, but they were paid calls and the report says so out loud
    instead of quietly losing them.

    Per campaign: clicks (``ad_clicks_v2``), spend (``ad_groups`` —
    ``metrics_cost_micros / 1e6``, Google's true billed cost, windowed to the
    range at daily granularity), name-matched paid calls, ``booked``
    (PMS-reconciled: a distinct ``PMS_Unified.Appointments`` row CREATED within
    ``match_days`` on/after the CLOSEST genuine, connected name-matched call.
    Requires a real conversation — spam, wrong-number, no-conversation and
    no-transcript calls do NOT book, so a coincidental phone match is excluded —
    but patient type is IGNORED: new and existing patients both count), and
    attributed revenue (matched callers phone-joined to their patient →
    every positive ``InvoiceMaster`` invoice in the window, deduped per
    ``order_id`` — the same revenue rule as the §04 headline
    (:func:`paid_call_revenue`) and "Revenue from Call traffic"; no
    on/after-first-call gate).

    NOTE: captures ad → *phone call* → booking only; CounselEar web-portal
    conversions (no call) are not linked here. Returns one dict per campaign
    (plus the remainder row when there is one) with derived ratios (CPC,
    cost-per-call, cost-per-booking, ROAS). With no linked Invoca campaigns the
    call/booked/revenue columns are 0 and there is no remainder row (clicks +
    spend still report). Skips clinics with no linked Google Ads campaigns.
    """
    if not ga_campaign_ids:
        return []
    w = _win(window, days)
    ga_in = "(" + ", ".join(f"'{c}'" for c in ga_campaign_ids) + ")"
    iv_in = "(" + ", ".join(f"'{c}'" for c in (invoca_campaign_ids or [])) + ")"
    calls_scope = f"CAST(t.invoca_campaign_id AS STRING) IN {iv_in}" if invoca_campaign_ids else "FALSE"

    def _norm(col: str) -> str:
        # Strip a leading "Beta " (Google Ads names) case-insensitively, trim,
        # lower-case — so "Beta Princeton" (Ads) matches "Princeton" (Invoca).
        return f"LOWER(TRIM(REGEXP_REPLACE(IFNULL({col}, ''), r'(?i)^Beta\\s+', '')))"

    sql = f"""
        WITH ga AS (
            SELECT
              google_ads_campaign_id,
              ANY_VALUE(campaign_name) AS campaign_name,
              COUNT(*) AS clicks
            FROM `{_CLINIC_DATA}.ad_clicks_v2`
            WHERE google_ads_campaign_id IN {ga_in}
              AND {_ts_between("timestamp", w)}
            GROUP BY google_ads_campaign_id
        ),
        ga_norm AS (
            SELECT google_ads_campaign_id, campaign_name, clicks,
                   {_norm("campaign_name")} AS norm
            FROM ga
        ),
        ga_name_map AS (
            -- name → ONE campaign. Two linked campaigns can normalize to the
            -- same name; picking one deterministically keeps a call from being
            -- credited to both (which would break the sum-to-headline property).
            SELECT norm, MIN(google_ads_campaign_id) AS google_ads_campaign_id
            FROM ga_norm
            WHERE norm != ''
            GROUP BY norm
        ),
        gclid_clicks AS (
            -- Tier 1 of the attribution cascade: GCLID → campaign, restricted to
            -- the clinic's linked campaigns so a click from an unlinked campaign
            -- falls through to the name match instead of inventing a row.
            SELECT click_view_gclid AS gclid,
                   MIN(google_ads_campaign_id) AS google_ads_campaign_id
            FROM `{_CLINIC_DATA}.ad_clicks_v2`
            WHERE google_ads_campaign_id IN {ga_in}
              AND click_view_gclid IS NOT NULL
              AND click_view_gclid NOT IN ('nan', '')
            GROUP BY click_view_gclid
        ),
        spend AS (
            -- Accurate billed spend straight from Google Ads
            -- (metrics_cost_micros, in micros → /1e6). ``ad_groups`` is ingested
            -- at DAILY granularity, so summing the rows whose own date falls in
            -- the window gives exact spend for any range — no month-snap and no
            -- clicks × average_cpc estimate.
            SELECT
              google_ads_campaign_id,
              SUM(SAFE_CAST(metrics_cost_micros AS FLOAT64) / 1e6) AS spend
            FROM `{_CLINIC_DATA}.ad_groups`
            WHERE google_ads_campaign_id IN {ga_in}
              AND {_ts_between("timestamp", w)}
            GROUP BY google_ads_campaign_id
        ),
        calls AS (
            -- Clinic's PAID calls (same classifier + spam filter + per-call
            -- dedup as paid_call_revenue / traffic_drivers, so this table is a
            -- per-campaign split of the §04 headline population), tagged with
            -- both attribution keys: the real gclid and the normalized campaign
            -- name. Aliased ``gclid_real`` (not ``gclid``) so it can't be
            -- confused with the raw column the channel CASE below reads.
            SELECT call_id, call_ts, phone_norm, norm, genuine,
                   IF(gclid IS NOT NULL AND gclid NOT IN ('nan', ''), gclid, NULL) AS gclid_real
            FROM (
                SELECT
                  t.complete_call_id AS call_id,
                  SAFE_CAST(t.timestamp AS TIMESTAMP) AS call_ts,
                  RIGHT(REGEXP_REPLACE(IFNULL(t.calling_phone_number, ''), r'\\D', ''), 10) AS phone_norm,
                  {_norm("t.advertiser_campaign_name")} AS norm,
                  t.gclid, t.wbraid, t.gbraid, t.msclkid, t.fbclid,
                  LOWER(t.utm_medium) AS um, LOWER(t.utm_source) AS us, t.marketing_channel,
                  (cs.complete_call_id IS NOT NULL
                   AND NOT IFNULL(cs.empty_transcript, FALSE)
                   AND NOT IFNULL(cs.wrong_number, FALSE)
                   AND NOT IFNULL(cs.no_conversation, FALSE)) AS genuine,
                  IFNULL(cs.spam_or_solicitor, FALSE) AS is_spam
                FROM `{_CLINIC_DATA}.transactions` t
                LEFT JOIN `{_CLINIC_DATA}.callscoring` cs
                  ON cs.complete_call_id = t.complete_call_id
                WHERE {calls_scope}
                  AND {_ts_between("t.timestamp", w)}
                QUALIFY ROW_NUMBER() OVER (
                  PARTITION BY t.complete_call_id ORDER BY t.timestamp DESC) = 1
            )
            WHERE NOT is_spam AND ({_channel_case_sql()}) = 'Paid'
        ),
        call_camp AS (
            -- Attach each paid call to a Google Ads campaign: GCLID first, then
            -- normalized campaign name, then the explicit remainder bucket. Every
            -- paid call appears exactly once, so per-campaign calls/booked sum to
            -- the §04 headline.
            SELECT c.call_id, c.call_ts, c.phone_norm, c.genuine,
                   COALESCE(gk.google_ads_campaign_id,
                            gn.google_ads_campaign_id,
                            '{UNATTRIBUTED_CAMPAIGN_ID}') AS google_ads_campaign_id
            FROM calls c
            LEFT JOIN gclid_clicks gk ON gk.gclid = c.gclid_real
            LEFT JOIN ga_name_map gn  ON gn.norm  = c.norm AND c.norm != ''
        ),
        patients AS (
            SELECT DISTINCT client_id, phone_norm
            FROM `{_PATIENT_CONTACTS}`
            WHERE _clinic_id = @clinic_id AND LENGTH(phone_norm) = 10
        ),
        booked_calls AS (
            -- One row per PMS appointment, credited to the MOST RECENT genuine,
            -- connected PAID call (same rule as §01 ``call_outcomes_funnel``).
            -- ``call_camp`` spans EVERY paid call — attributed and unattributed
            -- alike — so this ranking sees the same population
            -- ``paid_call_revenue`` ranks over and the per-campaign booked counts
            -- add up to its total. We then count DISTINCT booked CALLS per
            -- campaign (``gads_booked`` below) — the SAME unit §01 reports — so
            -- per-campaign booked reconciles with, and never exceeds, the §01
            -- total (counting distinct appointments here would over-count a call
            -- that produced several appointments). A booking requires a GENUINE,
            -- CONNECTED call (excludes no-transcript / spam / wrong / no-conv, so
            -- a coincidental phone match ≠ a booking). Orthogonal to patient type.
            SELECT google_ads_campaign_id, call_id FROM (
                SELECT cc.google_ads_campaign_id, cc.call_id, a.event_id,
                       ROW_NUMBER() OVER (
                         PARTITION BY a.event_id
                         ORDER BY cc.call_ts DESC
                       ) AS rn
                FROM call_camp cc
                JOIN patients p
                  ON p.phone_norm = cc.phone_norm AND LENGTH(cc.phone_norm) = 10
                JOIN `{_BP}.Appointments` a
                  ON a._clinic_id = @clinic_id AND a.client_id = p.client_id
                WHERE DATE_DIFF(DATE(SAFE_CAST(a.created_time AS TIMESTAMP)), DATE(cc.call_ts), DAY)
                      BETWEEN 0 AND @match_days
                  AND cc.genuine
            )
            WHERE rn = 1
        ),
        gads_calls AS (
            SELECT google_ads_campaign_id, COUNT(DISTINCT call_id) AS calls
            FROM call_camp GROUP BY google_ads_campaign_id
        ),
        gads_booked AS (
            SELECT google_ads_campaign_id, COUNT(DISTINCT call_id) AS booked
            FROM booked_calls GROUP BY google_ads_campaign_id
        ),
        -- Paid patients per campaign — identifies whose invoices count for the
        -- row (the §04 headline matches the same way, minus the campaign
        -- split). Revenue is the one column whose rows can still sum ABOVE the
        -- headline: a patient touched by two campaigns is credited to both,
        -- while the headline dedups them. Both are deliberate — the row answers
        -- "what did this campaign's callers transact", the headline answers
        -- "what did paid callers transact".
        campaign_first_call AS (
            SELECT cc.google_ads_campaign_id, p.client_id, MIN(cc.call_ts) AS first_call_ts
            FROM call_camp cc
            JOIN patients p
              ON p.phone_norm = cc.phone_norm AND LENGTH(cc.phone_norm) = 10
            GROUP BY cc.google_ads_campaign_id, p.client_id
        ),
        campaign_revenue AS (
            -- Same revenue rule as the §04 headline (paid_call_revenue) and
            -- "Revenue from Call traffic": every positive invoice in the
            -- window for a matched patient, deduped per order_id — no
            -- on/after-first-call gate — so the rows are a split of the
            -- headline, not a different convention.
            SELECT
              google_ads_campaign_id,
              SUM(amt)                  AS revenue,
              COUNT(DISTINCT order_id)  AS invoice_count
            FROM (
              SELECT cfc.google_ads_campaign_id, im.order_id,
                     MAX(SAFE_CAST(im.order_total_with_tax AS NUMERIC)) AS amt
              FROM campaign_first_call cfc
              JOIN `{_BP}.InvoiceMaster` im
                ON im._clinic_id = @clinic_id
               AND im.client_id = cfc.client_id
              WHERE SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
                AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)", w)}
              GROUP BY cfc.google_ads_campaign_id, im.order_id
            )
            GROUP BY google_ads_campaign_id
        ),
        base AS (
            -- One row per linked campaign, plus the remainder row when any paid
            -- call went unattributed (suppressed when there are none, so clinics
            -- with clean tracking don't grow an empty row).
            SELECT google_ads_campaign_id, campaign_name, clicks FROM ga_norm
            UNION ALL
            SELECT '{UNATTRIBUTED_CAMPAIGN_ID}', CAST(NULL AS STRING), 0
            FROM (SELECT COUNT(*) AS n FROM call_camp
                  WHERE google_ads_campaign_id = '{UNATTRIBUTED_CAMPAIGN_ID}')
            WHERE n > 0
        )
        SELECT
          g.google_ads_campaign_id AS campaign_id,
          g.campaign_name,
          g.clicks,
          COALESCE(gc.calls, 0)     AS calls,
          COALESCE(gb.booked, 0)    AS booked,
          COALESCE(s.spend, 0)      AS spend,
          COALESCE(cr.revenue, 0)   AS revenue,
          COALESCE(cr.invoice_count, 0) AS invoice_count
        FROM base g
        LEFT JOIN spend s USING (google_ads_campaign_id)
        LEFT JOIN gads_calls gc USING (google_ads_campaign_id)
        LEFT JOIN gads_booked gb USING (google_ads_campaign_id)
        LEFT JOIN campaign_revenue cr USING (google_ads_campaign_id)
        ORDER BY g.clicks DESC
    """
    client = _client()
    out: list[dict] = []
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
        bigquery.ScalarQueryParameter("match_days", "INT64", int(match_days)),
    ])
    for r in client.query(sql, job_config=job_config).result():
        clicks   = int(r.clicks or 0)
        calls    = int(r.calls or 0)
        booked   = int(r.booked or 0)
        spend    = float(r.spend or 0)
        revenue  = float(r.revenue or 0)
        invoices = int(r.invoice_count or 0)
        cpc      = (spend / clicks) if clicks else 0.0
        cpcall   = (spend / calls)  if calls  else 0.0
        cpbook   = (spend / booked) if booked else 0.0
        roas     = (revenue / spend) if spend else 0.0
        rev_per_book = (revenue / booked) if booked else 0.0
        click_to_call = (calls  / clicks) * 100 if clicks else 0.0
        call_to_book  = (booked / calls)  * 100 if calls  else 0.0
        unattributed  = r.campaign_id == UNATTRIBUTED_CAMPAIGN_ID
        out.append({
            "campaign_id":       r.campaign_id,
            "campaign_name":     (UNATTRIBUTED_CAMPAIGN_NAME if unattributed
                                  else (r.campaign_name or r.campaign_id)),
            "unattributed":      unattributed,
            "clicks":            clicks,
            "calls":             calls,
            "booked":            booked,
            "spend":             spend,
            "revenue":           revenue,
            "invoice_count":     invoices,
            "cpc":               cpc,
            "cost_per_call":     cpcall,
            "cost_per_booking":  cpbook,
            "roas":              roas,
            "revenue_per_booking": rev_per_book,
            "click_to_call_pct": click_to_call,
            "call_to_book_pct":  call_to_book,
        })
    return out


# ── Web-form submissions (parallel lead source) ──────────────────────────────

def webform_submissions(clinic_id: str, days: int = 90, window: "Window | None" = None) -> dict[str, Any]:
    """Website form-submission volume for the clinic over the window.

    Reads ``ClinicData.webforms`` (written live by the hypervisor ``POST
    /webforms`` endpoint and by one-off backfills), filtered by ``clinic_id``
    and ``submitted_at``. Forms are a lead source parallel to inbound calls —
    this returns volume only (total + new/returning split from
    ``customer_type``); it is not threaded into the call→revenue funnel.

    Returns zeros (never raises) if the table or columns are absent, so the
    funnel section renders cleanly for clinics with no form data yet.
    """
    w = _win(window, days)
    out = {"total": 0, "new": 0, "returning": 0, "window_days": w.span_days}
    try:
        rows = list(_client().query(
            f"""
                SELECT
                  COUNT(*)                                            AS total,
                  COUNTIF(LOWER(IFNULL(customer_type, '')) LIKE '%new%')    AS new_customers,
                  COUNTIF(LOWER(IFNULL(customer_type, '')) LIKE '%return%') AS returning
                FROM `{_CLINIC_DATA}.webforms`
                WHERE clinic_id = @clinic_id
                  AND {_ts_between("submitted_at", w)}
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
        ).result())
    except Exception as exc:  # table/columns may not exist yet for some clinics
        log.warning("webform_submissions query failed for clinic_id=%s: %s", clinic_id, exc)
        return out
    if rows:
        r = rows[0]
        out["total"]     = int(r.total or 0)
        out["new"]       = int(r.new_customers or 0)
        out["returning"] = int(r.returning or 0)
    return out


def webform_revenue(clinic_id: str, days: int = 90, window: "Window | None" = None) -> dict[str, Any]:
    """Revenue attributed to web-form submitters via Blueprint_PHI.

    Mirrors the ``google_ads_roi`` patient→invoice join, but keyed off web-form
    contact details instead of GCLID-matched calls. A form submission is matched
    to a Blueprint patient when its phone (any of the three ``ClientDemographics``
    slots, last-10-digit normalised) OR its email (``email_address``,
    lower-trimmed) matches. For each matched patient, invoices dated on/after that
    patient's earliest matching submission are summed once (deduped per patient),
    so revenue isn't double-counted across multiple submissions.

    Returns ``matched_patients`` (distinct patients tied to a form),
    ``invoiced_patients``, ``invoice_count`` and ``attributed_revenue``. Fails
    safe to zeros (never raises) when the webform table or Blueprint tables are
    absent for the clinic.
    """
    w = _win(window, days)
    out = {
        "matched_patients": 0, "invoiced_patients": 0,
        "invoice_count": 0, "attributed_revenue": 0.0, "window_days": w.span_days,
    }
    sql = f"""
        WITH forms AS (
            SELECT
              RIGHT(REGEXP_REPLACE(IFNULL(phone_number, ''), r'\\D', ''), 10) AS phone_norm,
              LOWER(TRIM(IFNULL(email, '')))                                   AS email_norm,
              DATE(submitted_at)                                              AS submitted_date
            FROM `{_CLINIC_DATA}.webforms`
            WHERE clinic_id = @clinic_id
              AND {_ts_between("submitted_at", w)}
        ),
        patients AS (
            SELECT DISTINCT client_id, phone_norm, email_norm
            FROM `{_PATIENT_CONTACTS}`
            WHERE _clinic_id = @clinic_id
        ),
        matched AS (
            -- Distinct patients tied to a form by phone (10-digit) OR email.
            -- Earliest matching submission gates which invoices count.
            SELECT
              p.client_id,
              MIN(f.submitted_date) AS first_form_date
            FROM forms f
            JOIN patients p
              ON (LENGTH(f.phone_norm) = 10 AND f.phone_norm = p.phone_norm)
              OR (f.email_norm != ''        AND f.email_norm = p.email_norm)
            GROUP BY p.client_id
        ),
        rev AS (
            SELECT
              COUNT(DISTINCT im.client_id) AS invoiced_patients,
              COUNT(DISTINCT im.order_id)  AS invoice_count,
              SUM(SAFE_CAST(im.order_total_with_tax AS NUMERIC)) AS revenue
            FROM matched m
            JOIN `{_BP}.InvoiceMaster` im
              ON im._clinic_id = @clinic_id
             AND im.client_id = m.client_id
            WHERE SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
              AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date) >= m.first_form_date
        )
        SELECT
          (SELECT COUNT(*) FROM matched) AS matched_patients,
          rev.invoiced_patients,
          rev.invoice_count,
          rev.revenue
        FROM rev
    """
    try:
        rows = list(_client().query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
        ).result())
    except Exception as exc:
        log.warning("webform_revenue query failed for clinic_id=%s: %s", clinic_id, exc)
        return out
    if rows:
        r = rows[0]
        out["matched_patients"]   = int(r.matched_patients or 0)
        out["invoiced_patients"]  = int(r.invoiced_patients or 0)
        out["invoice_count"]      = int(r.invoice_count or 0)
        out["attributed_revenue"] = float(r.revenue or 0.0)
    return out


def webform_appointments(clinic_id: str, days: int = 90, window: "Window | None" = None) -> dict[str, Any]:
    """Web-form submissions reconciled to PMS appointments.

    Same submitter→patient matching as :func:`webform_revenue` (phone last-10
    OR email, against ``PMS_Unified.patient_contacts``), but chased into
    ``Appointments`` instead of invoices: a submission "led to an appointment"
    when any matched patient has an appointment CREATED on/after the
    submission date — no upper bound, so a fortnight-old form that books next
    month still counts once the appointment lands.

    Units are SUBMISSIONS (each form counted once, even if its submitter
    matches several patient records or books several appointments);
    ``appt_patients`` adds the distinct-people view. Correlational like the
    rest of the webform section: it credits forms whose submitter later booked,
    not proof the form drove the visit.

    Returns ``{"submissions", "matched_submissions", "appt_submissions",
    "appt_patients", "window_days"}``. Fails safe to zeros (never raises) when
    the webform table is absent.
    """
    w = _win(window, days)
    out = {
        "submissions": 0, "matched_submissions": 0,
        "appt_submissions": 0, "appt_patients": 0, "window_days": w.span_days,
    }
    sql = f"""
        WITH forms AS (
            SELECT
              ROW_NUMBER() OVER (ORDER BY submitted_at)                        AS form_idx,
              RIGHT(REGEXP_REPLACE(IFNULL(phone_number, ''), r'\\D', ''), 10)  AS phone_norm,
              LOWER(TRIM(IFNULL(email, '')))                                   AS email_norm,
              DATE(submitted_at)                                               AS submitted_date
            FROM `{_CLINIC_DATA}.webforms`
            WHERE clinic_id = @clinic_id
              AND {_ts_between("submitted_at", w)}
        ),
        patients AS (
            SELECT DISTINCT client_id, phone_norm, email_norm
            FROM `{_PATIENT_CONTACTS}`
            WHERE _clinic_id = @clinic_id
        ),
        -- One row per (submission, matched patient) — a submission may match
        -- several patient records; it still counts once in every stage.
        matched AS (
            SELECT f.form_idx, f.submitted_date, p.client_id
            FROM forms f
            JOIN patients p
              ON (LENGTH(f.phone_norm) = 10 AND f.phone_norm = p.phone_norm)
              OR (f.email_norm != ''        AND f.email_norm = p.email_norm)
        ),
        appt AS (
            SELECT m.form_idx, m.client_id
            FROM matched m
            JOIN `{_BP}.Appointments` a
              ON a._clinic_id = @clinic_id
             AND a.client_id = m.client_id
             AND DATE(SAFE_CAST(a.created_time AS TIMESTAMP)) >= m.submitted_date
        )
        SELECT
          (SELECT COUNT(*) FROM forms)                     AS submissions,
          (SELECT COUNT(DISTINCT form_idx) FROM matched)   AS matched_submissions,
          (SELECT COUNT(DISTINCT form_idx) FROM appt)      AS appt_submissions,
          (SELECT COUNT(DISTINCT client_id) FROM appt)     AS appt_patients
    """
    try:
        rows = list(_client().query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
        ).result())
    except Exception as exc:
        log.warning("webform_appointments query failed for clinic_id=%s: %s", clinic_id, exc)
        return out
    if rows:
        r = rows[0]
        out["submissions"]         = int(r.submissions or 0)
        out["matched_submissions"] = int(r.matched_submissions or 0)
        out["appt_submissions"]    = int(r.appt_submissions or 0)
        out["appt_patients"]       = int(r.appt_patients or 0)
    return out


def webform_appointments_group(
    clinic_ids: list[str], days: int = 90, window: "Window | None" = None,
) -> dict[str, Any]:
    """Group-wide :func:`webform_appointments`.

    Three of the four counters are SUBMISSION-keyed, and a submission belongs to
    exactly one clinic (``webforms.clinic_id``), so ``submissions`` /
    ``matched_submissions`` / ``appt_submissions`` are genuinely additive — this
    reader returns the same numbers summing the per-clinic results would. It
    exists for the fourth:

    ``appt_patients`` is a distinct-PEOPLE count, and ``client_id`` is scoped to
    a clinic (see :func:`paid_call_revenue_group`), so summing counts a person on
    file at two locations twice. Group-wide it is keyed on the SUBMITTER'S
    CONTACT (phone, else email) — the identity the form itself carries and the
    only one comparable across locations. One person who submitted at two
    locations and booked at both counts ONCE.

    The submission key is likewise made global: the per-clinic reader's
    ``ROW_NUMBER`` restarts at 1 for each clinic, so it would collide here.
    """
    w = _win(window, days)
    out = {
        "submissions": 0, "matched_submissions": 0,
        "appt_submissions": 0, "appt_patients": 0, "window_days": w.span_days,
    }
    if not clinic_ids:
        return out
    sql = f"""
        WITH forms AS (
            SELECT
              -- Ordered by clinic first so the key is unique ACROSS the group;
              -- a per-clinic ROW_NUMBER would restart at 1 and collide.
              ROW_NUMBER() OVER (ORDER BY clinic_id, submitted_at)             AS form_idx,
              clinic_id,
              RIGHT(REGEXP_REPLACE(IFNULL(phone_number, ''), r'\\D', ''), 10)  AS phone_norm,
              LOWER(TRIM(IFNULL(email, '')))                                   AS email_norm,
              DATE(submitted_at)                                               AS submitted_date
            FROM `{_CLINIC_DATA}.webforms`
            WHERE clinic_id IN UNNEST(@clinic_ids)
              AND {_ts_between("submitted_at", w)}
        ),
        patients AS (
            SELECT DISTINCT _clinic_id, client_id, phone_norm, email_norm
            FROM `{_PATIENT_CONTACTS}`
            WHERE _clinic_id IN UNNEST(@clinic_ids)
        ),
        -- A form is matched against ITS OWN clinic's patient records only;
        -- dropping that predicate would match a submission to a namesake at
        -- another location.
        matched AS (
            SELECT f.form_idx, f.submitted_date, f.phone_norm, f.email_norm,
                   p._clinic_id, p.client_id
            FROM forms f
            JOIN patients p
              ON p._clinic_id = f.clinic_id
             AND ((LENGTH(f.phone_norm) = 10 AND f.phone_norm = p.phone_norm)
               OR (f.email_norm != ''        AND f.email_norm = p.email_norm))
        ),
        appt AS (
            SELECT m.form_idx, m.phone_norm, m.email_norm
            FROM matched m
            JOIN `{_BP}.Appointments` a
              ON a._clinic_id = m._clinic_id
             AND a.client_id = m.client_id
             AND DATE(SAFE_CAST(a.created_time AS TIMESTAMP)) >= m.submitted_date
        )
        SELECT
          (SELECT COUNT(*) FROM forms)                     AS submissions,
          (SELECT COUNT(DISTINCT form_idx) FROM matched)   AS matched_submissions,
          (SELECT COUNT(DISTINCT form_idx) FROM appt)      AS appt_submissions,
          -- Person = the contact the form carried, so a submitter who booked at
          -- two locations is one person, not two client records.
          (SELECT COUNT(DISTINCT IF(LENGTH(phone_norm) = 10, phone_norm, email_norm))
             FROM appt)                                    AS appt_patients
    """
    try:
        rows = list(_client().query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ArrayQueryParameter("clinic_ids", "STRING", list(clinic_ids)),
            ]),
        ).result())
    except Exception as exc:
        log.warning("webform_appointments_group query failed for %d clinics: %s",
                    len(clinic_ids), exc)
        return out
    if rows:
        r = rows[0]
        out["submissions"]         = int(r.submissions or 0)
        out["matched_submissions"] = int(r.matched_submissions or 0)
        out["appt_submissions"]    = int(r.appt_submissions or 0)
        out["appt_patients"]       = int(r.appt_patients or 0)
    return out


def webform_funnel(clinic_id: str, days: int = 90, window: "Window | None" = None) -> dict[str, Any]:
    """Web-form funnel broken down by UTM source: source → submissions → invoice.

    Per UTM source, counts submissions and how many converted to a paying
    patient (submitter matched to a Blueprint patient by phone OR email, with an
    invoice dated on/after the submission). Matching mirrors
    :func:`webform_revenue`; here it's grouped by ``utm_source`` and counted per
    submission (deduped via a deterministic ``ROW_NUMBER`` form id, never
    ``GENERATE_UUID``/``RAND`` which would defeat BigQuery's query cache).

    NULL/blank ``utm_source`` rolls up to "Direct / untagged" (surfaces the
    tracking-coverage gap, like the call funnel's "Untagged" channel). Per-source
    revenue dedups invoices by ``order_id`` within a source; a patient who
    submitted under two sources can be counted in both (same convention as
    :func:`google_ads_roi`). Fails safe to empty (never raises).
    """
    w = _win(window, days)
    out = {
        "sources": [], "total_submissions": 0,
        "total_invoiced": 0, "total_revenue": 0.0, "window_days": w.span_days,
    }
    sql = f"""
        WITH forms AS (
            SELECT
              ROW_NUMBER() OVER (ORDER BY submitted_at, email, phone_number)   AS form_id,
              -- Three-way, NOT two. Until 2026-08-10 the sites wrote the
              -- referring host into `utm_source`, so this grouped referrers and
              -- called them campaign sources; `referrer_host` now holds those
              -- (webforms._utm). A referrer is real signal and must not be
              -- dumped into 'Direct / untagged', but it is NOT a campaign, so
              -- the label says so and `source_kind` makes it machine-readable.
              CASE
                WHEN NULLIF(TRIM(utm_source), '')    IS NOT NULL THEN TRIM(utm_source)
                -- 'direct' is the sites' marker for NO referrer, so labelling it
                -- "direct (referrer)" would contradict itself.
                WHEN LOWER(TRIM(referrer_host)) = 'direct' THEN 'Direct (no referrer)'
                WHEN NULLIF(TRIM(referrer_host), '') IS NOT NULL
                     THEN CONCAT(TRIM(referrer_host), ' (referrer)')
                ELSE 'Direct / untagged'
              END                                                             AS source,
              CASE
                WHEN NULLIF(TRIM(utm_source), '')    IS NOT NULL THEN 'utm'
                WHEN NULLIF(TRIM(referrer_host), '') IS NOT NULL THEN 'referrer'
                ELSE 'untagged'
              END                                                             AS source_kind,
              RIGHT(REGEXP_REPLACE(IFNULL(phone_number, ''), r'\\D', ''), 10)  AS phone_norm,
              LOWER(TRIM(IFNULL(email, '')))                                   AS email_norm,
              DATE(submitted_at)                                              AS submitted_date
            FROM `{_CLINIC_DATA}.webforms`
            WHERE clinic_id = @clinic_id
              AND {_ts_between("submitted_at", w)}
        ),
        submissions AS (
            -- source_kind is functionally dependent on source (the label
            -- encodes it), so grouping by both cannot split a source row.
            SELECT source, source_kind, COUNT(*) AS submissions
            FROM forms GROUP BY source, source_kind
        ),
        patients AS (
            SELECT DISTINCT client_id, phone_norm, email_norm
            FROM `{_PATIENT_CONTACTS}`
            WHERE _clinic_id = @clinic_id
        ),
        form_clients AS (
            -- each submission paired with any Blueprint patient it matches
            SELECT DISTINCT f.form_id, f.source, f.submitted_date, p.client_id
            FROM forms f
            JOIN patients p
              ON (LENGTH(f.phone_norm) = 10 AND f.phone_norm = p.phone_norm)
              OR (f.email_norm != ''        AND f.email_norm = p.email_norm)
        ),
        form_invoices AS (
            -- qualifying invoices for a submission's matched patient(s)
            SELECT
              fc.form_id, fc.source, im.order_id,
              SAFE_CAST(im.order_total_with_tax AS NUMERIC) AS amount
            FROM form_clients fc
            JOIN `{_BP}.InvoiceMaster` im
              ON im._clinic_id = @clinic_id
             AND im.client_id = fc.client_id
            WHERE SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
              AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date) >= fc.submitted_date
        ),
        invoiced_by_source AS (
            -- distinct submissions that converted to ≥1 qualifying invoice
            SELECT source, COUNT(DISTINCT form_id) AS invoiced
            FROM form_invoices GROUP BY source
        ),
        revenue_by_source AS (
            -- each invoice counted once per source (dedup by order_id)
            SELECT source, SUM(amount) AS revenue FROM (
                SELECT source, order_id, ANY_VALUE(amount) AS amount
                FROM form_invoices GROUP BY source, order_id
            ) GROUP BY source
        )
        SELECT
          s.source,
          s.source_kind,
          s.submissions,
          IFNULL(i.invoiced, 0) AS invoiced,
          IFNULL(r.revenue, 0)  AS revenue
        FROM submissions s
        LEFT JOIN invoiced_by_source i USING (source)
        LEFT JOIN revenue_by_source  r USING (source)
        ORDER BY s.submissions DESC, s.source
    """
    try:
        rows = list(_client().query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
        ).result())
    except Exception as exc:
        log.warning("webform_funnel query failed for clinic_id=%s: %s", clinic_id, exc)
        return out
    for r in rows:
        out["sources"].append({
            "source":      r.source,
            "source_kind": r.source_kind,
            "submissions": int(r.submissions or 0),
            "invoiced":    int(r.invoiced or 0),
            "revenue":     float(r.revenue or 0.0),
        })
    out["total_submissions"] = sum(s["submissions"] for s in out["sources"])
    out["total_invoiced"]    = sum(s["invoiced"] for s in out["sources"])
    out["total_revenue"]     = sum(s["revenue"] for s in out["sources"])
    return out


def _last_two_full_months() -> tuple[_dt.date, _dt.date, _dt.date]:
    """(prior_month_first, last_full_month_first, current_month_first) in UTC.

    'Last full month' is the most recent month that has fully elapsed (never the
    in-progress month); 'prior' is the one before it. E.g. mid-June → (Apr 1,
    May 1, Jun 1)."""
    today = _dt.datetime.now(_dt.timezone.utc).date()
    cur_first = today.replace(day=1)
    last_first = (cur_first - _dt.timedelta(days=1)).replace(day=1)
    prior_first = (last_first - _dt.timedelta(days=1)).replace(day=1)
    return prior_first, last_first, cur_first


def headline_metrics(clinic_id: str, invoca_campaign_ids: list[str]) -> dict[str, Any]:
    """Month-over-month KPIs for the headline section: the last fully-elapsed
    month vs the month before it (day-window independent).

    Per month: ``connected`` calls (non-spam, real conversation) and ``booked``
    (connected + appointment_booked) → phone-call **capture rate** = booked /
    connected; web-form ``submissions`` and ``form_bookings`` (submitter matched
    to a Blueprint patient with an appointment on/after submission) → **form
    response rate** = form_bookings / submissions. ``calls`` and total invoiced
    ``revenue`` are carried as context for the headline writer. Each sub-query is
    fail-safe.
    """
    prior_first, last_first, cur_first = _last_two_full_months()
    labels = {
        prior_first.strftime("%Y-%m"): "prior",
        last_first.strftime("%Y-%m"): "last",
    }

    def _slot(d: _dt.date) -> dict:
        return {
            "month": d.strftime("%Y-%m"), "label": d.strftime("%b %Y"),
            "calls": 0, "connected": 0, "booked": 0,
            "submissions": 0, "form_bookings": 0, "revenue": 0.0,
            "capture_rate": None, "form_rate": None,
        }

    out = {"prior": _slot(prior_first), "last": _slot(last_first)}
    lo = f"{prior_first.isoformat()} 00:00:00+00:00"
    hi = f"{cur_first.isoformat()} 00:00:00+00:00"      # exclusive: drops the in-progress month
    lo_d, hi_d = prior_first.isoformat(), cur_first.isoformat()
    client = _client()

    def _run(sql, params=None):
        try:
            cfg = bigquery.QueryJobConfig(query_parameters=params) if params else None
            return list(client.query(sql, job_config=cfg).result())
        except Exception as exc:
            log.warning("headline_metrics sub-query failed clinic=%s: %s", clinic_id, exc)
            return []

    if invoca_campaign_ids:
        in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
        for r in _run(f"""
            WITH c AS (
                SELECT
                  FORMAT_TIMESTAMP('%Y-%m', t.timestamp)        AS mo,
                  IFNULL(cs.spam_or_solicitor, FALSE)           AS is_spam,
                  (cs.complete_call_id IS NOT NULL)             AS has_cs,
                  IFNULL(cs.no_conversation, FALSE)             AS no_conv,
                  IFNULL(cs.appointment_booked, FALSE)          AS booked
                FROM `{_CLINIC_DATA}.transactions` t
                LEFT JOIN `{_CLINIC_DATA}.callscoring` cs
                  ON cs.complete_call_id = t.complete_call_id
                WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
                  AND t.timestamp >= TIMESTAMP('{lo}') AND t.timestamp < TIMESTAMP('{hi}')
            )
            SELECT mo,
              COUNTIF(NOT is_spam)                                      AS calls,
              COUNTIF(NOT is_spam AND has_cs AND NOT no_conv)           AS connected,
              COUNTIF(NOT is_spam AND has_cs AND NOT no_conv AND booked) AS booked
            FROM c GROUP BY mo
        """):
            slot = labels.get(r.mo)
            if slot:
                out[slot].update(calls=int(r.calls or 0), connected=int(r.connected or 0),
                                 booked=int(r.booked or 0))

    for r in _run(f"""
        WITH forms AS (
            SELECT
              ROW_NUMBER() OVER (ORDER BY submitted_at, email, phone_number) AS form_id,
              FORMAT_TIMESTAMP('%Y-%m', submitted_at)                        AS mo,
              DATE(submitted_at)                                            AS sd,
              RIGHT(REGEXP_REPLACE(IFNULL(phone_number,''), r'\\D',''), 10)  AS phone_norm,
              LOWER(TRIM(IFNULL(email,'')))                                 AS email_norm
            FROM `{_CLINIC_DATA}.webforms`
            WHERE clinic_id = @clinic_id
              AND submitted_at >= TIMESTAMP('{lo}') AND submitted_at < TIMESTAMP('{hi}')
        ),
        subs AS (SELECT mo, COUNT(*) AS submissions FROM forms GROUP BY mo),
        patients AS (
            SELECT DISTINCT client_id, phone_norm, email_norm
            FROM `{_PATIENT_CONTACTS}`
            WHERE _clinic_id = @clinic_id
        ),
        fc AS (
            SELECT DISTINCT f.form_id, f.mo, f.sd, p.client_id
            FROM forms f JOIN patients p
              ON (LENGTH(f.phone_norm)=10 AND f.phone_norm=p.phone_norm)
              OR (f.email_norm != '' AND f.email_norm=p.email_norm)
        ),
        appts AS (
            SELECT DISTINCT fc.mo, fc.form_id
            FROM fc JOIN `{_BP}.Appointments` a
              ON a._clinic_id = @clinic_id AND a.client_id = fc.client_id
            WHERE SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(a.start_time,1,10)) >= fc.sd
        ),
        book AS (SELECT mo, COUNT(DISTINCT form_id) AS form_bookings FROM appts GROUP BY mo)
        SELECT s.mo, s.submissions, IFNULL(b.form_bookings,0) AS form_bookings
        FROM subs s LEFT JOIN book b USING (mo)
    """, _params(clinic_id)):
        slot = labels.get(r.mo)
        if slot:
            out[slot].update(submissions=int(r.submissions or 0),
                             form_bookings=int(r.form_bookings or 0))

    for r in _run(f"""
        SELECT FORMAT_DATE('%Y-%m', SAFE.PARSE_DATE('%Y-%m-%d', invoice_date)) AS mo,
               SUM(SAFE_CAST(order_total_with_tax AS NUMERIC)) AS revenue
        FROM `{_BP}.InvoiceMaster`
        WHERE _clinic_id = @clinic_id
          AND SAFE_CAST(order_total_with_tax AS NUMERIC) > 0
          AND SAFE.PARSE_DATE('%Y-%m-%d', invoice_date) >= DATE '{lo_d}'
          AND SAFE.PARSE_DATE('%Y-%m-%d', invoice_date) <  DATE '{hi_d}'
        GROUP BY mo
    """, _params(clinic_id)):
        slot = labels.get(r.mo)
        if slot:
            out[slot]["revenue"] = float(r.revenue or 0.0)

    for slot in ("prior", "last"):
        d = out[slot]
        d["capture_rate"] = (d["booked"] / d["connected"]) if d["connected"] else None
        d["form_rate"] = (d["form_bookings"] / d["submissions"]) if d["submissions"] else None
    return out


def _month_buckets(days: int, window: "Window | None" = None) -> list[str]:
    """Ordered ``YYYY-MM`` labels from the window-start month through this month."""
    w = _win(window, days)
    start = w.start
    # Last month included is the month of the last included day (end_excl - 1).
    last = w.end_excl - _dt.timedelta(days=1)
    out, y, m = [], start.year, start.month
    while (y, m) <= (last.year, last.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def monthly_trends(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    ga_campaign_ids: list[str],
    days: int = 365,
    window: "Window | None" = None,
) -> list[dict]:
    """Per-month metrics for the two acquisition streams, for trend charts.

    One zero-filled row per month in the window. Everything is bucketed by the
    **acquisition-event month** (call month / submission month), so downstream
    bookings/invoices/revenue are attributed back to the month the lead came in
    (a cohort view — month M's calls and the revenue they eventually produced sit
    in the same bucket).

    Call stream (gated on linked Invoca campaigns):
      - ``calls``         — inbound calls (``transactions``)
      - ``bookings``      — calls with ``callscoring.appointment_booked``
      - ``call_invoices`` — invoices for booking-call patients (phone→Blueprint),
                            dated on/after the call; deduped by order per month
      - ``call_revenue``  — sum of those invoices

    Web-form stream:
      - ``submissions``       — web-form submissions (``webforms``)
      - ``webform_bookings``  — submissions whose submitter (phone/email→Blueprint)
                                has an appointment starting on/after submission
      - ``webform_revenue``   — invoices for matched submitters dated on/after
                                submission; deduped by order per month

    Each sub-query is independently fail-safe — a missing table leaves that
    stream's metrics at zero rather than dropping the whole series.
    """
    w = _win(window, days)
    months = _month_buckets(days, window=window)
    data = {
        mo: {"month": mo, "calls": 0, "bookings": 0, "call_invoices": 0,
             "call_revenue": 0.0, "submissions": 0, "webform_bookings": 0,
             "webform_revenue": 0.0}
        for mo in months
    }
    client = _client()

    def _run(sql: str, params=None):
        try:
            cfg = bigquery.QueryJobConfig(query_parameters=params) if params else None
            return list(client.query(sql, job_config=cfg).result())
        except Exception as exc:
            log.warning("monthly_trends sub-query failed for clinic_id=%s: %s", clinic_id, exc)
            return []

    # ── Call stream: calls, bookings, booking-attributed invoices + revenue ──
    if invoca_campaign_ids:
        in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
        for r in _run(f"""
            WITH calls AS (
                SELECT
                  FORMAT_TIMESTAMP('%Y-%m', t.timestamp)                       AS mo,
                  DATE(t.timestamp)                                            AS call_date,
                  t.transaction_id,
                  RIGHT(REGEXP_REPLACE(IFNULL(t.calling_phone_number,''), r'\\D',''), 10) AS phone_norm,
                  IFNULL(cs.appointment_booked, FALSE)                         AS booked
                FROM `{_CLINIC_DATA}.transactions` t
                LEFT JOIN `{_CLINIC_DATA}.callscoring` cs
                  ON cs.complete_call_id = t.complete_call_id
                WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
                  AND {_ts_between("t.timestamp", w)}
            ),
            counts AS (
                SELECT mo, COUNT(*) AS calls, COUNTIF(booked) AS bookings
                FROM calls GROUP BY mo
            ),
            patients AS (
                SELECT DISTINCT client_id, phone_norm
                FROM `{_PATIENT_CONTACTS}`
                WHERE _clinic_id = @clinic_id
                  AND LENGTH(phone_norm) = 10
            ),
            booking_clients AS (
                SELECT DISTINCT c.mo, c.call_date, p.client_id
                FROM calls c
                JOIN patients p ON p.phone_norm = c.phone_norm
                WHERE c.booked AND LENGTH(c.phone_norm) = 10
            ),
            booking_invoices AS (
                SELECT bc.mo, im.order_id,
                       ANY_VALUE(SAFE_CAST(im.order_total_with_tax AS NUMERIC)) AS amt
                FROM booking_clients bc
                JOIN `{_BP}.InvoiceMaster` im
                  ON im._clinic_id = @clinic_id AND im.client_id = bc.client_id
                WHERE SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
                  AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date) >= bc.call_date
                GROUP BY bc.mo, im.order_id
            ),
            inv_agg AS (
                SELECT mo, COUNT(*) AS call_invoices, SUM(amt) AS call_revenue
                FROM booking_invoices GROUP BY mo
            )
            SELECT
              c.mo, c.calls, c.bookings,
              IFNULL(i.call_invoices, 0) AS call_invoices,
              IFNULL(i.call_revenue, 0)  AS call_revenue
            FROM counts c LEFT JOIN inv_agg i USING (mo)
        """, _params(clinic_id)):
            if r.mo in data:
                data[r.mo]["calls"]         = int(r.calls or 0)
                data[r.mo]["bookings"]      = int(r.bookings or 0)
                data[r.mo]["call_invoices"] = int(r.call_invoices or 0)
                data[r.mo]["call_revenue"]  = float(r.call_revenue or 0.0)

    # ── Web-form stream: submissions, associated bookings, revenue ───────────
    for r in _run(f"""
        WITH forms AS (
            SELECT
              ROW_NUMBER() OVER (ORDER BY submitted_at, email, phone_number)  AS form_id,
              FORMAT_TIMESTAMP('%Y-%m', submitted_at)                         AS mo,
              DATE(submitted_at)                                             AS submitted_date,
              RIGHT(REGEXP_REPLACE(IFNULL(phone_number,''), r'\\D',''), 10)   AS phone_norm,
              LOWER(TRIM(IFNULL(email,'')))                                  AS email_norm
            FROM `{_CLINIC_DATA}.webforms`
            WHERE clinic_id = @clinic_id
              AND {_ts_between("submitted_at", w)}
        ),
        sub_counts AS (SELECT mo, COUNT(*) AS submissions FROM forms GROUP BY mo),
        patients AS (
            SELECT DISTINCT client_id, phone_norm, email_norm
            FROM `{_PATIENT_CONTACTS}`
            WHERE _clinic_id = @clinic_id
        ),
        form_clients AS (
            SELECT DISTINCT f.form_id, f.mo, f.submitted_date, p.client_id
            FROM forms f
            JOIN patients p
              ON (LENGTH(f.phone_norm) = 10 AND f.phone_norm = p.phone_norm)
              OR (f.email_norm != ''        AND f.email_norm = p.email_norm)
        ),
        form_appts AS (
            -- submissions whose matched patient has an appointment on/after submission
            SELECT DISTINCT fc.mo, fc.form_id
            FROM form_clients fc
            JOIN `{_BP}.Appointments` a
              ON a._clinic_id = @clinic_id AND a.client_id = fc.client_id
            WHERE SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(a.start_time, 1, 10)) >= fc.submitted_date
        ),
        book_agg AS (
            SELECT mo, COUNT(DISTINCT form_id) AS webform_bookings
            FROM form_appts GROUP BY mo
        ),
        form_invoices AS (
            SELECT fc.mo, im.order_id,
                   ANY_VALUE(SAFE_CAST(im.order_total_with_tax AS NUMERIC)) AS amt
            FROM form_clients fc
            JOIN `{_BP}.InvoiceMaster` im
              ON im._clinic_id = @clinic_id AND im.client_id = fc.client_id
            WHERE SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
              AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date) >= fc.submitted_date
            GROUP BY fc.mo, im.order_id
        ),
        rev_agg AS (
            SELECT mo, SUM(amt) AS webform_revenue FROM form_invoices GROUP BY mo
        )
        SELECT
          s.mo, s.submissions,
          IFNULL(b.webform_bookings, 0) AS webform_bookings,
          IFNULL(r.webform_revenue, 0)  AS webform_revenue
        FROM sub_counts s
        LEFT JOIN book_agg b USING (mo)
        LEFT JOIN rev_agg  r USING (mo)
    """, _params(clinic_id)):
        if r.mo in data:
            data[r.mo]["submissions"]      = int(r.submissions or 0)
            data[r.mo]["webform_bookings"] = int(r.webform_bookings or 0)
            data[r.mo]["webform_revenue"]  = float(r.webform_revenue or 0.0)

    return [data[mo] for mo in months]


# ── Marketing channel mix (for the Sankey preamble) ──────────────────────────

# ── UTM filter (call funnel) ─────────────────────────────────────────────────
#
# The call funnel can be narrowed by ``utm_source`` and/or ``utm_medium``. Each is
# a multi-select include-list: pick the sources and the mediums you want counted.
# A row is kept when its source is in the chosen sources (if any) AND its medium
# is in the chosen mediums (if any) — an empty list means "no constraint on that
# dimension". Values are messy in the wild (NULL, 'nan', 'google' vs 'google.com',
# 'cpc' vs 'paid search'); matching is case-insensitive + trimmed.

_UTM_NOISE = ("", "nan", "null", "none")


def _utm_clean(values: list[str] | None) -> list[str]:
    """Lowercase/trim/dedupe an include-list, dropping blanks. Preserves order."""
    out: list[str] = []
    for v in values or []:
        s = (v or "").strip().lower()
        if s and s not in out:
            out.append(s)
    return out


def _utm_filter_sql(
    utm_sources: list[str] | None,
    utm_mediums: list[str] | None,
    alias: str = "t",
) -> str:
    """Extra WHERE fragment (with leading ' AND ') for the UTM filter, else ''.

    References the bound array params ``@utm_sources`` / ``@utm_mediums``.
    """
    parts = []
    if _utm_clean(utm_sources):
        parts.append(f"LOWER(TRIM({alias}.utm_source)) IN UNNEST(@utm_sources)")
    if _utm_clean(utm_mediums):
        parts.append(f"LOWER(TRIM({alias}.utm_medium)) IN UNNEST(@utm_mediums)")
    return (" AND " + " AND ".join(parts)) if parts else ""


def _utm_params(
    utm_sources: list[str] | None,
    utm_mediums: list[str] | None,
) -> list[bigquery.ArrayQueryParameter]:
    """Array query params for the active UTM dimensions (empty when unfiltered)."""
    params = []
    srcs = _utm_clean(utm_sources)
    meds = _utm_clean(utm_mediums)
    if srcs:
        params.append(bigquery.ArrayQueryParameter("utm_sources", "STRING", srcs))
    if meds:
        params.append(bigquery.ArrayQueryParameter("utm_mediums", "STRING", meds))
    return params


def _utm_options(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    column: str,
    days: int,
    window: "Window | None" = None,
) -> list[dict]:
    """Distinct values of ``utm_source``/``utm_medium`` (with non-spam call counts)
    for a filter dropdown, descending by volume. Drops NULL/blank/'nan' noise."""
    if not invoca_campaign_ids:
        return []
    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    join_cs = _callscoring_join_sql()
    not_spam = _non_spam_predicate_sql()
    noise = ", ".join(f"'{n}'" for n in _UTM_NOISE)
    rows = list(_client().query(f"""
        SELECT LOWER(TRIM(t.{column})) AS value, COUNT(*) AS n
        FROM `{_CLINIC_DATA}.transactions` t
        {join_cs}
        WHERE {scope}
          AND {not_spam}
          AND t.{column} IS NOT NULL
          AND LOWER(TRIM(t.{column})) NOT IN ({noise})
        GROUP BY value
        ORDER BY n DESC
    """).result())
    return [{"value": r.value, "calls": int(r.n or 0)} for r in rows]


def funnel_utm_sources(clinic_id: str, invoca_campaign_ids: list[str], days: int = 90, window: "Window | None" = None) -> list[dict]:
    """``utm_source`` options (value + call count) for the funnel filter."""
    return _utm_options(clinic_id, invoca_campaign_ids, "utm_source", days, window=window)


def funnel_utm_mediums(clinic_id: str, invoca_campaign_ids: list[str], days: int = 90, window: "Window | None" = None) -> list[dict]:
    """``utm_medium`` options (value + call count) for the funnel filter."""
    return _utm_options(clinic_id, invoca_campaign_ids, "utm_medium", days, window=window)


def channel_mix(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    utm_sources: list[str] | None = None,
    utm_mediums: list[str] | None = None,
    window: "Window | None" = None,
) -> list[dict]:
    """Counts of NON-SPAM calls by Invoca's ``marketing_channel`` for the
    Sankey's left-most column. Empty strings and NULLs roll up to ``Untagged``.

    Spam is filtered using the same heuristic as :func:`spam_calls_summary` /
    :func:`revenue_funnel` so the channel totals reconcile with the Sankey's
    Inbound Calls node (which is also non-spam).

    Returns rows like ``[{"channel": "Paid Search", "count": 123}, ...]``
    sorted by descending count.
    """
    if not invoca_campaign_ids:
        return []
    w = _win(window, days)
    client = _client()
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    join_cs = _callscoring_join_sql()
    not_spam = _non_spam_predicate_sql()
    utm_sql = _utm_filter_sql(utm_sources, utm_mediums)
    rows = list(client.query(
        f"""
        SELECT
          COALESCE(NULLIF(t.marketing_channel, ''), 'Untagged') AS channel,
          COUNT(*) AS n
        FROM `{_CLINIC_DATA}.transactions` t
        {join_cs}
        WHERE {scope}
          AND {not_spam}
          {utm_sql}
        GROUP BY channel
        ORDER BY n DESC
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=_utm_params(utm_sources, utm_mediums),
        ),
    ).result())
    return [{"channel": r.channel, "count": int(r.n or 0)} for r in rows]


# ── Traffic drivers: channel classification + paid-campaign attribution ──────
#
# Two readers powering §01's "drivers of call traffic":
#   • traffic_drivers()        — every non-spam call bucketed by acquisition
#                                channel (Paid / Organic / Direct / Referral /
#                                Social / No data) from gclid + UTM signals.
#   • paid_campaign_drivers()  — the Google Ads campaigns behind the Paid calls,
#                                via the gclid → ad_clicks_v2 join.
#
# WHY gclid alone is not enough: only ~11% of calls carry a real gclid. gclid is
# a *website* artifact (landing-page URL / cookie) captured by Invoca's dynamic-
# number-insertion pooling. Google **Call Extension / Call-Only** ads let the
# caller tap the number straight from the search result WITHOUT visiting the
# site, so no gclid is ever captured even though the call is genuinely paid
# search. Those calls (the majority of paid volume) are still bucketed Paid via
# utm_medium=cpc, but they CANNOT be tied to a specific campaign — no gclid, and
# utm_campaign is empty for them. paid_campaign_drivers() therefore covers only
# the website-tracked (Pooling) paid slice; the section states this explicitly.

# Canonical bucket order for stable rendering (chart + table).
TRAFFIC_CHANNELS = ["Paid", "Organic", "Direct", "Referral", "Social", "No data"]

# utm_medium values (lower-cased) that mean paid search / paid.
_PAID_MEDIUMS = ("cpc", "paid", "ppc", "paid search")
# utm_medium / utm_source signals (lower-cased) that mean paid or organic social.
_SOCIAL_MEDIUMS = (
    "instagram_stories", "instagram_reels", "facebook_mobile_feed",
    "facebook_instream_video", "facebook_mobile_reels", "social",
)
_SOCIAL_SOURCES = (
    "fb", "ig", "facebook.com", "instagram.com", "facebook", "instagram",
)


def _real_id_sql(col: str) -> str:
    """SQL predicate: TRUE when ``col`` holds a genuine click ID. Treats NULL,
    empty string, and the legacy ``'nan'`` sentinel all as absent. The ``'nan'``
    guard is defensive — the ETL now nulls that sentinel and the table has been
    backfilled — so any straggler row still classifies correctly."""
    return f"({col} IS NOT NULL AND {col} NOT IN ('nan', ''))"


def _channel_case_sql() -> str:
    """CASE expression mapping one deduped call row (aliased columns
    ``gclid/wbraid/gbraid/msclkid/fbclid``, lower-cased ``um``/``us``, and
    ``marketing_channel``) to a canonical channel bucket. Precedence: paid click
    IDs and paid mediums first, then social, then organic/direct/referral by
    utm_medium, then Invoca's marketing_channel as a last-resort tiebreaker for
    calls with no usable UTM, else 'No data'."""
    paid_ids = " OR ".join(_real_id_sql(c) for c in ("gclid", "wbraid", "gbraid", "msclkid"))
    paid_mediums = ", ".join(f"'{m}'" for m in _PAID_MEDIUMS)
    social_mediums = ", ".join(f"'{m}'" for m in _SOCIAL_MEDIUMS)
    social_sources = ", ".join(f"'{s}'" for s in _SOCIAL_SOURCES)
    return f"""
        CASE
          WHEN {paid_ids} OR um IN ({paid_mediums}) THEN 'Paid'
          WHEN {_real_id_sql('fbclid')} OR um IN ({social_mediums})
               OR us IN ({social_sources}) THEN 'Social'
          WHEN um = 'organic' THEN 'Organic'
          WHEN um = 'direct'  THEN 'Direct'
          WHEN um = 'referral' THEN 'Referral'
          -- No usable UTM medium: fall back to Invoca's own classification.
          WHEN marketing_channel = 'Paid Search'    THEN 'Paid'
          WHEN marketing_channel = 'Organic Search' THEN 'Organic'
          WHEN marketing_channel = 'Direct'         THEN 'Direct'
          WHEN marketing_channel = 'Referral'       THEN 'Referral'
          ELSE 'No data'
        END
    """


def traffic_drivers(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> list[dict]:
    """Non-spam calls bucketed by acquisition channel for the clinic's Invoca
    campaigns within the window. One row per ``complete_call_id`` (deduped), spam
    filtered via callscoring — so totals reconcile with §02/§03's call counts.

    Returns ``[{"channel", "calls", "pct"}, ...]`` in canonical
    :data:`TRAFFIC_CHANNELS` order, including zero-count buckets so the section
    always renders the full taxonomy. Empty Invoca list → ``[]``.
    """
    if not invoca_campaign_ids:
        return []
    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    join_cs = _callscoring_join_sql()
    not_spam = _non_spam_predicate_sql()
    channel_case = _channel_case_sql()
    sql = f"""
        WITH calls AS (
          SELECT
            {channel_case} AS channel
          FROM (
            SELECT
              t.gclid, t.wbraid, t.gbraid, t.msclkid, t.fbclid,
              LOWER(t.utm_medium) AS um,
              LOWER(t.utm_source) AS us,
              t.marketing_channel
            FROM `{_CLINIC_DATA}.transactions` t
            {join_cs}
            WHERE {scope}
              AND {not_spam}
            QUALIFY ROW_NUMBER() OVER (
              PARTITION BY t.complete_call_id ORDER BY t.timestamp DESC) = 1
          )
        )
        SELECT channel, COUNT(*) AS calls
        FROM calls
        GROUP BY channel
    """
    rows = {r.channel: int(r.calls or 0) for r in _client().query(sql).result()}
    total = sum(rows.values())
    return [
        {
            "channel": ch,
            "calls": rows.get(ch, 0),
            "pct": (100.0 * rows.get(ch, 0) / total) if total else 0.0,
        }
        for ch in TRAFFIC_CHANNELS
    ]


def paid_campaign_drivers(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    google_ads_campaign_ids: list[str],
    days: int = 90,
    top_n: int = 10,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """Google Ads campaigns driving Paid calls, via the gclid → ad_clicks_v2
    join. Covers ONLY the website-tracked (Pooling) paid calls that carry a real
    gclid — Call Extension / tap-to-call paid calls have no gclid and are absent
    here (see the module note above); the section surfaces the uncovered count.

    Returns ``{"campaigns": [{"campaign_name", "google_ads_campaign_id",
    "calls"}], "attributed_calls": int}`` — ``attributed_calls`` is the distinct
    gclid-tracked calls that matched a campaign, for the coverage caption.
    Empty when either campaign list is empty (no scope to join).
    """
    if not invoca_campaign_ids or not google_ads_campaign_ids:
        return {"campaigns": [], "attributed_calls": 0}
    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    join_cs = _callscoring_join_sql()
    not_spam = _non_spam_predicate_sql()
    in_ga = "(" + ", ".join(f"'{c}'" for c in google_ads_campaign_ids) + ")"
    real_gclid = _real_id_sql("t.gclid")
    sql = f"""
        WITH paid_calls AS (
          SELECT t.complete_call_id, t.gclid
          FROM `{_CLINIC_DATA}.transactions` t
          {join_cs}
          WHERE {scope}
            AND {not_spam}
            AND {real_gclid}
          QUALIFY ROW_NUMBER() OVER (
            PARTITION BY t.complete_call_id ORDER BY t.timestamp DESC) = 1
        ),
        attributed AS (
          SELECT pc.complete_call_id, ac.campaign_name, ac.google_ads_campaign_id
          FROM paid_calls pc
          INNER JOIN `{_CLINIC_DATA}.ad_clicks_v2` ac
            ON ac.click_view_gclid = pc.gclid
           AND ac.google_ads_campaign_id IN {in_ga}
          -- one campaign per call (a gclid can appear on >1 click row)
          QUALIFY ROW_NUMBER() OVER (
            PARTITION BY pc.complete_call_id ORDER BY ac.campaign_name) = 1
        )
        SELECT
          COALESCE(NULLIF(campaign_name, ''), google_ads_campaign_id) AS campaign_name,
          google_ads_campaign_id,
          COUNT(DISTINCT complete_call_id) AS calls
        FROM attributed
        GROUP BY campaign_name, google_ads_campaign_id
        ORDER BY calls DESC
        LIMIT {int(top_n)}
    """
    rows = list(_client().query(sql).result())
    campaigns = [
        {
            "campaign_name": r.campaign_name,
            "google_ads_campaign_id": r.google_ads_campaign_id,
            "calls": int(r.calls or 0),
        }
        for r in rows
    ]
    return {
        "campaigns": campaigns,
        "attributed_calls": sum(c["calls"] for c in campaigns),
    }


def ad_click_attribution(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    google_ads_campaign_ids: list[str],
    days: int = 90,
    top_n: int = 10,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """Attribute ad-click data to the calls themselves: split the clinic's Paid
    calls by how far each one can be traced back to a Google Ads campaign through
    its gclid. Powers the Overview's "Ad clicks → calls" section.

    The Paid population is defined by the SAME classifier as
    :func:`traffic_drivers` (``channel = 'Paid'``), so ``paid_calls`` reconciles
    exactly with that section's Paid bucket. Each paid call falls into one of
    four mutually-exclusive segments:

    * ``matched``               — real gclid that joins a click in
                                  ``ad_clicks_v2`` for a linked campaign → the
                                  call is tied to a specific campaign.
    * ``gclid_unmatched``       — real gclid, but no matching click row (click
                                  outside our ingest window / a campaign not
                                  linked to this clinic).
    * ``no_gclid_call_extension`` — no gclid, ``media_type = 'Google Call
                                  Extension'``: a tap-to-call from the search ad
                                  that never hit the website, so no click ID
                                  exists to capture (see the module note above).
    * ``no_gclid_web``          — no gclid, other media: reached the site but the
                                  click ID wasn't captured (cookie / consent).

    Returns the segment counts, the rolled-up ``has_gclid`` / ``no_gclid``
    totals, and ``campaigns`` (the matched campaigns, ranked). Empty Invoca list
    → all-zero shell so the section renders a clean empty state.
    """
    empty = {
        "paid_calls": 0, "matched": 0, "gclid_unmatched": 0,
        "no_gclid_call_extension": 0, "no_gclid_web": 0,
        "has_gclid": 0, "no_gclid": 0, "campaigns": [],
    }
    if not invoca_campaign_ids:
        return empty

    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    join_cs = _callscoring_join_sql()
    not_spam = _non_spam_predicate_sql()
    channel_case = _channel_case_sql()
    # Matched-click set for the clinic's linked Google Ads campaigns. Guard the
    # empty-list case so we never emit an invalid `IN ()` — with no linked GA
    # campaigns nothing can match, so every gclid lands in `gclid_unmatched`.
    ga_filter = (
        "google_ads_campaign_id IN (" + ", ".join(f"'{c}'" for c in google_ads_campaign_ids) + ")"
        if google_ads_campaign_ids else "FALSE"
    )
    sql = f"""
        WITH calls AS (
          SELECT
            gclid, media_type, has_gclid,
            {channel_case} AS channel
          FROM (
            SELECT
              t.gclid, t.media_type, t.wbraid, t.gbraid, t.msclkid, t.fbclid,
              LOWER(t.utm_medium) AS um, LOWER(t.utm_source) AS us, t.marketing_channel,
              (t.gclid IS NOT NULL AND t.gclid NOT IN ('nan', '')) AS has_gclid
            FROM `{_CLINIC_DATA}.transactions` t
            {join_cs}
            WHERE {scope}
              AND {not_spam}
            QUALIFY ROW_NUMBER() OVER (
              PARTITION BY t.complete_call_id ORDER BY t.timestamp DESC) = 1
          )
        ),
        paid AS (SELECT * FROM calls WHERE channel = 'Paid'),
        matched_clicks AS (
          SELECT DISTINCT click_view_gclid
          FROM `{_CLINIC_DATA}.ad_clicks_v2`
          WHERE {ga_filter}
        )
        SELECT
          COUNT(*) AS paid_calls,
          COUNTIF(p.has_gclid AND mc.click_view_gclid IS NOT NULL)  AS matched,
          COUNTIF(p.has_gclid AND mc.click_view_gclid IS NULL)      AS gclid_unmatched,
          COUNTIF(NOT p.has_gclid AND p.media_type = 'Google Call Extension') AS no_gclid_call_extension,
          COUNTIF(NOT p.has_gclid AND (p.media_type IS NULL OR p.media_type != 'Google Call Extension')) AS no_gclid_web
        FROM paid p
        LEFT JOIN matched_clicks mc ON mc.click_view_gclid = p.gclid
    """
    r = list(_client().query(sql).result())[0]
    matched = int(r.matched or 0)
    gclid_unmatched = int(r.gclid_unmatched or 0)
    no_gclid_ce = int(r.no_gclid_call_extension or 0)
    no_gclid_web = int(r.no_gclid_web or 0)
    campaigns = paid_campaign_drivers(
        clinic_id, invoca_campaign_ids, google_ads_campaign_ids,
        days=days, top_n=top_n, window=w,
    )["campaigns"]
    return {
        "paid_calls": int(r.paid_calls or 0),
        "matched": matched,
        "gclid_unmatched": gclid_unmatched,
        "no_gclid_call_extension": no_gclid_ce,
        "no_gclid_web": no_gclid_web,
        "has_gclid": matched + gclid_unmatched,
        "no_gclid": no_gclid_ce + no_gclid_web,
        "campaigns": campaigns,
    }


def ad_clicks_keywords(
    ga_campaign_ids: list[str],
    invoca_campaign_ids: list[str] | None = None,
    days: int = 90,
    window: "Window | None" = None,
    top_n: int = 15,
) -> dict[str, Any]:
    """Ad-click volume + keyword distribution for the linked Google Ads
    campaigns in the window (``ad_clicks_v2``, one row per click).

    Keywords come from ``click_view_keyword_info_text`` — clicks with no
    keyword (Performance Max / display placements, missing click data) roll up
    into a ``"(no keyword)"`` bucket so the distribution always sums to
    ``clicks``. Top ``top_n`` keywords returned individually; the tail rolls
    into ``other_clicks``.

    Each keyword also carries ``calls`` — non-spam calls (deduped per
    ``complete_call_id``) whose gclid joins one of that keyword's clicks. Only
    gclid-carrying calls can be keyword-attributed (Call Extension tap-to-call
    never has one), so ``keyword_attributed_calls`` is a floor, not the full
    paid call volume; the section states this.

    Returns ``{"clicks", "keywords": [{"keyword", "match_type", "clicks",
    "pct", "calls"}...], "other_clicks", "other_calls",
    "keyword_attributed_calls", "window_days"}``. Empty campaign list → zeros.
    """
    w = _win(window, days)
    out = {
        "clicks": 0, "keywords": [], "other_clicks": 0, "other_calls": 0,
        "keyword_attributed_calls": 0, "window_days": w.span_days,
    }
    if not ga_campaign_ids:
        return out
    in_ga = "(" + ", ".join(f"'{c}'" for c in ga_campaign_ids) + ")"
    # Keyword→call join is only possible with linked Invoca campaigns.
    if invoca_campaign_ids:
        in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
        calls_cte = f"""
        calls AS (
          -- Non-spam calls carrying a real gclid, deduped per call.
          SELECT gclid, complete_call_id FROM (
            SELECT t.gclid, t.complete_call_id,
                   IFNULL(cs.spam_or_solicitor, FALSE) AS is_spam
            FROM `{_CLINIC_DATA}.transactions` t
            LEFT JOIN `{_CLINIC_DATA}.callscoring` cs
              ON cs.complete_call_id = t.complete_call_id
            WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
              AND {_ts_between("t.timestamp", w)}
              AND t.gclid IS NOT NULL AND t.gclid NOT IN ('nan', '')
            QUALIFY ROW_NUMBER() OVER (
              PARTITION BY t.complete_call_id ORDER BY t.timestamp DESC) = 1
          ) WHERE NOT is_spam
        ),
        kw_calls AS (
          SELECT c.keyword, COUNT(DISTINCT ca.complete_call_id) AS calls
          FROM clicks c
          JOIN calls ca ON ca.gclid = c.click_view_gclid
          GROUP BY c.keyword
        ),"""
    else:
        calls_cte = """
        kw_calls AS (SELECT '' AS keyword, 0 AS calls FROM (SELECT 1) WHERE FALSE),"""
    sql = f"""
        WITH clicks AS (
          SELECT
            COALESCE(NULLIF(TRIM(click_view_keyword_info_text), ''), '(no keyword)') AS keyword,
            click_view_keyword_info_match_type AS match_type,
            click_view_gclid
          FROM `{_CLINIC_DATA}.ad_clicks_v2`
          WHERE google_ads_campaign_id IN {in_ga}
            AND {_ts_between("timestamp", w)}
        ),
        {calls_cte}
        kw AS (
          SELECT keyword, ANY_VALUE(match_type) AS match_type, COUNT(*) AS clicks
          FROM clicks GROUP BY keyword
        )
        SELECT kw.keyword, kw.match_type, kw.clicks, COALESCE(kc.calls, 0) AS calls
        FROM kw LEFT JOIN kw_calls kc USING (keyword)
        ORDER BY kw.clicks DESC
    """
    rows = list(_client().query(sql).result())
    total = sum(int(r.clicks or 0) for r in rows)
    total_calls = sum(int(r.calls or 0) for r in rows)
    out["clicks"] = total
    out["keyword_attributed_calls"] = total_calls
    for r in rows[:top_n]:
        n = int(r.clicks or 0)
        out["keywords"].append({
            "keyword": r.keyword,
            "match_type": r.match_type,
            "clicks": n,
            "pct": (100.0 * n / total) if total else 0.0,
            "calls": int(r.calls or 0),
        })
    out["other_clicks"] = total - sum(k["clicks"] for k in out["keywords"])
    out["other_calls"] = total_calls - sum(k["calls"] for k in out["keywords"])
    return out


def paid_call_revenue(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
    match_days: int = CALL_BOOKING_MATCH_DAYS,
) -> dict[str, Any]:
    """Revenue generated downstream of the clinic's PAID calls.

    The Paid population is the SAME classifier as :func:`traffic_drivers` /
    :func:`ad_click_attribution` (``channel = 'Paid'``: real paid click ID,
    paid utm_medium, or Invoca's 'Paid Search' fallback), spam excluded,
    deduped per ``complete_call_id`` — so ``paid_calls`` reconciles with those
    sections. Paid callers are phone-matched (last-10-digit) to
    ``PMS_Unified.patient_contacts``; each matched patient's positive
    ``InvoiceMaster`` invoices dated inside the window are summed, deduped per
    ``order_id`` (MAX amount per order).

    This is the §04 headline "Attributed revenue". The revenue rule is the
    SAME as "Revenue from Call traffic" (:func:`pipeline_revenue_by_month`) and
    the other revenue paths: any positive invoice in the window for a touched
    patient — there is deliberately NO "dated on/after the first paid call"
    gate, so the §04 headline, the per-campaign rows
    (:func:`google_ads_roi`), and the §01 pipeline chart all speak the same
    unit and differ only in POPULATION (paid callers vs all call traffic).
    Correlational, not causal: it credits paid callers who also transacted in
    the window.

    ``booked_calls`` is PMS-reconciled, NOT callscoring-flag based: one row per
    ``Appointments`` row CREATED within ``match_days`` of a genuine connected
    paid call (excludes no-transcript / spam / wrong-number / no-conversation,
    so a coincidental phone match ≠ a booking), credited to the MOST RECENT
    such call, counted as distinct calls — the IDENTICAL rule as
    :func:`google_ads_roi`'s per-campaign ``booked`` and §01's funnel, but over
    ALL paid calls rather than only name-matched ones.

    Returns ``{"paid_calls", "booked_calls", "matched_patients",
    "invoiced_patients", "invoice_count", "revenue", "window_days"}``. Empty
    Invoca list → zeros.
    """
    w = _win(window, days)
    out = {
        "paid_calls": 0, "booked_calls": 0, "matched_patients": 0,
        "invoiced_patients": 0, "invoice_count": 0, "revenue": 0.0,
        "window_days": w.span_days,
    }
    if not invoca_campaign_ids:
        return out
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    join_cs = _callscoring_join_sql()
    not_spam = _non_spam_predicate_sql()
    channel_case = _channel_case_sql()
    sql = f"""
        WITH calls AS (
          SELECT
            call_id, phone_norm, call_ts, genuine,
            {channel_case} AS channel
          FROM (
            SELECT
              t.complete_call_id AS call_id,
              RIGHT(REGEXP_REPLACE(IFNULL(t.calling_phone_number, ''), r'\\D', ''), 10) AS phone_norm,
              SAFE_CAST(t.timestamp AS TIMESTAMP) AS call_ts,
              t.gclid, t.wbraid, t.gbraid, t.msclkid, t.fbclid,
              LOWER(t.utm_medium) AS um, LOWER(t.utm_source) AS us, t.marketing_channel,
              (cs.complete_call_id IS NOT NULL
               AND NOT IFNULL(cs.empty_transcript, FALSE)
               AND NOT IFNULL(cs.wrong_number, FALSE)
               AND NOT IFNULL(cs.no_conversation, FALSE)) AS genuine
            FROM `{_CLINIC_DATA}.transactions` t
            {join_cs}
            WHERE {scope}
              AND {not_spam}
            QUALIFY ROW_NUMBER() OVER (
              PARTITION BY t.complete_call_id ORDER BY t.timestamp DESC) = 1
          )
        ),
        paid AS (SELECT * FROM calls WHERE channel = 'Paid'),
        patients AS (
          SELECT DISTINCT client_id, phone_norm
          FROM `{_PATIENT_CONTACTS}`
          WHERE _clinic_id = @clinic_id AND LENGTH(phone_norm) = 10
        ),
        paid_x_patient AS (
          SELECT pc.call_id, pc.call_ts, pc.genuine, p.client_id
          FROM paid pc
          JOIN patients p
            ON p.phone_norm = pc.phone_norm AND LENGTH(pc.phone_norm) = 10
        ),
        -- Booked: one row per PMS appointment created within match_days of a
        -- genuine connected paid call, credited to the MOST RECENT such call —
        -- the identical rule as google_ads_roi / §01 call_outcomes_funnel, so
        -- this KPI and the per-campaign table speak the same unit.
        booked AS (
          SELECT COUNT(DISTINCT call_id) AS booked_calls FROM (
            SELECT pxp.call_id,
                   ROW_NUMBER() OVER (
                     PARTITION BY a.event_id
                     ORDER BY pxp.call_ts DESC
                   ) AS rn
            FROM paid_x_patient pxp
            JOIN `{_BP}.Appointments` a
              ON a._clinic_id = @clinic_id
              AND a.client_id = pxp.client_id
            WHERE DATE_DIFF(DATE(SAFE_CAST(a.created_time AS TIMESTAMP)),
                            DATE(pxp.call_ts), DAY) BETWEEN 0 AND {int(match_days)}
              AND pxp.genuine
          )
          WHERE rn = 1
        ),
        -- Distinct matched patients (paid callers with a PMS record).
        paid_first_call AS (
          SELECT client_id, MIN(call_ts) AS first_call_ts
          FROM paid_x_patient
          GROUP BY client_id
        ),
        -- Same revenue rule as pipeline_revenue_by_month ("Revenue from Call
        -- traffic"): every positive invoice in the window for a matched
        -- patient, deduped per order_id (MAX amount per order) — no
        -- on/after-first-call gate.
        rev AS (
          SELECT
            COUNT(DISTINCT client_id) AS invoiced_patients,
            COUNT(DISTINCT order_id)  AS invoice_count,
            COALESCE(SUM(amt), 0)     AS revenue
          FROM (
            SELECT im.client_id, im.order_id,
                   MAX(SAFE_CAST(im.order_total_with_tax AS NUMERIC)) AS amt
            FROM `{_BP}.InvoiceMaster` im
            JOIN paid_first_call pfc USING (client_id)
            WHERE im._clinic_id = @clinic_id
              AND SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
              AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)", w)}
            GROUP BY im.client_id, im.order_id
          )
        )
        SELECT
          (SELECT COUNT(*) FROM paid)            AS paid_calls,
          (SELECT booked_calls FROM booked)      AS booked_calls,
          (SELECT COUNT(*) FROM paid_first_call) AS matched_patients,
          rev.invoiced_patients,
          rev.invoice_count,
          rev.revenue
        FROM rev
    """
    try:
        rows = list(_client().query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
        ).result())
    except Exception as exc:
        log.warning("paid_call_revenue query failed for clinic_id=%s: %s", clinic_id, exc)
        return out
    if rows:
        r = rows[0]
        out["paid_calls"]        = int(r.paid_calls or 0)
        out["booked_calls"]      = int(r.booked_calls or 0)
        out["matched_patients"]  = int(r.matched_patients or 0)
        out["invoiced_patients"] = int(r.invoiced_patients or 0)
        out["invoice_count"]     = int(r.invoice_count or 0)
        out["revenue"]           = float(r.revenue or 0.0)
    return out


def paid_call_revenue_group(
    clinic_ids: list[str],
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
    match_days: int = CALL_BOOKING_MATCH_DAYS,
) -> dict[str, Any]:
    """Group-wide :func:`paid_call_revenue` — same metric across N clinics.

    This exists because the per-clinic results CANNOT simply be summed. Two
    distinct problems, and they pull in opposite directions:

    1. ``client_id`` IS SCOPED TO A CLINIC. The same human registered at two
       locations has two different ids, and (worse) two different humans can
       share an id across clinics. So every PMS join here keys on the COMPOSITE
       ``(_clinic_id, client_id)`` — joining on ``client_id`` alone would
       cross-match one clinic's patient to another clinic's appointments and
       invoices. Same rule the existing multi-clinic reader
       (``group_queries.zoolstra_attribution``) follows.

    2. Because of (1), ``COUNT(DISTINCT client_id)`` is meaningless group-wide.
       The only cross-clinic identity we hold is the CALLER'S PHONE — which is
       already what the paid-call matching runs on. So ``matched_patients`` and
       ``invoiced_patients`` count DISTINCT ``phone_norm``: one person who calls
       and is on file at two locations counts ONCE, where summing per-clinic
       results would have counted them twice.

    Consequence worth knowing when reconciling: this reader is <= the sum of the
    per-clinic values, and the gap IS the cross-location overlap. ``revenue`` and
    ``invoice_count`` dedupe on ``(_clinic_id, order_id)`` — order ids are
    clinic-scoped too — so those DO equal the per-clinic sum.

    ``invoca_campaign_ids`` is the UNION across the group's clinics; calls dedupe
    on ``complete_call_id``, which is globally unique, so no call is counted
    twice even when two clinics share a campaign.
    """
    w = _win(window, days)
    out = {
        "paid_calls": 0, "booked_calls": 0, "matched_patients": 0,
        "invoiced_patients": 0, "invoice_count": 0, "revenue": 0.0,
        "window_days": w.span_days,
    }
    if not invoca_campaign_ids or not clinic_ids:
        return out
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    join_cs = _callscoring_join_sql()
    not_spam = _non_spam_predicate_sql()
    channel_case = _channel_case_sql()
    sql = f"""
        WITH calls AS (
          SELECT
            call_id, phone_norm, call_ts, genuine,
            {channel_case} AS channel
          FROM (
            SELECT
              t.complete_call_id AS call_id,
              RIGHT(REGEXP_REPLACE(IFNULL(t.calling_phone_number, ''), r'\\D', ''), 10) AS phone_norm,
              SAFE_CAST(t.timestamp AS TIMESTAMP) AS call_ts,
              t.gclid, t.wbraid, t.gbraid, t.msclkid, t.fbclid,
              LOWER(t.utm_medium) AS um, LOWER(t.utm_source) AS us, t.marketing_channel,
              (cs.complete_call_id IS NOT NULL
               AND NOT IFNULL(cs.empty_transcript, FALSE)
               AND NOT IFNULL(cs.wrong_number, FALSE)
               AND NOT IFNULL(cs.no_conversation, FALSE)) AS genuine
            FROM `{_CLINIC_DATA}.transactions` t
            {join_cs}
            WHERE {scope}
              AND {not_spam}
            QUALIFY ROW_NUMBER() OVER (
              PARTITION BY t.complete_call_id ORDER BY t.timestamp DESC) = 1
          )
        ),
        paid AS (SELECT * FROM calls WHERE channel = 'Paid'),
        -- Patient records carry their OWNING clinic; both travel together from
        -- here on so no join can cross a clinic boundary.
        patients AS (
          SELECT DISTINCT _clinic_id, client_id, phone_norm
          FROM `{_PATIENT_CONTACTS}`
          WHERE _clinic_id IN UNNEST(@clinic_ids) AND LENGTH(phone_norm) = 10
        ),
        paid_x_patient AS (
          SELECT pc.call_id, pc.call_ts, pc.genuine, pc.phone_norm,
                 p._clinic_id, p.client_id
          FROM paid pc
          JOIN patients p
            ON p.phone_norm = pc.phone_norm AND LENGTH(pc.phone_norm) = 10
        ),
        booked AS (
          SELECT COUNT(DISTINCT call_id) AS booked_calls FROM (
            SELECT pxp.call_id,
                   ROW_NUMBER() OVER (
                     PARTITION BY a._clinic_id, a.event_id
                     ORDER BY pxp.call_ts DESC
                   ) AS rn
            FROM paid_x_patient pxp
            JOIN `{_BP}.Appointments` a
              ON a._clinic_id = pxp._clinic_id
              AND a.client_id = pxp.client_id
            WHERE DATE_DIFF(DATE(SAFE_CAST(a.created_time AS TIMESTAMP)),
                            DATE(pxp.call_ts), DAY) BETWEEN 0 AND {int(match_days)}
              AND pxp.genuine
          )
          WHERE rn = 1
        ),
        -- Keyed by phone, not client_id: the group-wide unit of "a person".
        matched AS (
          SELECT COUNT(DISTINCT phone_norm) AS matched_patients
          FROM paid_x_patient
        ),
        rev AS (
          SELECT
            COUNT(DISTINCT phone_norm) AS invoiced_patients,
            COUNT(DISTINCT order_key)  AS invoice_count,
            COALESCE(SUM(amt), 0)      AS revenue
          FROM (
            SELECT
              pxp.phone_norm,
              CONCAT(im._clinic_id, ':', im.order_id) AS order_key,
              MAX(SAFE_CAST(im.order_total_with_tax AS NUMERIC)) AS amt
            FROM `{_BP}.InvoiceMaster` im
            JOIN (SELECT DISTINCT _clinic_id, client_id, phone_norm
                  FROM paid_x_patient) pxp
              ON im._clinic_id = pxp._clinic_id
             AND im.client_id  = pxp.client_id
            WHERE SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
              AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)", w)}
            GROUP BY pxp.phone_norm, order_key
          )
        )
        SELECT
          (SELECT COUNT(*) FROM paid)                 AS paid_calls,
          (SELECT booked_calls FROM booked)           AS booked_calls,
          (SELECT matched_patients FROM matched)      AS matched_patients,
          rev.invoiced_patients,
          rev.invoice_count,
          rev.revenue
        FROM rev
    """
    try:
        rows = list(_client().query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ArrayQueryParameter("clinic_ids", "STRING", list(clinic_ids)),
            ]),
        ).result())
    except Exception as exc:
        log.warning("paid_call_revenue_group query failed for %d clinics: %s",
                    len(clinic_ids), exc)
        return out
    if rows:
        r = rows[0]
        out["paid_calls"]        = int(r.paid_calls or 0)
        out["booked_calls"]      = int(r.booked_calls or 0)
        out["matched_patients"]  = int(r.matched_patients or 0)
        out["invoiced_patients"] = int(r.invoiced_patients or 0)
        out["invoice_count"]     = int(r.invoice_count or 0)
        out["revenue"]           = float(r.revenue or 0.0)
    return out


# ── Revenue funnel: calls → patient → appointment → invoice ──────────────────

# Status labels coming out of Blueprint's `status_2` column. The numeric status
# codes (e.g. 7=Completed) get resolved to these strings during the ETL load.
_STATUS_COMPLETED  = ("Completed", "Arrived")
_STATUS_CANCELLED  = ("Cancelled",)
_STATUS_NO_SHOW    = ("No show",)
_STATUS_FUTURE     = ("Tentative", "Confirmed", "Ready", "In progress")


def revenue_funnel(
    clinic_id: str,
    ga_campaign_ids: list[str],
    invoca_campaign_ids: list[str],
    days: int = 90,
    booking_window_hours: int = 24,
    utm_sources: list[str] | None = None,
    utm_mediums: list[str] | None = None,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """End-to-end funnel from inbound calls through Blueprint invoices.

    Spam is filtered upstream — heuristic-spam calls (matches §02's predicate:
    autodialer line-check, masked caller ID, toll-free originator, high-volume
    number) are excluded from every downstream stage. The spam count is
    returned separately as ``spam`` for the report to display as an upstream-
    filter callout.

    Stage flags (answered / discussed / booked) come from
    ``ClinicData.callscoring`` — the LLM-scored per-call booleans
    (``no_conversation``, ``appointment_booked``, ``qualified_lead_no_conversion``).
    Among non-spam calls, two mutually-exclusive first-stage buckets sum to the
    total: ``voicemail_hangup`` (no callscoring row OR ``no_conversation = TRUE``
    — both mean no real conversation was captured) and ``answered`` (= Connected).

    Patient-side stages (``matched_patient``, ``appt_within_window``,
    ``invoiced``, ``matched_revenue``) join call ``calling_phone_number`` to
    ``ClientDemographics`` (any of the three phone slots), then chase
    ``client_id`` into ``Appointments`` (created within ``booking_window_hours``
    of the call) and into ``InvoiceMaster`` (any invoice dated on or after the
    call). Revenue is summed once per distinct invoiced patient regardless of
    how many calls matched it.
    """
    w = _win(window, days)
    out: dict[str, Any] = {
        "clicks": 0, "calls": 0,
        "spam": 0, "voicemail_hangup": 0,
        "answered": 0, "discussed": 0, "booked": 0,
        "matched_patient": 0, "appt_within_window": 0, "appt_completed": 0,
        "invoiced": 0, "matched_revenue": 0.0,
        "window_days": w.span_days, "booking_window_hours": booking_window_hours,
    }
    client = _client()

    # Clicks live in ClinicData; no patient join needed.
    if ga_campaign_ids:
        in_ga = "(" + ", ".join(f"'{c}'" for c in ga_campaign_ids) + ")"
        rows = list(client.query(f"""
            SELECT COUNT(*) AS n
            FROM `{_CLINIC_DATA}.ad_clicks_v2`
            WHERE google_ads_campaign_id IN {in_ga}
              AND {_ts_between("timestamp", w)}
        """).result())
        out["clicks"] = int(rows[0].n or 0)

    if not invoca_campaign_ids:
        return out

    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    hrs = int(booking_window_hours)
    utm_sql = _utm_filter_sql(utm_sources, utm_mediums)
    sql = f"""
        WITH calls AS (
            -- Per-call bucket derivation. Spam = callscoring.spam_or_solicitor
            -- (LLM judgment, NULL → non-spam). After spam exclusion, two
            -- mutually-exclusive first-stage buckets remain (sum to non-spam
            -- total): voicemail (no callscoring row OR no_conversation = TRUE)
            -- and answered (= Connected). Discussed / Booked are subsets of
            -- answered, driven by callscoring's appointment_booked and
            -- qualified_lead_no_conversion flags.
            SELECT
              t.transaction_id,
              RIGHT(REGEXP_REPLACE(IFNULL(t.calling_phone_number, ''), r'\\D', ''), 10) AS phone_norm,
              SAFE_CAST(t.timestamp AS TIMESTAMP)            AS call_ts,
              IFNULL(cs.spam_or_solicitor, FALSE)            AS is_spam,
              (cs.complete_call_id IS NOT NULL)              AS has_cs,
              IFNULL(cs.no_conversation, FALSE)              AS is_no_conv,
              IFNULL(cs.appointment_booked, FALSE)           AS cs_booked,
              IFNULL(cs.qualified_lead_no_conversion, FALSE) AS cs_qlnc
            FROM `{_CLINIC_DATA}.transactions` t
            LEFT JOIN `{_CLINIC_DATA}.callscoring` cs
              ON cs.complete_call_id = t.complete_call_id
            WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
              AND {_ts_between("t.timestamp", w)}
              {utm_sql}
        ),
        spam_summary AS (
            -- Spam count is reported separately as an "upstream filter" stat;
            -- spam rows do not appear in any downstream funnel stage.
            SELECT COUNTIF(is_spam) AS spam_calls FROM calls
        ),
        call_predicates AS (
            -- Spam is excluded entirely. Two mutually-exclusive first-stage
            -- buckets remain (sum to non-spam total): voicemail (no transcript
            -- to score, or callscoring flagged no_conversation) and answered
            -- (= Connected). Unscored calls roll into voicemail because the
            -- absence of a transcript means no real conversation was captured.
            SELECT
              transaction_id,
              phone_norm,
              call_ts,
              (NOT has_cs OR (has_cs AND is_no_conv)) AS is_voicemail,
              (has_cs AND NOT is_no_conv)             AS answered,
              (has_cs AND NOT is_no_conv
               AND (cs_booked OR cs_qlnc))            AS discussed,
              (has_cs AND NOT is_no_conv
               AND cs_booked)                         AS booked
            FROM calls
            WHERE NOT is_spam
        ),
        patients AS (
            SELECT DISTINCT client_id, phone_norm
            FROM `{_PATIENT_CONTACTS}`
            WHERE _clinic_id = @clinic_id
              AND LENGTH(phone_norm) = 10
        ),
        call_x_patient AS (
            SELECT c.*, p.client_id
            FROM call_predicates c
            LEFT JOIN patients p
              ON p.phone_norm = c.phone_norm
              AND LENGTH(c.phone_norm) = 10
        ),
        call_appt AS (
            SELECT
              cxp.transaction_id,
              LOGICAL_OR(a.event_id IS NOT NULL)                    AS has_appt,
              LOGICAL_OR(a.status_2 IN UNNEST({list(_STATUS_COMPLETED)})) AS has_appt_completed
            FROM call_x_patient cxp
            LEFT JOIN `{_BP}.Appointments` a
              ON a._clinic_id = @clinic_id
              AND a.client_id = cxp.client_id
              AND SAFE_CAST(a.created_time AS TIMESTAMP) >= cxp.call_ts
              AND SAFE_CAST(a.created_time AS TIMESTAMP)
                  <= TIMESTAMP_ADD(cxp.call_ts, INTERVAL {hrs} HOUR)
            GROUP BY cxp.transaction_id
        ),
        call_inv AS (
            SELECT
              cxp.transaction_id,
              LOGICAL_OR(im.order_id IS NOT NULL) AS has_invoice
            FROM call_x_patient cxp
            LEFT JOIN `{_BP}.InvoiceMaster` im
              ON im._clinic_id = @clinic_id
              AND im.client_id = cxp.client_id
              AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date) >= DATE(cxp.call_ts)
              AND SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
            GROUP BY cxp.transaction_id
        ),
        call_summary AS (
            SELECT
              cxp.transaction_id,
              ANY_VALUE(cxp.is_voicemail) AS is_voicemail,
              ANY_VALUE(cxp.answered)     AS answered,
              ANY_VALUE(cxp.discussed)    AS discussed,
              ANY_VALUE(cxp.booked)       AS booked,
              LOGICAL_OR(cxp.client_id IS NOT NULL) AS matched_patient
            FROM call_x_patient cxp
            GROUP BY cxp.transaction_id
        ),
        -- Per-call combined state. is_voicemail and answered are mutually
        -- exclusive and exhaustive at the first stage of the (non-spam) funnel;
        -- discussed and booked are subsets of answered.
        call_state AS (
            SELECT
              cs.transaction_id,
              cs.is_voicemail,
              cs.answered,
              cs.discussed,
              cs.booked,
              cs.matched_patient,
              IFNULL(ca.has_appt,           FALSE) AS has_appt,
              IFNULL(ca.has_appt_completed, FALSE) AS has_appt_completed,
              IFNULL(ci.has_invoice,        FALSE) AS has_invoice
            FROM call_summary cs
            LEFT JOIN call_appt ca USING (transaction_id)
            LEFT JOIN call_inv  ci USING (transaction_id)
        ),
        -- Revenue: distinct patients matched by any tracked call (booked or
        -- not — match Virsono's loose attribution). Each patient's invoices
        -- contribute once regardless of how many calls hit them. Invoices
        -- before the patient's first tracked call are excluded — they can't
        -- be marketing-attributed.
        patient_first_call AS (
            SELECT client_id, MIN(call_ts) AS first_call_ts
            FROM call_x_patient
            WHERE client_id IS NOT NULL
            GROUP BY client_id
        ),
        matched_invoice_revenue AS (
            SELECT COALESCE(SUM(per_patient.revenue), 0) AS revenue
            FROM (
                SELECT im.client_id,
                       SUM(SAFE_CAST(im.order_total_with_tax AS NUMERIC)) AS revenue
                FROM `{_BP}.InvoiceMaster` im
                JOIN patient_first_call pfc USING (client_id)
                WHERE im._clinic_id = @clinic_id
                  AND SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
                  AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)", w)}
                  AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)
                      >= DATE(pfc.first_call_ts)
                GROUP BY im.client_id
            ) per_patient
        )
        SELECT
          -- Funnel operates on non-spam calls only. `calls` = non-spam total;
          -- `spam` is reported separately as the upstream filter count.
          COUNT(*)                                               AS calls,
          (SELECT spam_calls FROM spam_summary)                  AS spam,
          -- Mutually exclusive first-stage buckets — sum to `calls` (non-spam).
          COUNTIF(is_voicemail)                                  AS voicemail_hangup,
          COUNTIF(answered)                                      AS answered,
          COUNTIF(discussed)                                     AS discussed,
          COUNTIF(booked)                                        AS booked,
          -- matched_patient / appt / invoiced are NOT gated on booked so a
          -- non-booked call that turns into a real visit still counts
          -- (matches virsono_report/metrics.py::funnel_stages).
          COUNTIF(matched_patient)                               AS matched_patient,
          COUNTIF(has_appt)                                      AS appt_within_window,
          COUNTIF(has_appt_completed)                            AS appt_completed,
          COUNTIF(has_invoice)                                   AS invoiced,
          (SELECT revenue FROM matched_invoice_revenue)          AS matched_revenue
        FROM call_state
    """
    rows = list(client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=_params(clinic_id) + _utm_params(utm_sources, utm_mediums),
        ),
    ).result())
    if rows:
        r = rows[0]
        out.update({
            "calls":              int(r.calls or 0),
            "spam":               int(r.spam or 0),
            "voicemail_hangup":   int(r.voicemail_hangup or 0),
            "answered":           int(r.answered or 0),
            "discussed":          int(r.discussed or 0),
            "booked":             int(r.booked or 0),
            "matched_patient":    int(r.matched_patient or 0),
            "appt_within_window": int(r.appt_within_window or 0),
            "appt_completed":     int(r.appt_completed or 0),
            "invoiced":           int(r.invoiced or 0),
            "matched_revenue":    float(r.matched_revenue or 0),
        })
    return out


# ── Attributed invoice detail (per-row drill-down) ───────────────────────────

def attributed_invoice_detail(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 365,
    booking_window_hours: int = 24,
    limit: int | None = None,
    window: "Window | None" = None,
) -> list[dict]:
    """All revenue from patients matched to a tracked phone call (Virsono-loose).

    Patient-centric attribution mirroring §02 Revenue funnel's methodology:
    every patient whose phone matched a tracked Invoca call gets credited
    with all their invoices in the window — booked or not, appt-within-24h
    or not. This is the same loose definition Virsono uses in
    ``metrics.py::funnel_stages`` for the ``invoiced`` stage.

    For each such patient we show:
      • acquisition touch  — the first tracked call (earliest by call_ts)
      • first appt in 24h  — if it exists (LEFT JOIN — blank when missing)
      • every invoice      — all invoices in window for this patient

    Returns one row per (patient × invoice). A patient with three invoices
    appears three times sharing the same acquisition UTM, first call, and
    first-appt cell.
    """
    if not invoca_campaign_ids:
        return []

    w = _win(window, days)
    client = _client()
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    hrs = int(booking_window_hours)
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    sql = f"""
        WITH calls AS (
            SELECT
              transaction_id,
              complete_call_id,
              RIGHT(REGEXP_REPLACE(IFNULL(calling_phone_number, ''), r'\\D', ''), 10) AS phone_norm,
              calling_phone_number,
              SAFE_CAST(timestamp AS TIMESTAMP) AS call_ts,
              utm_source, utm_medium, marketing_channel
            FROM `{_CLINIC_DATA}.transactions`
            WHERE CAST(invoca_campaign_id AS STRING) IN {in_iv}
              AND {_ts_between("timestamp", w)}
        ),
        patients AS (
            SELECT DISTINCT
              cd.client_id,
              cd.given_name,
              cd.surname,
              cd.status AS patient_status,
              RIGHT(REGEXP_REPLACE(phone, r'\\D', ''), 10) AS phone_norm
            FROM `{_BP}.ClientDemographics` cd,
            UNNEST([cd.home_telephone_no, cd.work_telephone_no, cd.mobile_telephone_no]) AS phone
            WHERE cd._clinic_id = @clinic_id
              AND phone IS NOT NULL
              AND LENGTH(RIGHT(REGEXP_REPLACE(phone, r'\\D', ''), 10)) = 10
        ),
        call_x_patient AS (
            SELECT c.*, p.client_id, p.given_name, p.surname, p.patient_status
            FROM calls c
            INNER JOIN patients p
              ON p.phone_norm = c.phone_norm AND LENGTH(c.phone_norm) = 10
        ),
        -- First tracked call per patient (the marketing acquisition touch).
        first_call AS (
            SELECT * EXCEPT(rn) FROM (
                SELECT
                  cxp.*,
                  ROW_NUMBER() OVER (
                      PARTITION BY cxp.client_id
                      ORDER BY cxp.call_ts ASC
                  ) AS rn
                FROM call_x_patient cxp
            )
            WHERE rn = 1
        ),
        -- First appt booked within +N hours of the first call (optional —
        -- left-joined so patients without an in-window appt still surface).
        first_appt AS (
            SELECT * EXCEPT(rn) FROM (
                SELECT
                  fc.client_id,
                  a.event_id                         AS appt_event_id,
                  a.event_type                       AS appt_event_type,
                  a.start_time                       AS appt_start_time,
                  a.status_2                         AS appt_status,
                  a.title                            AS appt_title,
                  ROW_NUMBER() OVER (
                      PARTITION BY fc.client_id
                      ORDER BY SAFE_CAST(a.created_time AS TIMESTAMP) ASC
                  ) AS rn
                FROM first_call fc
                JOIN `{_BP}.Appointments` a
                  ON a._clinic_id = @clinic_id
                 AND a.client_id   = fc.client_id
                 AND SAFE_CAST(a.created_time AS TIMESTAMP) >= fc.call_ts
                 AND SAFE_CAST(a.created_time AS TIMESTAMP)
                     <= TIMESTAMP_ADD(fc.call_ts, INTERVAL {hrs} HOUR)
            )
            WHERE rn = 1
        )
        SELECT
          fc.complete_call_id                                  AS first_call_id,
          fc.call_ts                                           AS first_call_ts,
          fc.calling_phone_number,
          fc.utm_source,
          fc.utm_medium,
          fc.marketing_channel,
          fc.client_id,
          fc.given_name,
          fc.surname,
          fc.patient_status,
          fa.appt_event_id,
          fa.appt_event_type,
          fa.appt_start_time,
          fa.appt_status,
          fa.appt_title,
          im.order_id                                          AS invoice_order_id,
          im.invoice_number,
          SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)         AS invoice_date,
          SAFE_CAST(im.order_total_with_tax AS NUMERIC)        AS order_total
        FROM first_call fc
        LEFT JOIN first_appt fa USING (client_id)
        JOIN `{_BP}.InvoiceMaster` im
          ON im._clinic_id = @clinic_id
         AND im.client_id  = fc.client_id
        WHERE SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
          AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)", w)}
          -- Only invoices on or after the patient's first tracked call:
          -- pre-call invoices can't be marketing-attributed.
          AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date) >= DATE(fc.call_ts)
        ORDER BY fc.call_ts DESC, fc.client_id, im.invoice_date ASC
        {limit_clause}
    """
    rows = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
    ).result()
    out: list[dict] = []
    for r in rows:
        out.append({
            "first_call_id":        r.first_call_id or "",
            "first_call_ts":        r.first_call_ts,
            "calling_phone_number": r.calling_phone_number,
            "utm_source":           r.utm_source,
            "utm_medium":           r.utm_medium,
            "marketing_channel":    r.marketing_channel,
            "client_id":            r.client_id,
            "given_name":           r.given_name,
            "surname":              r.surname,
            "patient_status":       r.patient_status,
            "appt_event_id":        r.appt_event_id,
            "appt_event_type":      r.appt_event_type,
            "appt_start_time":      r.appt_start_time,
            "appt_status":          r.appt_status,
            "appt_title":           r.appt_title,
            "invoice_order_id":     r.invoice_order_id,
            "invoice_number":       r.invoice_number,
            "invoice_date":         r.invoice_date,
            "order_total":          float(r.order_total or 0),
        })
    return out


def attributed_invoice_count(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 365,
    booking_window_hours: int = 24,
    window: "Window | None" = None,
) -> int:
    """Total count of (patient × invoice) rows the attributed-invoices query
    would return — used by the cohort banner without materialising every row.
    """
    if not invoca_campaign_ids:
        return 0
    w = _win(window, days)
    client = _client()
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    hrs = int(booking_window_hours)
    # Mirror the same JOIN chain as attributed_invoice_detail but project only
    # the count. first_appt isn't needed for the count itself but is left in
    # so behaviour matches the detail query exactly.
    sql = f"""
        WITH calls AS (
            SELECT
              RIGHT(REGEXP_REPLACE(IFNULL(calling_phone_number, ''), r'\\D', ''), 10) AS phone_norm,
              SAFE_CAST(timestamp AS TIMESTAMP) AS call_ts
            FROM `{_CLINIC_DATA}.transactions`
            WHERE CAST(invoca_campaign_id AS STRING) IN {in_iv}
              AND {_ts_between("timestamp", w)}
        ),
        patients AS (
            SELECT DISTINCT
              cd.client_id,
              RIGHT(REGEXP_REPLACE(phone, r'\\D', ''), 10) AS phone_norm
            FROM `{_BP}.ClientDemographics` cd,
            UNNEST([cd.home_telephone_no, cd.work_telephone_no, cd.mobile_telephone_no]) AS phone
            WHERE cd._clinic_id = @clinic_id
              AND phone IS NOT NULL
              AND LENGTH(RIGHT(REGEXP_REPLACE(phone, r'\\D', ''), 10)) = 10
        ),
        first_call AS (
            SELECT client_id, MIN(call_ts) AS call_ts
            FROM (
                SELECT p.client_id, c.call_ts
                FROM calls c
                INNER JOIN patients p
                  ON p.phone_norm = c.phone_norm AND LENGTH(c.phone_norm) = 10
            )
            GROUP BY client_id
        )
        SELECT COUNT(*) AS n
        FROM first_call fc
        JOIN `{_BP}.InvoiceMaster` im
          ON im._clinic_id = @clinic_id
         AND im.client_id  = fc.client_id
        WHERE SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
          AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)", w)}
          AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date) >= DATE(fc.call_ts)
    """
    rows = list(client.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
    ).result())
    return int(rows[0].n or 0) if rows else 0


# ── Acquisition · Drivers of call traffic ────────────────────────────────────
#
# The patient-acquisition page (see patient_acquisition_data_model.md memo)
# treats inbound calls as the engagement surface and Google Ads clicks as the
# acquisition surface. These three readers feed the "Drivers of call traffic"
# block: how many calls came in, what share were driven by an ad click, and —
# for the ad-driven slice — what regions / keywords show up.
#
# Scoping: calls are filtered by the clinic's linked Invoca campaigns; clicks
# are filtered by the clinic's linked Google Ads campaigns. We use the
# JOIN ON click_view_gclid = transactions.gclid to attribute regions / keywords
# back to actual calls (not just raw clicks).


def acquisition_call_traffic(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> dict[str, int]:
    """Non-spam calls + paid-search calls (gclid present) for the clinic's Invoca
    campaigns within the rolling window. Spam/solicitor calls are excluded from
    BOTH the count and the total so the paid-search share is of genuine inbound
    calls (matching how "calls" is counted elsewhere).

    Returns ``{"total_calls", "ad_driven_calls", "ad_driven_pct"}``. Empty
    Invoca list → all zeros (the clinic isn't tracking calls).
    """
    if not invoca_campaign_ids:
        return {"total_calls": 0, "ad_driven_calls": 0, "ad_driven_pct": 0.0}

    w = _win(window, days)
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    client = _client()
    rows = list(client.query(f"""
        SELECT
          COUNTIF(NOT is_spam)                                  AS total_calls,
          COUNTIF(NOT is_spam AND has_gclid)                    AS ad_driven_calls
        FROM (
          SELECT
            IFNULL(cs.spam_or_solicitor, FALSE)                 AS is_spam,
            (t.gclid IS NOT NULL AND t.gclid != '')             AS has_gclid
          FROM `{_CLINIC_DATA}.transactions` t
          LEFT JOIN `{_CLINIC_DATA}.callscoring` cs
            ON cs.complete_call_id = t.complete_call_id
          WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
            AND {_ts_between("t.timestamp", w)}
        )
    """).result())
    r = rows[0]
    total = int(r.total_calls or 0)
    ad = int(r.ad_driven_calls or 0)
    pct = (100.0 * ad / total) if total else 0.0
    return {"total_calls": total, "ad_driven_calls": ad, "ad_driven_pct": pct}


def top_calling_regions(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    ga_campaign_ids: list[str],
    days: int = 90,
    top_n: int = 10,
    window: "Window | None" = None,
) -> list[dict]:
    """Top regions for ad-driven calls, sourced from
    ``ad_clicks_v2.click_view_area_of_interest_region`` via the gclid join.

    Region values come in as ``geoTargetConstants/<criterion_id>``. We extract
    the numeric ID and LEFT JOIN against ``ClinicData.geo_targets`` (populated
    by :mod:`intelligence_report.load_geo_targets`) to resolve a human-readable
    canonical name. Unresolved IDs fall back to the raw resource string; NULL,
    empty, and the string ``'nan'`` all collapse into ``'(unspecified)'``.

    Returns ``[{"region": str, "calls": int}, ...]`` sorted by call count desc.
    Empty when either campaign list is empty (no scope to join).
    """
    if not invoca_campaign_ids or not ga_campaign_ids:
        return []
    w = _win(window, days)
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    in_ga = "(" + ", ".join(f"'{c}'" for c in ga_campaign_ids) + ")"
    client = _client()
    sql = f"""
        WITH joined AS (
          SELECT
            ac.click_view_area_of_interest_region                       AS raw_region,
            SAFE_CAST(REGEXP_EXTRACT(ac.click_view_area_of_interest_region,
                                     r'geoTargetConstants/(\\d+)') AS INT64) AS criterion_id
          FROM `{_CLINIC_DATA}.transactions` t
          INNER JOIN `{_CLINIC_DATA}.ad_clicks_v2` ac
            ON ac.click_view_gclid = t.gclid
           AND ac.google_ads_campaign_id IN {in_ga}
          WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
            AND t.gclid IS NOT NULL AND t.gclid != ''
            AND {_ts_between("t.timestamp", w)}
        )
        SELECT
          CASE
            WHEN gt.canonical_name IS NOT NULL AND gt.canonical_name != '' THEN gt.canonical_name
            WHEN j.raw_region IS NULL OR TRIM(j.raw_region) IN ('', 'nan')  THEN '(unspecified)'
            ELSE j.raw_region
          END                       AS region,
          COUNT(*)                  AS calls
        FROM joined j
        LEFT JOIN `{_CLINIC_DATA}.geo_targets` gt
          ON gt.criterion_id = j.criterion_id
        GROUP BY region
        ORDER BY calls DESC
        LIMIT {int(top_n)}
    """
    return [{"region": r.region, "calls": int(r.calls or 0)}
            for r in client.query(sql).result()]


def top_keywords(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    ga_campaign_ids: list[str],
    days: int = 90,
    top_n: int = 10,
    window: "Window | None" = None,
) -> list[dict]:
    """Top keywords for ad-driven calls, sourced from
    ``ad_clicks_v2.click_view_keyword_info_text`` via the gclid join.

    NULL, empty, and the string ``'nan'`` all collapse into ``'(no keyword)'``.

    Returns ``[{"keyword": str, "calls": int}, ...]`` sorted by call count
    desc. Empty when either campaign list is empty.
    """
    if not invoca_campaign_ids or not ga_campaign_ids:
        return []
    w = _win(window, days)
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    in_ga = "(" + ", ".join(f"'{c}'" for c in ga_campaign_ids) + ")"
    client = _client()
    sql = f"""
        SELECT
          CASE
            WHEN ac.click_view_keyword_info_text IS NULL
              OR TRIM(ac.click_view_keyword_info_text) IN ('', 'nan')
              THEN '(no keyword)'
            ELSE TRIM(ac.click_view_keyword_info_text)
          END                       AS keyword,
          COUNT(*)                  AS calls
        FROM `{_CLINIC_DATA}.transactions` t
        INNER JOIN `{_CLINIC_DATA}.ad_clicks_v2` ac
          ON ac.click_view_gclid = t.gclid
         AND ac.google_ads_campaign_id IN {in_ga}
        WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
          AND t.gclid IS NOT NULL AND t.gclid != ''
          AND {_ts_between("t.timestamp", w)}
        GROUP BY keyword
        ORDER BY calls DESC
        LIMIT {int(top_n)}
    """
    return [{"keyword": r.keyword, "calls": int(r.calls or 0)}
            for r in client.query(sql).result()]


# ── Engagement · spam-call filtering ─────────────────────────────────────────
#
# Spam classification is LLM-driven: ClinicData.callscoring.spam_or_solicitor
# is the sole source of truth. Calls without a callscoring row are treated as
# non-spam (innocent until proven guilty) so the spam filter never produces
# false positives from missing data.
#
# Centralised here so every section that filters spam shares the same join
# pattern and predicate.


def _spam_scope_clause(invoca_campaign_ids: list[str], days: int, t_alias: str = "t", window: "Window | None" = None) -> str:
    """Common WHERE clause — scopes a transactions row to the clinic's Invoca
    campaigns within the window. Injected verbatim into the SQL strings below;
    callers must guarantee ``invoca_campaign_ids`` is non-empty.
    """
    w = _win(window, days)
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    return (
        f"CAST({t_alias}.invoca_campaign_id AS STRING) IN {in_iv} "
        f"AND {_ts_between(f'{t_alias}.timestamp', w)}"
    )


def _callscoring_join_sql(t_alias: str = "t", cs_alias: str = "cs") -> str:
    """LEFT JOIN clause that exposes ``<cs_alias>.spam_or_solicitor`` and the
    rest of the callscoring flags for a transactions row aliased ``<t_alias>``.
    Calls without a stored transcript leave every cs.* column NULL.
    """
    return (
        f"LEFT JOIN `{_CLINIC_DATA}.callscoring` {cs_alias}\n"
        f"  ON {cs_alias}.complete_call_id = {t_alias}.complete_call_id"
    )


def _is_spam_sql(cs_alias: str = "cs") -> str:
    """Predicate that is TRUE for spam-classified transactions rows.

    ``cs.spam_or_solicitor = TRUE`` from the LLM. NULL (no callscoring row) is
    treated as non-spam — the funnel never penalises a call for missing data.
    """
    return f"IFNULL({cs_alias}.spam_or_solicitor, FALSE)"


def _non_spam_predicate_sql(cs_alias: str = "cs", **_legacy) -> str:
    """Predicate that is TRUE for non-spam transactions rows.

    Inverse of :func:`_is_spam_sql`. ``**_legacy`` accepts (and ignores) the
    old ``high_vol_cte`` keyword so any straggling callers don't break during
    the heuristic→callscoring transition.
    """
    return f"NOT {_is_spam_sql(cs_alias)}"


def spam_calls_summary(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """Total + spam-classified call counts for the window.

    Returns ``{"total_calls", "spam_calls", "spam_pct"}``. Empty Invoca list →
    all zeros.
    """
    if not invoca_campaign_ids:
        return {"total_calls": 0, "spam_calls": 0, "spam_pct": 0.0}
    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    join_cs = _callscoring_join_sql()
    is_spam = _is_spam_sql()
    sql = f"""
        SELECT
          COUNT(*)        AS total_calls,
          COUNTIF({is_spam}) AS spam_calls
        FROM `{_CLINIC_DATA}.transactions` t
        {join_cs}
        WHERE {scope}
    """
    rows = list(_client().query(sql).result())
    r = rows[0]
    total = int(r.total_calls or 0)
    spam = int(r.spam_calls or 0)
    pct = (100.0 * spam / total) if total else 0.0
    return {"total_calls": total, "spam_calls": spam, "spam_pct": pct}


def spam_calls_detail(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> list[dict]:
    """Per-call rows for every call the LLM classified as spam in the window.

    Returns ``[{"complete_call_id", "start_time_local", "calling_phone_number",
    "duration", "connect_duration", "spam_reason"}, ...]`` newest first. Empty
    Invoca list → empty list. ``complete_call_id`` is the key the renderer
    uses to fetch transcripts from GCS. ``spam_reason`` is the LLM's
    ``reasoning`` field — the one-sentence justification it produced when it
    set ``spam_or_solicitor = TRUE``.
    """
    if not invoca_campaign_ids:
        return []
    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    join_cs = _callscoring_join_sql()
    sql = f"""
        SELECT
          t.complete_call_id,
          t.start_time_local,
          t.calling_phone_number,
          t.duration,
          t.connect_duration,
          cs.reasoning AS spam_reason
        FROM `{_CLINIC_DATA}.transactions` t
        {join_cs}
        WHERE {scope}
          AND cs.spam_or_solicitor = TRUE
        ORDER BY t.timestamp DESC
    """
    out: list[dict] = []
    for r in _client().query(sql).result():
        out.append({
            "complete_call_id":    r.complete_call_id or "",
            "start_time_local":    r.start_time_local,
            "calling_phone_number": r.calling_phone_number,
            "duration":            int(r.duration or 0),
            "connect_duration":    int(r.connect_duration or 0),
            "spam_reason":         r.spam_reason or "LLM-classified spam",
        })
    return out


# ── End-to-end pipeline · Stage 1 · ad clicks → inbound calls by UTM medium ──
#
# Spam is filtered out for the whole pipeline. Per the spec, NULL / empty /
# 'untagged' / 'none' / 'na' (and the ETL's stringified 'nan') all collapse to
# a single 'untagged' bucket. Compared case-insensitively after TRIM.


def stage1_utm_breakdown(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> list[dict]:
    """Calls bucketed by ``utm_medium`` (spam filtered).

    Returns ``[{"medium": str, "calls": int, "pct": float}, ...]`` sorted by
    call count desc. ``pct`` is share of all non-spam calls in the window.
    """
    if not invoca_campaign_ids:
        return []
    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    join_cs = _callscoring_join_sql()
    not_spam = _non_spam_predicate_sql()
    sql = f"""
        WITH non_spam AS (
          SELECT
            CASE
              WHEN t.utm_medium IS NULL
                OR LOWER(TRIM(t.utm_medium)) IN ('', 'untagged', 'none', 'na', 'nan')
                THEN 'untagged'
              ELSE LOWER(TRIM(t.utm_medium))
            END AS medium
          FROM `{_CLINIC_DATA}.transactions` t
          {join_cs}
          WHERE {scope}
            AND {not_spam}
        )
        SELECT
          medium,
          COUNT(*)                                                   AS calls,
          ROUND(100 * COUNT(*) / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) AS pct
        FROM non_spam
        GROUP BY medium
        ORDER BY calls DESC
    """
    return [
        {"medium": r.medium, "calls": int(r.calls or 0), "pct": float(r.pct or 0)}
        for r in _client().query(sql).result()
    ]


# ── End-to-end pipeline · Stage 2 · call outcomes (callscoring-backed) ───────
#
# Outcomes per spec:
#   Appointment Booked          → callscoring.appointment_booked
#   No Conversation             → callscoring.no_conversation (funnel ends)
#   Qualified Lead - No Conv    → callscoring.qualified_lead_no_conversion
#   (Out of scope)              → existing customer / wrong number / unscored
#
# Bucketing precedence handles non-exclusive flags: a booked call shows under
# Appointment Booked even if existing_customer is also flagged.


def _outcome_case_sql(prefix: str = "cs.") -> str:
    """Single CASE that buckets a callscoring row into the funnel's outcome.
    `prefix` is the SQL alias used for the callscoring table.
    """
    return f"""
        CASE
          WHEN {prefix}appointment_booked                       THEN 'Appointment Booked'
          WHEN {prefix}no_conversation                          THEN 'No Conversation'
          WHEN {prefix}qualified_lead_no_conversion             THEN 'Qualified Lead - No Conversion'
          WHEN {prefix}existing_customer OR {prefix}wrong_number THEN 'Out of scope'
          ELSE 'Other'
        END
    """


OUTCOME_LABELS = (
    "Appointment Booked",
    "No Conversation",
    "Qualified Lead - No Conversion",
    "Out of scope",
    "Other",
)


def stage2_outcome_breakdown(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> list[dict]:
    """Non-spam, scored calls bucketed by outcome.

    Returns ``[{"outcome": str, "calls": int}, ...]`` in the canonical order
    above. Buckets with zero calls are omitted from the result.
    """
    if not invoca_campaign_ids:
        return []
    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    not_spam = _non_spam_predicate_sql()
    outcome_case = _outcome_case_sql("cs.")
    sql = f"""
        SELECT
          {outcome_case} AS outcome,
          COUNT(*)       AS calls
        FROM `{_CLINIC_DATA}.transactions` t
        JOIN `{_CLINIC_DATA}.callscoring` cs ON cs.complete_call_id = t.complete_call_id
        WHERE {scope}
          AND {not_spam}
        GROUP BY outcome
        ORDER BY calls DESC
    """
    return [
        {"outcome": r.outcome, "calls": int(r.calls or 0)}
        for r in _client().query(sql).result()
    ]


def funnel_medium_to_outcome(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> list[dict]:
    """Crosstab of UTM medium × outcome for the Sankey's first link set.

    Each row carries the call count flowing from one medium bucket into one
    outcome bucket. Spam excluded; only calls with a callscoring row count.
    """
    if not invoca_campaign_ids:
        return []
    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    not_spam = _non_spam_predicate_sql()
    outcome_case = _outcome_case_sql("cs.")
    sql = f"""
        SELECT
          CASE
            WHEN t.utm_medium IS NULL
              OR LOWER(TRIM(t.utm_medium)) IN ('', 'untagged', 'none', 'na', 'nan')
              THEN 'untagged'
            ELSE LOWER(TRIM(t.utm_medium))
          END            AS medium,
          {outcome_case} AS outcome,
          COUNT(*)       AS calls
        FROM `{_CLINIC_DATA}.transactions` t
        JOIN `{_CLINIC_DATA}.callscoring` cs ON cs.complete_call_id = t.complete_call_id
        WHERE {scope}
          AND {not_spam}
        GROUP BY medium, outcome
    """
    return [
        {"medium": r.medium, "outcome": r.outcome, "calls": int(r.calls or 0)}
        for r in _client().query(sql).result()
    ]


# ── End-to-end pipeline · Stage 3 · patient type (Existing / New / Not Found) ─
#
# Only "Appointment Booked" calls feed into Stage 3 per the spec. Matching
# is done on the last 10 digits of the calling phone vs the patient's
# home/work/mobile telephone columns in Blueprint_PHI.ClientDemographics.
# A patient is "Existing" if their `created_time` is before the call's
# `start_time_local`, and "New" otherwise.


def stage3_patient_type(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> list[dict]:
    """Booked calls bucketed by patient type.

    Returns ``[{"patient_type": str, "calls": int}, ...]``. Buckets:
    ``Existing``, ``New``, ``Not Found``.
    """
    if not invoca_campaign_ids:
        return []
    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    not_spam = _non_spam_predicate_sql()
    sql = f"""
        WITH booked_calls AS (
          SELECT
            t.transaction_id,
            t.start_time_local,
            RIGHT(REGEXP_REPLACE(IFNULL(t.calling_phone_number, ''), r'\\D', ''), 10) AS phone_norm
          FROM `{_CLINIC_DATA}.transactions` t
          JOIN `{_CLINIC_DATA}.callscoring` cs ON cs.complete_call_id = t.complete_call_id
          WHERE {scope}
            AND {not_spam}
            AND cs.appointment_booked = TRUE
        ),
        patients AS (
          SELECT
            client_id,
            SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%E*S', created_time) AS created_ts,
            RIGHT(REGEXP_REPLACE(phone, r'\\D', ''), 10) AS phone_norm
          FROM `{_BP}.ClientDemographics` cd,
          UNNEST([cd.home_telephone_no, cd.work_telephone_no, cd.mobile_telephone_no]) AS phone
          WHERE cd._clinic_id = @clinic_id
            AND phone IS NOT NULL
            AND LENGTH(RIGHT(REGEXP_REPLACE(phone, r'\\D', ''), 10)) = 10
        ),
        matched AS (
          -- For each call, find the earliest patient row that matches by phone.
          SELECT
            bc.transaction_id,
            bc.start_time_local,
            ANY_VALUE(p.created_ts) AS patient_created_ts
          FROM booked_calls bc
          JOIN patients p ON p.phone_norm = bc.phone_norm
          GROUP BY bc.transaction_id, bc.start_time_local
        ),
        labelled AS (
          SELECT
            bc.transaction_id,
            CASE
              WHEN m.patient_created_ts IS NULL THEN 'Not Found'
              -- start_time_local is a TZ-suffixed string; parse with offset.
              WHEN m.patient_created_ts <=
                   SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S %Ez',
                                        bc.start_time_local) THEN 'Existing'
              ELSE 'New'
            END AS patient_type
          FROM booked_calls bc
          LEFT JOIN matched m USING (transaction_id)
        )
        SELECT patient_type, COUNT(*) AS calls
        FROM labelled
        GROUP BY patient_type
    """
    job_config = bigquery.QueryJobConfig(query_parameters=_params(clinic_id))
    return [
        {"patient_type": r.patient_type, "calls": int(r.calls or 0)}
        for r in _client().query(sql, job_config=job_config).result()
    ]


# ── Stage 2 detail pages: per-call line items for the two leak buckets ───────
#
# Both pages show the same shape (timestamp, phone, duration, reasoning) — the
# only difference is the outcome filter. Spam excluded.


def _stage2_outcome_detail(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int,
    outcome: str,
    limit: int | None = None,
    window: "Window | None" = None,
) -> list[dict]:
    if not invoca_campaign_ids:
        return []
    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    not_spam = _non_spam_predicate_sql()
    outcome_case = _outcome_case_sql("cs.")
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    sql = f"""
        SELECT
          t.complete_call_id,
          t.start_time_local,
          t.timestamp,
          t.calling_phone_number,
          t.duration,
          t.connect_duration,
          t.utm_medium,
          cs.reasoning,
          {outcome_case} AS outcome
        FROM `{_CLINIC_DATA}.transactions` t
        JOIN `{_CLINIC_DATA}.callscoring` cs ON cs.complete_call_id = t.complete_call_id
        WHERE {scope}
          AND {not_spam}
          AND ({outcome_case}) = @outcome
        ORDER BY t.timestamp DESC
        {limit_clause}
    """
    params = [bigquery.ScalarQueryParameter("outcome", "STRING", outcome)]
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    out: list[dict] = []
    for r in _client().query(sql, job_config=job_config).result():
        out.append({
            "complete_call_id":     r.complete_call_id or "",
            "start_time_local":     r.start_time_local,
            "calling_phone_number": r.calling_phone_number,
            "duration":             int(r.duration or 0),
            "connect_duration":     int(r.connect_duration or 0),
            "utm_medium":           r.utm_medium or "",
            "reasoning":            r.reasoning or "",
        })
    return out


def _stage2_outcome_count(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int,
    outcome: str,
    window: "Window | None" = None,
) -> int:
    """COUNT(*) of calls in a given Stage-2 outcome bucket.

    Companion to :func:`_stage2_outcome_detail` — used to surface the cohort
    total when the detail query is LIMITed for an inline preview.
    """
    if not invoca_campaign_ids:
        return 0
    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    not_spam = _non_spam_predicate_sql()
    outcome_case = _outcome_case_sql("cs.")
    sql = f"""
        SELECT COUNT(*) AS n
        FROM `{_CLINIC_DATA}.transactions` t
        JOIN `{_CLINIC_DATA}.callscoring` cs ON cs.complete_call_id = t.complete_call_id
        WHERE {scope}
          AND {not_spam}
          AND ({outcome_case}) = @outcome
    """
    params = [bigquery.ScalarQueryParameter("outcome", "STRING", outcome)]
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    rows = list(_client().query(sql, job_config=job_config).result())
    return int(rows[0].n or 0) if rows else 0


def callscoring_flag_summary(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """Per-flag call counts from ``ClinicData.callscoring`` for the window.

    Flags are NOT mutually exclusive — a single call can be flagged as both
    ``existing_customer`` and ``appointment_booked``, for example. Counts here
    are raw flag hits (not bucketed via precedence), so they overlap.

    Returns ``{
        "total_scored": int,
        "flags": [
            {"key": "appointment_booked", "label": "Appointment booked", "calls": 142},
            ...
        ]  # sorted by calls desc
    }``. Empty Invoca list → zeros.
    """
    empty = {"total_scored": 0, "flags": []}
    if not invoca_campaign_ids:
        return empty
    w = _win(window, days)
    scope = _spam_scope_clause(invoca_campaign_ids, days, window=w)
    sql = f"""
        SELECT
          COUNTIF(cs.appointment_booked)           AS appointment_booked,
          COUNTIF(cs.qualified_lead_no_conversion) AS qualified_lead_no_conversion,
          COUNTIF(cs.existing_customer)            AS existing_customer,
          COUNTIF(cs.no_conversation)              AS no_conversation,
          COUNTIF(cs.spam_or_solicitor)            AS spam_or_solicitor,
          COUNTIF(cs.wrong_number)                 AS wrong_number,
          COUNT(*)                                 AS total_scored
        FROM `{_CLINIC_DATA}.transactions` t
        JOIN `{_CLINIC_DATA}.callscoring` cs
          ON cs.complete_call_id = t.complete_call_id
        WHERE {scope}
    """
    rows = list(_client().query(sql).result())
    if not rows:
        return empty
    r = rows[0]
    labels = [
        ("appointment_booked",           "Appointment booked"),
        ("qualified_lead_no_conversion", "Qualified lead — no conversion"),
        ("existing_customer",            "Existing customer"),
        ("no_conversation",              "No conversation"),
        ("spam_or_solicitor",            "Spam / solicitor"),
        ("wrong_number",                 "Wrong number"),
    ]
    flags = [
        {"key": key, "label": label, "calls": int(getattr(r, key) or 0)}
        for key, label in labels
    ]
    flags.sort(key=lambda f: f["calls"], reverse=True)
    return {"total_scored": int(r.total_scored or 0), "flags": flags}


def caller_types_summary(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    last_n: int = 100,
) -> dict[str, Any]:
    """Bucket the most-recent ``last_n`` scored calls by caller type.

    Flags in callscoring are non-mutually-exclusive, so we apply precedence:
        1. spam_or_solicitor          → "Spam / solicitor"
        2. wrong_number               → "Wrong number"
        3. existing_customer          → "Existing patient"
        4. no_conversation            → "Voicemail / hangup"
        5. appointment_booked         → "New prospect — booked"
        6. qualified_lead_no_conv     → "New prospect — leaked"
        7. else                       → "Other"

    Returns ``{"window_size": int, "buckets": [{"type": str, "calls": int, "pct": float}]}``.
    `window_size` is the actual number of rows considered (may be < last_n).
    """
    if not invoca_campaign_ids:
        return {"window_size": 0, "buckets": []}
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    sql = f"""
        WITH recent AS (
          SELECT
            cs.existing_customer, cs.spam_or_solicitor, cs.no_conversation,
            cs.wrong_number, cs.appointment_booked,
            cs.qualified_lead_no_conversion
          FROM `{_CLINIC_DATA}.transactions` t
          JOIN `{_CLINIC_DATA}.callscoring` cs ON cs.complete_call_id = t.complete_call_id
          WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
          ORDER BY t.timestamp DESC
          LIMIT {int(last_n)}
        )
        SELECT
          CASE
            WHEN spam_or_solicitor                THEN 'Spam / solicitor'
            WHEN wrong_number                     THEN 'Wrong number'
            WHEN existing_customer                THEN 'Existing patient'
            WHEN no_conversation                  THEN 'Voicemail / hangup'
            WHEN appointment_booked               THEN 'New prospect — booked'
            WHEN qualified_lead_no_conversion     THEN 'New prospect — leaked'
            ELSE 'Other'
          END AS type,
          COUNT(*) AS calls
        FROM recent
        GROUP BY type
        ORDER BY calls DESC
    """
    rows = list(_client().query(sql).result())
    total = sum(int(r.calls or 0) for r in rows)
    buckets = [
        {
            "type": r.type,
            "calls": int(r.calls or 0),
            "pct": (100.0 * int(r.calls or 0) / total) if total else 0.0,
        }
        for r in rows
    ]
    return {"window_size": total, "buckets": buckets}


def no_conversation_detail(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    limit: int | None = None,
    window: "Window | None" = None,
) -> list[dict]:
    """Per-call rows where the funnel ends at "No Conversation"."""
    return _stage2_outcome_detail(
        clinic_id, invoca_campaign_ids, days, "No Conversation", limit=limit, window=window
    )


def no_conversation_count(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> int:
    """Total count of "No Conversation" calls — for cohort banners."""
    return _stage2_outcome_count(
        clinic_id, invoca_campaign_ids, days, "No Conversation", window=window
    )


def qualified_lead_no_conv_detail(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    limit: int | None = None,
    window: "Window | None" = None,
) -> list[dict]:
    """Per-call rows where the caller was a qualified lead but didn't book."""
    return _stage2_outcome_detail(
        clinic_id, invoca_campaign_ids, days, "Qualified Lead - No Conversion", limit=limit, window=window
    )


def qualified_lead_no_conv_count(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> int:
    """Total count of "Qualified Lead - No Conversion" calls."""
    return _stage2_outcome_count(
        clinic_id, invoca_campaign_ids, days, "Qualified Lead - No Conversion", window=window
    )


# ── Actionable client worklists (Blueprint-only) ─────────────────────────────
#
# These power the per-clinic "worklists" the front desk acts on. They read only
# Blueprint_PHI and are isolated by `_clinic_id`. The matched strings below are
# tunable per the SELECT DISTINCT checks run against live data (Blueprint's
# free-text fields vary by clinic); confirm before onboarding a new PMS feed.

# event_type values that denote a fitting appointment. Match is LOWER LIKE so
# all variants (Fitting, Fitting ITE/BTE, Lyric Fitting, Pre-Fitting, …) are
# covered without enumerating every clinic's spelling. Uses "%fit%" rather than
# "%fitting%" because some clinics abbreviate the event type to "Fit"/"Re-fit"
# (e.g. Alto), which "%fitting%" would silently miss — zeroing their fitting
# metrics. Verified across all clinics that every "%fit%" match is genuinely
# fitting-related (Fit, Re-fit, First Fit, HEARING TEST & FIT, …) with no false
# positives like "benefit".
_FITTING_EVENT_LIKE = "%fit%"

# InvoiceLineItems.item_type values that count as a hearing-aid purchase.
_HA_ITEM_TYPES = ("ha", "hao")

# ClientAids.status prefix for a device the patient currently owns (excludes
# 'Order returned', 'Order cancelled', '* - Inactive', 'Added', etc.).
_ACTIVE_AID_STATUS_LIKE = "Active%"

# ClientAids includes accessories (chargers, dry-kits, ear-tips, TV streamers).
# HearingAidModel.is_hearing_aid (STRING 'True'/'False') is the authoritative
# classifier — join on model_id to keep only actual hearing aids in the
# warranty / upgrade segments. Returned as a SQL fragment joined into the FROM.
def _hearing_aid_join_sql(aid_alias: str = "a", model_alias: str = "m") -> str:
    return (
        f"JOIN `{_BP}.HearingAidModel` {model_alias} "
        f"ON {model_alias}._clinic_id = {aid_alias}._clinic_id "
        f"AND {model_alias}.model_id = {aid_alias}.model_id "
        f"AND LOWER(TRIM({model_alias}.is_hearing_aid)) = 'true'"
    )


def _digits(v: "str | None") -> str:
    """Strip a phone string to bare digits (for CSV export)."""
    return re.sub(r"\D", "", v or "")


def cohort_detail(
    clinic_id: str,
    *,
    event_types: "list[str] | None" = None,
    event_like: "str | None" = None,
    statuses: "list[str] | tuple[str, ...]" = _STATUS_COMPLETED,
    require_no_sale: bool = True,
    ha_item_types: "list[str] | tuple[str, ...]" = _HA_ITEM_TYPES,
    days: int = 365,
    limit: int | None = None,
    include_contact: bool = False,
    window: "Window | None" = None,
) -> list[dict]:
    """Patients in a reactivation cohort — the generalized worklist query.

    Selects each patient's most-recent qualifying appointment (matched by
    ``event_types`` IN-list OR ``event_like`` LIKE pattern, and ``statuses``) in
    the window. When ``require_no_sale`` is set, patients with a hearing-aid
    invoice line (``ha_item_types``) in the same window are excluded — the
    "tested/fitted but not sold" signal; when unset (e.g. no-show), no sale
    exclusion is applied. Compliance flags are surfaced, never dropped.

    ``include_contact`` additionally selects email/phone + ``do_not_email`` (PHI)
    for the CSV export path; the on-screen worklist calls with it False.

    Taxonomy is passed in by the caller (resolved from Cloud SQL); this function
    stays BigQuery-only. All values are bound query parameters.
    """
    if bool(event_types) == bool(event_like):
        raise ValueError("pass exactly one of event_types / event_like")
    if not statuses:
        raise ValueError("statuses must be non-empty")
    if require_no_sale and not ha_item_types:
        raise ValueError("ha_item_types must be non-empty when require_no_sale is set")

    w = _win(window, days)
    client = _client()
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""

    params = [
        bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
        bigquery.ArrayQueryParameter("statuses", "STRING", list(statuses)),
    ]
    if event_types:
        appt_filter = "LOWER(event_type) IN UNNEST(@event_types)"
        params.append(bigquery.ArrayQueryParameter(
            "event_types", "STRING", [t.lower() for t in event_types]))
    else:
        appt_filter = "LOWER(event_type) LIKE @event_like"
        params.append(bigquery.ScalarQueryParameter("event_like", "STRING", event_like))

    if require_no_sale:
        # Exclude only patients who bought a hearing aid WITHIN the same window —
        # a visit that didn't convert is the signal regardless of a purchase
        # years ago. Also bounds the InvoiceLineItems scan.
        ha_cte = f""",
            ha_clients AS (
              SELECT DISTINCT client_id
              FROM `{_BP}.InvoiceLineItems`
              WHERE _clinic_id = @clinic_id
                AND LOWER(item_type) IN UNNEST(@ha_item_types)
                AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', invoice_date)", w)}
            )"""
        ha_join = "LEFT JOIN ha_clients h USING (client_id)"
        ha_where = "AND h.client_id IS NULL"
        params.append(bigquery.ArrayQueryParameter(
            "ha_item_types", "STRING", [t.lower() for t in ha_item_types]))
    else:
        ha_cte = ha_join = ha_where = ""

    contact_select = ""
    if include_contact:
        contact_select = """,
              cd.email_address,
              cd.mobile_telephone_no,
              cd.home_telephone_no,
              cd.work_telephone_no,
              COALESCE(cd.do_not_email, '') AS do_not_email"""

    rows = list(client.query(
        f"""
            WITH tested AS (
              SELECT
                client_id,
                event_type,
                status_2,
                start_time,
                ROW_NUMBER() OVER (
                  PARTITION BY client_id
                  ORDER BY SAFE_CAST(start_time AS TIMESTAMP) DESC
                ) AS rn
              FROM `{_BP}.Appointments`
              WHERE _clinic_id = @clinic_id
                AND {appt_filter}
                AND status_2 IN UNNEST(@statuses)
                AND {_ts_between("SAFE_CAST(start_time AS TIMESTAMP)", w)}
            ){ha_cte}
            SELECT
              f.client_id,
              cd.given_name,
              cd.surname,
              f.event_type                          AS appt_event_type,
              f.start_time                           AS appt_start_time,
              f.status_2                             AS appt_status,
              COALESCE(NULLIF(cd.status, ''), 'Unknown') AS patient_status,
              COALESCE(cd.do_not_send_commercial_messages, '') AS do_not_send_commercial_messages,
              COALESCE(cd.do_not_text, '')           AS do_not_text{contact_select}
            FROM tested f
            {ha_join}
            LEFT JOIN `{_BP}.ClientDemographics` cd
              ON cd._clinic_id = @clinic_id AND cd.client_id = f.client_id
            WHERE f.rn = 1
              {ha_where}
            ORDER BY SAFE_CAST(f.start_time AS TIMESTAMP) DESC
            {limit_clause}
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result())

    out = []
    for r in rows:
        row = {
            "client_id":        r.client_id or "",
            "given_name":       r.given_name or "",
            "surname":          r.surname or "",
            "appt_event_type":  r.appt_event_type or "",
            "appt_start_time":  r.appt_start_time,
            "appt_status":      r.appt_status or "",
            "patient_status":   r.patient_status,
            "do_not_send_commercial_messages": _truthy_flag(r.do_not_send_commercial_messages),
            "do_not_text":      _truthy_flag(r.do_not_text),
        }
        if include_contact:
            mobile, home, work = (_digits(r.mobile_telephone_no),
                                  _digits(r.home_telephone_no),
                                  _digits(r.work_telephone_no))
            row.update({
                "email":         (r.email_address or "").strip().lower(),
                "primary_phone": mobile or home or work,   # prefer mobile for SMS
                "mobile_phone":  mobile,
                "home_phone":    home,
                "work_phone":    work,
                "do_not_email":  _truthy_flag(r.do_not_email),
            })
        out.append(row)
    return out


def pms_taxonomy_options(clinic_id: str) -> dict:
    """Distinct appointment event types + invoice item types for a clinic.

    Powers the "Load from PMS" helper in the worklist-taxonomy config UI, so an
    admin can pick each cohort's event/item types from the clinic's real values
    (with volume counts) rather than guessing spellings. Aggregate counts only —
    no patient rows / PHI. ``completed`` counts attended visits (Completed/
    Arrived) so the admin can see which types actually have throughput.
    """
    client = _client()
    params = [bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id)]
    appts = client.query(
        f"""
            SELECT event_type,
                   COUNT(*) AS appts,
                   COUNTIF(status_2 IN UNNEST(@completed)) AS completed,
                   COUNT(DISTINCT client_id) AS clients
            FROM `{_BP}.Appointments`
            WHERE _clinic_id = @clinic_id
            GROUP BY event_type
            ORDER BY appts DESC
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            *params,
            bigquery.ArrayQueryParameter("completed", "STRING", list(_STATUS_COMPLETED)),
        ]),
    ).result()
    items = client.query(
        f"""
            SELECT LOWER(item_type) AS item_type,
                   COUNT(*) AS lines,
                   COUNT(DISTINCT client_id) AS clients
            FROM `{_BP}.InvoiceLineItems`
            WHERE _clinic_id = @clinic_id
            GROUP BY item_type
            ORDER BY lines DESC
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result()
    return {
        "event_types": [
            {"value": r.event_type or "", "appts": r.appts,
             "completed": r.completed, "clients": r.clients}
            for r in appts if r.event_type
        ],
        "item_types": [
            {"value": r.item_type or "", "lines": r.lines, "clients": r.clients}
            for r in items if r.item_type
        ],
    }


def fitting_no_purchase_detail(
    clinic_id: str,
    days: int = 365,
    limit: int | None = None,
    window: "Window | None" = None,
) -> list[dict]:
    """Patients who had a fitting appointment but no hearing-aid purchase.

    Back-compat wrapper over :func:`cohort_detail` using the built-in default
    fitting cohort (``%fit%`` / Completed-Arrived / ``('ha','hao')``). Kept so
    ``fitting_no_purchase_count``, ``lifecycle_client_ids``, ``revenue_leakage``,
    ``lifecycle_summary`` and ``active_leads`` continue to work unchanged. The
    per-clinic configured cohorts flow through the worklist router, not here.
    """
    return cohort_detail(
        clinic_id,
        event_like=_FITTING_EVENT_LIKE,
        statuses=_STATUS_COMPLETED,
        require_no_sale=True,
        ha_item_types=_HA_ITEM_TYPES,
        days=days,
        limit=limit,
        window=window,
    )


def warranty_expiring_detail(
    clinic_id: str,
    days_ahead: int = 90,
    limit: int | None = None,
) -> list[dict]:
    """Patients whose device warranty or service plan expires within the window.

    Re-engagement / upsell list. Restricted to currently-owned ('Active%')
    devices so returned/cancelled/inactive orders don't generate calls. The
    upper-bound date is computed in Python and embedded as a literal to keep
    the query text byte-stable for BigQuery's results cache (see the window
    helpers above).
    """
    client = _client()
    today = _dt.datetime.now(_dt.timezone.utc).date()
    horizon = (today + _dt.timedelta(days=int(days_ahead))).isoformat()
    today_lit = today.isoformat()
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    rows = list(client.query(
        f"""
            WITH aids AS (
              SELECT
                a.client_id, a.model_name, a.side, a.status,
                a.purchase_date,
                a.warranty_expiry_date,
                a.service_plan_expiry_date,
                SAFE.PARSE_DATE('%Y-%m-%d', a.warranty_expiry_date)     AS wexp,
                SAFE.PARSE_DATE('%Y-%m-%d', a.service_plan_expiry_date) AS sexp
              FROM `{_BP}.ClientAids` a
              {_hearing_aid_join_sql("a", "m")}
              WHERE a._clinic_id = @clinic_id
                AND a.status LIKE @active_status_like
            )
            SELECT
              a.client_id,
              cd.given_name,
              cd.surname,
              a.model_name,
              a.side,
              a.purchase_date,
              a.warranty_expiry_date,
              a.service_plan_expiry_date,
              CASE
                WHEN a.wexp BETWEEN DATE '{today_lit}' AND DATE '{horizon}'
                 AND a.sexp BETWEEN DATE '{today_lit}' AND DATE '{horizon}' THEN 'warranty+service_plan'
                WHEN a.wexp BETWEEN DATE '{today_lit}' AND DATE '{horizon}' THEN 'warranty'
                ELSE 'service_plan'
              END AS expiring_type,
              LEAST(
                IFNULL(a.wexp, DATE '{horizon}'),
                IFNULL(a.sexp, DATE '{horizon}')
              ) AS soonest_expiry,
              COALESCE(NULLIF(cd.status, ''), 'Unknown') AS patient_status,
              COALESCE(cd.do_not_send_commercial_messages, '') AS do_not_send_commercial_messages,
              COALESCE(cd.do_not_text, '')               AS do_not_text
            FROM aids a
            LEFT JOIN `{_BP}.ClientDemographics` cd
              ON cd._clinic_id = @clinic_id AND cd.client_id = a.client_id
            WHERE a.wexp BETWEEN DATE '{today_lit}' AND DATE '{horizon}'
               OR a.sexp BETWEEN DATE '{today_lit}' AND DATE '{horizon}'
            ORDER BY soonest_expiry ASC
            {limit_clause}
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(
            clinic_id, active_status_like=_ACTIVE_AID_STATUS_LIKE)),
    ).result())
    return [
        {
            "client_id":                r.client_id or "",
            "given_name":               r.given_name or "",
            "surname":                  r.surname or "",
            "model_name":               r.model_name or "",
            "side":                     r.side or "",
            "purchase_date":            r.purchase_date or "",
            "warranty_expiry_date":     r.warranty_expiry_date or "",
            "service_plan_expiry_date": r.service_plan_expiry_date or "",
            "expiring_type":            r.expiring_type,
            "patient_status":           r.patient_status,
            "do_not_send_commercial_messages": _truthy_flag(r.do_not_send_commercial_messages),
            "do_not_text":              _truthy_flag(r.do_not_text),
        }
        for r in rows
    ]


# ── Database-reactivation segments (Blueprint-only, dormant patients) ─────────
#
# Like the worklists above: isolated by `_clinic_id`, surface compliance flags,
# date cutoffs computed in Python and embedded as literals for cache-stability.
# Deceased patients are always excluded.

# ClientDemographics.status values that are NOT contactable for reactivation.
_REACTIVATION_EXCLUDE_STATUS = ("Deceased",)


def _exclude_status_sql(alias: str = "cd") -> str:
    vals = ", ".join(f"'{s}'" for s in _REACTIVATION_EXCLUDE_STATUS)
    return f"{alias}.status NOT IN ({vals})"


def lapsed_patients_detail(clinic_id: str, years: int = 3, limit: int | None = None) -> list[dict]:
    """Patients with no appointment AND no invoice in the last ``years``.

    Dormant-patient reactivation: their most recent appointment and most recent
    invoice are both older than the cutoff (a recent touch of either kind means
    they're not lapsed). Requires at least one historical touch so never-engaged
    ghost records are excluded.
    """
    client = _client()
    cutoff = (_dt.datetime.now(_dt.timezone.utc).date()
              - _dt.timedelta(days=int(years) * 365)).isoformat()
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    rows = list(client.query(
        f"""
            WITH last_appt AS (
              SELECT client_id, MAX(SAFE_CAST(start_time AS TIMESTAMP)) AS la
              FROM `{_BP}.Appointments` WHERE _clinic_id = @clinic_id GROUP BY client_id
            ),
            last_inv AS (
              SELECT client_id, MAX(SAFE.PARSE_DATE('%Y-%m-%d', invoice_date)) AS li
              FROM `{_BP}.InvoiceMaster` WHERE _clinic_id = @clinic_id GROUP BY client_id
            )
            SELECT
              cd.client_id,
              cd.given_name,
              cd.surname,
              COALESCE(NULLIF(cd.status, ''), 'Unknown') AS patient_status,
              DATE(a.la)                                 AS last_appt_date,
              i.li                                       AS last_invoice_date,
              COALESCE(cd.do_not_send_commercial_messages, '') AS do_not_send_commercial_messages,
              COALESCE(cd.do_not_text, '')               AS do_not_text
            FROM `{_BP}.ClientDemographics` cd
            LEFT JOIN last_appt a ON a.client_id = cd.client_id
            LEFT JOIN last_inv  i ON i.client_id = cd.client_id
            WHERE cd._clinic_id = @clinic_id
              AND {_exclude_status_sql('cd')}
              AND (a.la IS NOT NULL OR i.li IS NOT NULL)
              AND COALESCE(DATE(a.la), DATE '1900-01-01') < DATE '{cutoff}'
              AND COALESCE(i.li, DATE '1900-01-01')       < DATE '{cutoff}'
            ORDER BY GREATEST(
              COALESCE(DATE(a.la), DATE '1900-01-01'),
              COALESCE(i.li, DATE '1900-01-01')
            ) DESC
            {limit_clause}
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
    ).result())
    return [
        {
            "client_id":         r.client_id or "",
            "given_name":        r.given_name or "",
            "surname":           r.surname or "",
            "patient_status":    r.patient_status,
            "last_appt_date":    str(r.last_appt_date) if r.last_appt_date else "",
            "last_invoice_date": str(r.last_invoice_date) if r.last_invoice_date else "",
            "do_not_send_commercial_messages": _truthy_flag(r.do_not_send_commercial_messages),
            "do_not_text":       _truthy_flag(r.do_not_text),
        }
        for r in rows
    ]


def recall_due_detail(
    clinic_id: str,
    overdue_days: int = 365,
    days_ahead: int = 30,
    limit: int | None = None,
) -> list[dict]:
    """Missed / due follow-ups from Blueprint's ClientRecall.

    A recall whose date falls in ``[today - overdue_days, today + days_ahead]``
    and for which the patient has had NO appointment on/after the recall date —
    i.e. the scheduled follow-up was never actioned.
    """
    client = _client()
    today = _dt.datetime.now(_dt.timezone.utc).date()
    lower = (today - _dt.timedelta(days=int(overdue_days))).isoformat()
    upper = (today + _dt.timedelta(days=int(days_ahead))).isoformat()
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    rows = list(client.query(
        f"""
            WITH recalls AS (
              SELECT client_id,
                     recall_date,
                     recall_type,
                     SAFE.PARSE_DATE('%Y-%m-%d', recall_date) AS rd
              FROM `{_BP}.ClientRecall` WHERE _clinic_id = @clinic_id
            ),
            appts AS (
              SELECT client_id, MAX(SAFE_CAST(start_time AS TIMESTAMP)) AS la
              FROM `{_BP}.Appointments` WHERE _clinic_id = @clinic_id GROUP BY client_id
            )
            SELECT
              cd.client_id,
              cd.given_name,
              cd.surname,
              r.recall_date,
              COALESCE(NULLIF(r.recall_type, ''), 'Recall') AS recall_type,
              COALESCE(NULLIF(cd.status, ''), 'Unknown')    AS patient_status,
              COALESCE(cd.do_not_send_commercial_messages, '') AS do_not_send_commercial_messages,
              COALESCE(cd.do_not_text, '')                  AS do_not_text
            FROM recalls r
            JOIN `{_BP}.ClientDemographics` cd
              ON cd._clinic_id = @clinic_id AND cd.client_id = r.client_id
             AND {_exclude_status_sql('cd')}
            LEFT JOIN appts a ON a.client_id = r.client_id
            WHERE r.rd BETWEEN DATE '{lower}' AND DATE '{upper}'
              AND (a.la IS NULL OR DATE(a.la) < r.rd)
            ORDER BY r.rd ASC
            {limit_clause}
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
    ).result())
    return [
        {
            "client_id":      r.client_id or "",
            "given_name":     r.given_name or "",
            "surname":        r.surname or "",
            "recall_date":    r.recall_date or "",
            "recall_type":    r.recall_type,
            "patient_status": r.patient_status,
            "do_not_send_commercial_messages": _truthy_flag(r.do_not_send_commercial_messages),
            "do_not_text":    _truthy_flag(r.do_not_text),
        }
        for r in rows
    ]


def upgrade_candidates_detail(
    clinic_id: str,
    min_age_years: int = 4,
    limit: int | None = None,
) -> list[dict]:
    """Patients on a currently-owned device older than ``min_age_years``.

    Upgrade / re-engagement: active aids ('Active%') past typical HA lifespan.
    """
    client = _client()
    cutoff = (_dt.datetime.now(_dt.timezone.utc).date()
              - _dt.timedelta(days=int(min_age_years) * 365)).isoformat()
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    rows = list(client.query(
        f"""
            SELECT
              a.client_id,
              cd.given_name,
              cd.surname,
              a.model_name,
              a.side,
              a.purchase_date,
              a.warranty_expiry_date,
              COALESCE(NULLIF(cd.status, ''), 'Unknown') AS patient_status,
              COALESCE(cd.do_not_send_commercial_messages, '') AS do_not_send_commercial_messages,
              COALESCE(cd.do_not_text, '')               AS do_not_text
            FROM `{_BP}.ClientAids` a
            {_hearing_aid_join_sql("a", "m")}
            JOIN `{_BP}.ClientDemographics` cd
              ON cd._clinic_id = @clinic_id AND cd.client_id = a.client_id
             AND {_exclude_status_sql('cd')}
            WHERE a._clinic_id = @clinic_id
              AND a.status LIKE @active_status_like
              AND SAFE.PARSE_DATE('%Y-%m-%d', a.purchase_date) < DATE '{cutoff}'
            ORDER BY SAFE.PARSE_DATE('%Y-%m-%d', a.purchase_date) ASC
            {limit_clause}
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(
            clinic_id, active_status_like=_ACTIVE_AID_STATUS_LIKE)),
    ).result())
    return [
        {
            "client_id":            r.client_id or "",
            "given_name":           r.given_name or "",
            "surname":              r.surname or "",
            "model_name":           r.model_name or "",
            "side":                 r.side or "",
            "purchase_date":        r.purchase_date or "",
            "warranty_expiry_date": r.warranty_expiry_date or "",
            "patient_status":       r.patient_status,
            "do_not_send_commercial_messages": _truthy_flag(r.do_not_send_commercial_messages),
            "do_not_text":          _truthy_flag(r.do_not_text),
        }
        for r in rows
    ]


# ════════════════════════════════════════════════════════════════════════════
# Intelligence Overview metrics (date-range driven)
#
# These power the new React Overview page. Each is window-aware (accepts an
# explicit ``Window`` or falls back to ``days``) and fail-safe (returns zeros /
# ``None`` on query error rather than raising), so a single bad sub-query never
# blanks the whole page. Clinic-hours parsing and the LLM "forward
# recommendations" live outside this pure-data module (see ``clinic_hours.py``
# and ``payloads.py``).
# ════════════════════════════════════════════════════════════════════════════

def _year_ago(w: "Window") -> "Window":
    """The same calendar span shifted back one year, for the YoY headline."""
    def _back(d: _dt.date) -> _dt.date:
        try:
            return d.replace(year=d.year - 1)
        except ValueError:           # Feb 29 in a non-leap prior year → Feb 28
            return d.replace(year=d.year - 1, day=28)
    start = _back(w.start)
    end_incl = _back(w.end_excl - _dt.timedelta(days=1))
    return Window(start.isoformat(), end_incl.isoformat())


def call_capture(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """Phone-side volume + capture for the window: ``calls`` (non-spam),
    ``connected`` (real conversation), ``booked`` (connected + appointment),
    and ``capture_rate`` = booked / connected."""
    w = _win(window, days)
    out = {"calls": 0, "connected": 0, "booked": 0, "capture_rate": None}
    if not invoca_campaign_ids:
        return out
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    try:
        rows = list(_client().query(f"""
            WITH c AS (
              SELECT
                IFNULL(cs.spam_or_solicitor, FALSE)   AS is_spam,
                (cs.complete_call_id IS NOT NULL)     AS has_cs,
                IFNULL(cs.no_conversation, FALSE)     AS no_conv,
                IFNULL(cs.appointment_booked, FALSE)  AS booked
              FROM `{_CLINIC_DATA}.transactions` t
              LEFT JOIN `{_CLINIC_DATA}.callscoring` cs
                ON cs.complete_call_id = t.complete_call_id
              WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
                AND {_ts_between('t.timestamp', w)}
            )
            SELECT
              COUNTIF(NOT is_spam)                                       AS calls,
              COUNTIF(NOT is_spam AND has_cs AND NOT no_conv)            AS connected,
              COUNTIF(NOT is_spam AND has_cs AND NOT no_conv AND booked) AS booked
            FROM c
        """).result())
    except Exception as exc:
        log.warning("call_capture failed clinic=%s: %s", clinic_id, exc)
        return out
    if rows:
        r = rows[0]
        out["calls"] = int(r.calls or 0)
        out["connected"] = int(r.connected or 0)
        out["booked"] = int(r.booked or 0)
        out["capture_rate"] = (out["booked"] / out["connected"]) if out["connected"] else None
    return out


# (CALL_BOOKING_MATCH_DAYS is defined at the top of the module — it is a default
# argument for functions declared above this point, and defaults evaluate at
# definition time.)

# How far back before a booking call to look for preceding calls from the same
# number that "led to" the booking (the touchpoint history the client sees when
# filtering on Booked). Bounds an episode so an unrelated old call isn't linked.
CALL_BOOKING_LOOKBACK_DAYS = 30

# How many days after a call a PMS invoice may be dated and still be attributed
# to that call's patient. Much wider than the booking window: the appointment is
# created during/just after the call, but the actual sale (hearing aids) often
# lands weeks later. Patient-level attribution — approximate, labelled as such.
CALL_INVOICE_ATTRIBUTION_DAYS = 90


def _call_tagging_cte(in_iv: str, w: "Window") -> str:
    """Shared CTE that tags each inbound call with the funnel's mutually-exclusive
    bucket flags. Both the aggregate funnel (``call_outcomes_funnel``) and the
    per-call drill-down lists (``leak_calls``) build on this ONE definition so
    their numbers always reconcile. Expects query params ``@clinic_id`` and
    ``@match_days``; exposes a ``tagged`` CTE with per-call columns
    (complete_call_id, call_ts, start_time_local, phone_raw, reasoning) plus the
    flags: has_cs, no_transcript, spam, is_wrong, genuine, reconciled,
    connected_raw, is_voicemail, is_hangup, qualified, existing, appt_active,
    appt_lapsing, appt_deep_dormant, appt_none, tx_existing, appt_booked.

    ``existing`` is the CANONICAL new-vs-existing patient signal: the UNION of a
    PMS prior-appointment (caller's phone matches a clinic patient with an
    appointment on an EARLIER CLINIC-LOCAL DAY than the call — day-grain, so an
    appointment created by this very call can never mark the caller existing;
    PMS times are local-naive strings, so timestamp-level compares against the
    UTC call time skew by the UTC offset) and the transcript
    ``existing_customer`` flag. Either alone under-counts existing patients — the appointment anchor
    misses existing patients whose PMS history isn't linked to the matched
    record (common: duplicate/merged records), and the transcript misses
    family-on-behalf callers — so the union is used. A caller flagged by neither
    is NEW (new-patient acquisition). The ``appt_*`` flags split existing patients
    by the recency of their last appointment BEFORE the call (appointment-only,
    against the hearing-care recall cycle): ``appt_active`` = within 12 months;
    ``appt_lapsing`` = 12–24 months; ``appt_deep_dormant`` = 24 months+;
    ``appt_none`` = existing patient with no prior appointment (transcript-only /
    tested-not-sold — dormant from day one). Active vs dormant = appt_active vs
    NOT appt_active; the bands are a sub-segmentation of existing only.

    ``reconciled`` (booked) is ORTHOGONAL to patient type — it does NOT exclude
    existing patients; a booking is reported within its type segment
    (booked_new vs booked_existing). ``tx_existing`` exposes the raw transcript
    flag.

    NOTE: ``genuine`` requires ``has_cs`` (a callscoring row / transcript
    exists). Calls with NO transcript are ``no_transcript`` and partition the
    total alongside spam / wrong — they are NOT under genuine. Everything
    downstream keys on ``connected_raw`` (which implies ``has_cs``), so only
    genuine / never-connected shift because of this.

    MANUAL RELABELS: ``{_OVERRIDES_TABLE_NAME}`` (written by the hypervisor's
    relabel endpoint, latest row per call wins; a NULL ``outcome`` clears) is
    joined here and REWRITES the scoring flags, so a human relabel is
    authoritative for every consumer built on this CTE — funnel counts, monthly
    charts, drill-downs, and the per-call table all move together. An override
    also forces ``has_cs`` (a human label substitutes for a missing/empty
    transcript) and strips ``reconciled`` booked-credit (you can't relabel TO
    ``booked``; relabelling a booked call away is explicit un-crediting).
    ``override_outcome`` is exposed on ``tagged`` for display precedence."""
    return f"""
        WITH c0 AS (
          SELECT
            t.complete_call_id,
            t.timestamp                                    AS call_ts,
            t.start_time_local                             AS start_time_local,
            t.calling_phone_number                         AS phone_raw,
            RIGHT(REGEXP_REPLACE(IFNULL(t.calling_phone_number, ''), r'\\D', ''), 10) AS phone_norm,
            -- The call's CLINIC-LOCAL date. PMS timestamps (appointment
            -- start/created) are clinic-local naive strings that cast to UTC
            -- as-is, so comparing them against the true-UTC call_ts skews by
            -- the UTC offset — a same-day appointment booked minutes AFTER an
            -- afternoon call reads as BEFORE it. All call↔appointment date
            -- comparisons below use this local date so both sides are in
            -- clinic wall-clock.
            COALESCE(SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(t.start_time_local, 1, 10)),
                     DATE(t.timestamp))                    AS call_date_local,
            IFNULL(cs.spam_or_solicitor, FALSE)            AS spam,
            IFNULL(cs.wrong_number, FALSE)                 AS wrong,
            (cs.complete_call_id IS NOT NULL)              AS has_cs,
            IFNULL(cs.empty_transcript, FALSE)             AS empty_transcript,
            IFNULL(cs.no_conversation, FALSE)              AS no_conv,
            cs.no_conversation_type                        AS no_conv_type,
            IFNULL(cs.qualified_lead_no_conversion, FALSE) AS qualified,
            IFNULL(cs.existing_customer, FALSE)            AS tx_existing,
            IFNULL(cs.looking_to_book, FALSE)              AS looking_to_book,
            IFNULL(cs.appointment_booked, FALSE)           AS appt_booked,
            cs.reasoning                                   AS reasoning
          FROM `{_CLINIC_DATA}.transactions` t
          LEFT JOIN `{_CLINIC_DATA}.callscoring` cs
            ON cs.complete_call_id = t.complete_call_id
          WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
            AND {_ts_between('t.timestamp', w)}
          -- Exactly one row per call: transactions (and occasionally callscoring)
          -- can carry duplicate complete_call_id rows; keep the latest-scored so
          -- every consumer (funnel counts, tree, per-call table) agrees.
          QUALIFY ROW_NUMBER() OVER (
            PARTITION BY t.complete_call_id
            ORDER BY cs.scored_at DESC NULLS LAST, t.timestamp DESC) = 1
        ),
        ov AS (
          -- Latest manual relabel per call (append-only table; a NULL outcome
          -- row is an explicit "cleared" marker → falls back to model flags).
          SELECT complete_call_id, outcome
          FROM `{_OVERRIDES_TABLE}`
          WHERE clinic_id = @clinic_id
          QUALIFY ROW_NUMBER() OVER (
            PARTITION BY complete_call_id ORDER BY set_at DESC) = 1
        ),
        c AS (
          -- Scoring flags with manual relabels applied. An override REPLACES the
          -- model's mutually-exclusive outcome flags (each flag becomes "is the
          -- override exactly this label"), so downstream bucket logic needs no
          -- special cases.
          SELECT
            c0.complete_call_id, c0.call_ts, c0.start_time_local, c0.call_date_local,
            c0.phone_raw, c0.phone_norm,
            IF(ov.outcome IS NOT NULL, ov.outcome = 'spam',                    c0.spam)        AS spam,
            IF(ov.outcome IS NOT NULL, ov.outcome = 'wrong_number',            c0.wrong)       AS wrong,
            (c0.has_cs OR ov.outcome IS NOT NULL)                                              AS has_cs,
            IF(ov.outcome IS NOT NULL, FALSE,                                  c0.empty_transcript) AS empty_transcript,
            IF(ov.outcome IS NOT NULL, ov.outcome = 'no_conversation',         c0.no_conv)     AS no_conv,
            c0.no_conv_type,
            IF(ov.outcome IS NOT NULL, ov.outcome = 'qualified_no_conversion', c0.qualified)   AS qualified,
            IF(ov.outcome IS NOT NULL, ov.outcome = 'existing_patient',        c0.tx_existing) AS tx_existing,
            c0.looking_to_book, c0.appt_booked, c0.reasoning,
            ov.outcome AS override_outcome
          FROM c0
          LEFT JOIN ov USING (complete_call_id)
        ),
        patients AS (
          SELECT DISTINCT client_id, phone_norm
          FROM `{_PATIENT_CONTACTS}`
          WHERE _clinic_id = @clinic_id AND LENGTH(phone_norm) = 10
        ),
        -- PMS appointment dates for the clinic's patients, keyed by contact phone.
        -- Reuses the ``patients`` CTE so there's no second contacts scan.
        appt_dates AS (
          SELECT p.phone_norm, SAFE_CAST(a.start_time AS TIMESTAMP) AS appt_ts, a.status_2
          FROM patients p
          JOIN `{_BP}.Appointments` a
            ON a.client_id = p.client_id AND a._clinic_id = @clinic_id
        ),
        -- PMS prior-appointment signal: TRUE when the caller's phone matches a
        -- clinic patient who has an appointment DATED BEFORE the call. One boolean
        -- per call (LOGICAL_OR across any matching appointment). Combined with the
        -- transcript flag as a UNION (see ``existing`` in ``tagged``), so each
        -- catches what the other misses.
        prior_appt AS (
          SELECT c.complete_call_id,
                 -- Strictly-earlier LOCAL DAY, not timestamp: appt times are
                 -- clinic-local strings while call_ts is UTC, so a timestamp
                 -- compare marks brand-new patients "existing" when their first
                 -- appointment is created the same day as (minutes after) their
                 -- first call. Day-grain means a same-day appointment never
                 -- makes the caller existing — that appointment is this very
                 -- call's episode.
                 LOGICAL_OR(DATE(ad.appt_ts) < c.call_date_local) AS has_prior_appt,
                 -- Most recent appointment DATED BEFORE the call. Drives the
                 -- recency (active vs dormant) split of existing patients against
                 -- the hearing-care recall/replacement cycle. Appointment-only.
                 MAX(IF(DATE(ad.appt_ts) < c.call_date_local, ad.appt_ts, NULL)) AS last_prior_appt_ts
          FROM c
          LEFT JOIN appt_dates ad
            ON ad.phone_norm = c.phone_norm AND LENGTH(c.phone_norm) = 10
          GROUP BY c.complete_call_id
        ),
        booked_calls AS (
          -- Reconciled bookings, ONE call per appointment. Each PMS appointment
          -- created within match_days on/after a call is credited to the MOST
          -- RECENT genuine, connected call that could have produced it (the last
          -- call before the booking landed) — so ``booked`` counts DISTINCT
          -- appointments, not reconciled calls. Repeat earlier calls to the same
          -- appointment are NOT credited.
          --
          -- ``booked`` is ORTHOGONAL to patient type: it does NOT exclude
          -- existing patients. New-vs-existing is a separate dimension
          -- (``existing``), so a booking is reported within its patient-type
          -- segment (booked_new vs booked_existing) rather than being hidden.
          -- Still excludes spam / wrong-number / no-conversation / no-transcript
          -- (a coincidental phone match to an appointment ≠ a booking).
          SELECT complete_call_id FROM (
            SELECT c.complete_call_id,
                   ROW_NUMBER() OVER (
                     PARTITION BY a.event_id
                     ORDER BY c.call_ts DESC
                   ) AS rn
            FROM c
            JOIN patients p ON p.phone_norm = c.phone_norm AND LENGTH(c.phone_norm) = 10
            JOIN `{_BP}.Appointments` a ON a._clinic_id = @clinic_id AND a.client_id = p.client_id
            -- Local-day vs local-day (created_time is a clinic-local string):
            -- an evening call whose UTC date rolls to tomorrow must still match
            -- a booking created minutes later the same local day.
            WHERE DATE_DIFF(DATE(SAFE_CAST(a.created_time AS TIMESTAMP)), c.call_date_local, DAY)
                  BETWEEN 0 AND @match_days
              AND c.has_cs AND NOT c.empty_transcript
              AND NOT c.spam AND NOT c.wrong AND NOT c.no_conv
          )
          WHERE rn = 1
        ),
        tagged AS (
          SELECT
            c.complete_call_id, c.call_ts, c.start_time_local, c.phone_raw, c.reasoning,
            c.override_outcome,
            c.has_cs,
            -- "Has a usable transcript" — a callscoring row AND real content. An
            -- empty ``[]`` transcript still gets scored, so gate on content, not
            -- just row existence, or empty calls wrongly land in hangup.
            (c.has_cs AND NOT c.empty_transcript) AS has_content,
            (NOT (c.has_cs AND NOT c.empty_transcript)) AS no_transcript,
            c.spam,
            (NOT c.spam AND c.wrong)             AS is_wrong,
            (c.has_cs AND NOT c.empty_transcript AND NOT c.spam AND NOT c.wrong) AS genuine,
            -- A manual relabel strips booked-credit: 'booked' is not a relabel
            -- option (it's a PMS fact), so an override on a reconciled call is
            -- an explicit "this call wasn't the booking".
            (c.complete_call_id IN (SELECT complete_call_id FROM booked_calls)
             AND c.override_outcome IS NULL) AS reconciled,
            (c.has_cs AND NOT c.empty_transcript AND NOT c.no_conv) AS connected_raw,
            (c.no_conv AND c.no_conv_type = 'voicemail')                        AS is_voicemail,
            (c.no_conv AND (c.no_conv_type IS NULL OR c.no_conv_type != 'voicemail')) AS is_hangup,
            -- ``existing`` = UNION of the PMS prior-appointment signal and the
            -- transcript ``existing_customer`` flag. The appointment anchor catches
            -- phone-matched existing patients with linked history; the transcript
            -- catches existing patients whose PMS history isn't linked to the
            -- matched record (the larger group in practice). ``tx_existing`` also
            -- exposed raw for descriptive use.
            c.qualified,
            -- Override is authoritative for patient type too: a relabel decides
            -- the bucket outright (the PMS prior-appointment signal is ignored
            -- for relabelled calls so table + funnel can't disagree).
            IF(c.override_outcome IS NOT NULL,
               c.override_outcome = 'existing_patient',
               IFNULL(pa.has_prior_appt, FALSE) OR c.tx_existing) AS existing,
            -- Recency sub-segmentation of EXISTING patients, appointment-only,
            -- against the hearing-care recall/replacement cycle. ACTIVE = a prior
            -- appointment within the last 12 months (inside the annual recall
            -- rhythm). DORMANT = last prior appointment 12+ months ago OR an
            -- existing patient with no prior appointment at all (transcript-only /
            -- tested-not-sold — dormant from day one). Finer bands map to the CEC
            -- warranty-recent vs deep-dormant segmentation (lapsing 12–24mo,
            -- deep-dormant 24mo+). Data is backfilled to 2021-01-01, so a 12-month
            -- lookback from a 2026 call is fully observed. These flags partition
            -- existing only; they do NOT move new / existing / connected / booked.
            (pa.last_prior_appt_ts IS NOT NULL
             AND DATE_DIFF(c.call_date_local, DATE(pa.last_prior_appt_ts), DAY) <= 365) AS appt_active,
            (pa.last_prior_appt_ts IS NOT NULL
             AND DATE_DIFF(c.call_date_local, DATE(pa.last_prior_appt_ts), DAY) BETWEEN 366 AND 730) AS appt_lapsing,
            (pa.last_prior_appt_ts IS NOT NULL
             AND DATE_DIFF(c.call_date_local, DATE(pa.last_prior_appt_ts), DAY) > 730) AS appt_deep_dormant,
            (pa.last_prior_appt_ts IS NULL) AS appt_none,
            c.tx_existing, c.looking_to_book, c.appt_booked
          FROM c
          LEFT JOIN prior_appt pa USING (complete_call_id)
        )
    """


# Bucket predicates over the shared ``tagged`` CTE — used by BOTH the funnel and
# the per-call sample lists so the counts and rows match exactly. Each predicate
# mirrors the mutually-exclusive segment of ``call_outcomes_funnel`` with the same
# name, so a call appears under exactly one bucket here.
_BUCKET_PREDICATE = {
    "booked": "genuine AND connected_raw AND reconciled",
    "existing_customer": "genuine AND connected_raw AND NOT reconciled AND NOT qualified AND existing",
    "qualified_no_conversion": "genuine AND connected_raw AND NOT reconciled AND qualified",
    # never_connected now = voicemail + hangup only (genuine requires a
    # transcript), so this drill-down no longer surfaces transcript-less calls.
    "never_connected": "genuine AND NOT connected_raw",
    "voicemail": "genuine AND NOT connected_raw AND is_voicemail",
    "hangup": "genuine AND NOT connected_raw AND is_hangup",
}


def call_outcomes_funnel(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
    match_days: int = CALL_BOOKING_MATCH_DAYS,
) -> dict[str, Any]:
    """Call-traffic outcome funnel for the window. Two dimensions:

    (1) Reachability partition (mutually exclusive, sums to total):
      total → (no_transcript + spam + wrong_number, filtered out) → genuine
            → missed (has transcript, no real conversation): voicemail + hangup
            → connected (real conversation)

    (2) Connected calls partitioned by PATIENT TYPE (mutually exclusive):
            → new                (no prior appointment, transcript not existing)
            → existing           → active (last appointment within 12 months)
                                 → lapsed/dormant (last appointment 12+ months ago,
                                   or existing with no prior appointment). Lapsed
                                   further splits: lapsing 12–24mo, deep_dormant
                                   24mo+, dormant_never (no prior appointment).

    ``booked`` is an ORTHOGONAL overlay on connected calls (NOT a bucket): a call
    reconciled to a ``PMS_Unified.Appointments`` row CREATED within ``match_days``
    on/after the call, credited to the most-recent such call per appointment. It
    is reported within each patient-type segment — ``booked_new`` and
    ``booked_existing`` (they sum to ``booked``) — so bookings by existing
    patients are surfaced, not hidden.

    ``no_transcript`` = calls with no callscoring row; they sit beside spam /
    wrong_number, NOT under genuine. Level-1 partition:
    no_transcript + spam + wrong_number + genuine = total;
    genuine = voicemail + hangup + connected;
    connected = new + existing; existing = active + lapsed.
    ``qualified_not_booked`` / ``other`` are the no-booking split of NEW callers,
    retained for the new-patient leak view. Empty Invoca list → all zeros."""
    w = _win(window, days)
    out = {
        "total": 0, "no_transcript": 0, "spam": 0, "wrong_number": 0, "genuine": 0,
        "missed": 0, "voicemail": 0, "hangup": 0,
        "connected": 0, "connected_new": 0, "connected_existing": 0,
        "booked": 0, "booked_new": 0, "booked_existing": 0,
        "booked_existing_active": 0, "booked_existing_lapsing": 0,
        "booked_existing_deep_dormant": 0, "booked_existing_never": 0,
        "existing_patient": 0, "existing_active": 0, "existing_lapsed": 0,
        "existing_lapsing": 0, "existing_deep_dormant": 0, "existing_dormant_never": 0,
        "qualified_not_booked": 0, "other": 0, "booked_rate": None,
        "booked_method": "pms_reconciled", "match_days": int(match_days),
    }
    if not invoca_campaign_ids:
        return out
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    # Funnel is strictly nested (so the visual differences are exact):
    #   total     = no_transcript + spam + wrong_number + genuine
    #   genuine   = connected + never-connected(missed); missed = voicemail + hangup
    #   connected = booked + existing_patient + qualified_not_booked + other
    #     where existing_patient == connected_existing (existing-patient activity:
    #     reschedules / pickups / service — NOT a new booking).
    # ``booked`` = distinct PMS booking on a genuine connected call, credited to
    # the most-recent such call per appointment (orthogonal to patient type;
    # booked_new + booked_existing == booked). ``qualified``/``other`` are the
    # no-booking split of NEW callers.
    sql = _call_tagging_cte(in_iv, w) + """
            SELECT
              COUNT(*)                                                              AS total,
              COUNTIF(no_transcript)                                                AS no_transcript,
              COUNTIF(spam)                                                         AS spam,
              COUNTIF(is_wrong)                                                     AS wrong_number,
              COUNTIF(genuine)                                                      AS genuine,
              COUNTIF(genuine AND connected_raw)                                    AS connected,
              COUNTIF(genuine AND connected_raw AND NOT existing)                   AS connected_new,
              COUNTIF(genuine AND connected_raw AND existing)                       AS connected_existing,
              COUNTIF(genuine AND NOT connected_raw)                                AS missed,
              COUNTIF(genuine AND NOT connected_raw AND is_voicemail)               AS voicemail,
              COUNTIF(genuine AND NOT connected_raw AND is_hangup)                  AS hangup,
              -- Booked overlay (orthogonal to patient type).
              COUNTIF(genuine AND connected_raw AND reconciled)                     AS booked,
              COUNTIF(genuine AND connected_raw AND reconciled AND NOT existing)     AS booked_new,
              COUNTIF(genuine AND connected_raw AND reconciled AND existing)         AS booked_existing,
              -- WHO among existing patients is booking, by recency of last visit:
              -- active (coming anyway) vs dormant/reactivated (ad pulled them back).
              -- These four sum to ``booked_existing``.
              COUNTIF(genuine AND connected_raw AND reconciled AND existing AND appt_active)       AS booked_existing_active,
              COUNTIF(genuine AND connected_raw AND reconciled AND existing AND appt_lapsing)      AS booked_existing_lapsing,
              COUNTIF(genuine AND connected_raw AND reconciled AND existing AND appt_deep_dormant) AS booked_existing_deep_dormant,
              COUNTIF(genuine AND connected_raw AND reconciled AND existing AND appt_none)         AS booked_existing_never,
              -- Patient-type partition of connected (existing → active / lapsed).
              COUNTIF(genuine AND connected_raw AND existing)                        AS existing_patient,
              -- Recency split of existing (appointment-only, 12-month recall cut).
              -- active + lapsed == existing_patient; lapsed = lapsing + deep_dormant
              -- + dormant_never (existing patients with no prior appointment).
              COUNTIF(genuine AND connected_raw AND existing AND appt_active)         AS existing_active,
              COUNTIF(genuine AND connected_raw AND existing AND NOT appt_active)     AS existing_lapsed,
              COUNTIF(genuine AND connected_raw AND existing AND appt_lapsing)        AS existing_lapsing,
              COUNTIF(genuine AND connected_raw AND existing AND appt_deep_dormant)   AS existing_deep_dormant,
              COUNTIF(genuine AND connected_raw AND existing AND appt_none)           AS existing_dormant_never,
              COUNTIF(genuine AND connected_raw AND NOT reconciled AND NOT existing AND looking_to_book)     AS qualified_not_booked,
              COUNTIF(genuine AND connected_raw AND NOT reconciled AND NOT existing AND NOT looking_to_book) AS other
            FROM tagged
    """
    try:
        rows = list(_client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ScalarQueryParameter("match_days", "INT64", int(match_days)),
        ])).result())
    except Exception as exc:
        log.warning("call_outcomes_funnel failed clinic=%s: %s", clinic_id, exc)
        return out
    if rows:
        r = rows[0]
        out["total"] = int(r.total or 0)
        out["no_transcript"] = int(r.no_transcript or 0)
        out["spam"] = int(r.spam or 0)
        out["wrong_number"] = int(r.wrong_number or 0)
        out["genuine"] = int(r.genuine or 0)
        out["booked"] = int(r.booked or 0)
        out["booked_new"] = int(r.booked_new or 0)
        out["booked_existing"] = int(r.booked_existing or 0)
        out["booked_existing_active"] = int(r.booked_existing_active or 0)
        out["booked_existing_lapsing"] = int(r.booked_existing_lapsing or 0)
        out["booked_existing_deep_dormant"] = int(r.booked_existing_deep_dormant or 0)
        out["booked_existing_never"] = int(r.booked_existing_never or 0)
        out["connected"] = int(r.connected or 0)
        out["connected_new"] = int(r.connected_new or 0)
        out["connected_existing"] = int(r.connected_existing or 0)
        out["missed"] = int(r.missed or 0)
        out["voicemail"] = int(r.voicemail or 0)
        out["hangup"] = int(r.hangup or 0)
        out["existing_patient"] = int(r.existing_patient or 0)
        out["existing_active"] = int(r.existing_active or 0)
        out["existing_lapsed"] = int(r.existing_lapsed or 0)
        out["existing_lapsing"] = int(r.existing_lapsing or 0)
        out["existing_deep_dormant"] = int(r.existing_deep_dormant or 0)
        out["existing_dormant_never"] = int(r.existing_dormant_never or 0)
        out["qualified_not_booked"] = int(r.qualified_not_booked or 0)
        out["other"] = int(r.other or 0)
        out["booked_rate"] = (out["booked"] / out["connected"]) if out["connected"] else None
    return out


def call_funnel_matthew_split(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
    match_days: int = CALL_BOOKING_MATCH_DAYS,
) -> dict[str, int] | None:
    """For each CONNECTED funnel bucket, how many calls were answered by Matthew
    (the AI receptionist) rather than clinic staff. Reuses the funnel's shared
    tagging CTE and joins ``ClinicData.matthew_calls`` by ``complete_call_id``,
    so the per-bucket splits reconcile exactly with ``call_outcomes_funnel``.

    Returns None when there is no Matthew signal — the ``matthew_calls`` table is
    absent (clinics without the AI receptionist) or no connected call was
    Matthew-answered — so the caller can omit the split. Kept SEPARATE from
    ``call_outcomes_funnel`` on purpose: a missing ``matthew_calls`` table must
    not zero out the main funnel."""
    w = _win(window, days)
    if not invoca_campaign_ids:
        return None
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    # Matthew membership as a set test in the SELECT (not a CTE join): it keeps
    # the shared tagging CTE untouched and confines the matthew_calls reference
    # to this one query, whose own try/except isolates a missing-table failure.
    mx = (
        f"complete_call_id IN (SELECT complete_call_id "
        f"FROM `{_CLINIC_DATA}.matthew_calls` WHERE answered_by_matthew)"
    )
    sql = _call_tagging_cte(in_iv, w) + f"""
            SELECT
              COUNTIF(genuine AND connected_raw AND {mx})                                                       AS connected,
              COUNTIF(genuine AND connected_raw AND reconciled AND {mx})                                        AS booked,
              COUNTIF(genuine AND connected_raw AND NOT reconciled AND qualified AND {mx})                      AS qualified_not_booked,
              COUNTIF(genuine AND connected_raw AND NOT reconciled AND NOT qualified AND existing AND {mx})     AS existing_customer,
              COUNTIF(genuine AND connected_raw AND NOT reconciled AND NOT qualified AND NOT existing AND {mx}) AS other
            FROM tagged
    """
    try:
        rows = list(_client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ScalarQueryParameter("match_days", "INT64", int(match_days)),
        ])).result())
    except Exception as exc:
        # info, not warning: a missing matthew_calls table is expected for
        # clinics without the AI receptionist and is not an error condition.
        log.info("call_funnel_matthew_split skipped clinic=%s: %s", clinic_id, exc)
        return None
    if not rows:
        return None
    r = rows[0]
    split = {
        "connected": int(r.connected or 0),
        "booked": int(r.booked or 0),
        "qualified_not_booked": int(r.qualified_not_booked or 0),
        "existing_customer": int(r.existing_customer or 0),
        "other": int(r.other or 0),
    }
    # No Matthew-answered connected calls → nothing to split; omit the section.
    return split if split["connected"] > 0 else None


def connected_outcomes_by_month(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
    match_days: int = CALL_BOOKING_MATCH_DAYS,
) -> list[dict]:
    """Month-over-month distribution of CONNECTED-call outcomes: per month, the
    split of connected calls into booked / qualified_not_booked / existing_customer
    / other. The trend is pinned to start at ``MIN_WINDOW_DATE`` (Dec 4 2025) and
    runs monthly through the selected window's end. Built on the SAME shared
    tagging CTE as the funnel, so each month's segments sum to that month's
    connected calls. Empty for no Invoca campaigns."""
    w = _win(window, days)
    end_incl = w.end_excl - _dt.timedelta(days=1)
    if not invoca_campaign_ids or end_incl < MIN_WINDOW_DATE:
        return []
    # Pin the start of the trend to the data floor, regardless of the selected
    # window's start; the end follows the selected range.
    span = Window(MIN_WINDOW_DATE.isoformat(), end_incl.isoformat())
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    sql = _call_tagging_cte(in_iv, span) + """
            SELECT
              FORMAT_DATE('%Y-%m', DATE(TIMESTAMP_TRUNC(call_ts, MONTH))) AS month,
              COUNTIF(reconciled)                                    AS booked,
              COUNTIF(NOT reconciled AND qualified)                  AS qualified_not_booked,
              COUNTIF(NOT reconciled AND NOT qualified AND existing) AS existing_customer,
              COUNTIF(NOT reconciled AND NOT qualified AND NOT existing) AS other,
              COUNT(*)                                               AS connected
            FROM tagged
            WHERE genuine AND connected_raw
            GROUP BY month
            ORDER BY month
    """
    try:
        rows = list(_client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ScalarQueryParameter("match_days", "INT64", int(match_days)),
        ])).result())
    except Exception as exc:
        log.warning("connected_outcomes_by_month failed clinic=%s: %s", clinic_id, exc)
        return []
    return [{
        "month": r.month,
        "booked": int(r.booked or 0),
        "qualified_not_booked": int(r.qualified_not_booked or 0),
        "existing_customer": int(r.existing_customer or 0),
        "other": int(r.other or 0),
        "connected": int(r.connected or 0),
    } for r in rows]


def pipeline_revenue_by_month(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> list[dict]:
    """Month-over-month revenue the marketing pipeline is responsible for, pinned
    to start Dec 2025.

    A patient is "pipeline-touched" when their phone matched a tracked inbound
    call (Invoca) OR they have a ``Referral - Zoolstra`` CounselEar appointment.
    All their invoices in the window are summed by INVOICE month (any date — no
    on/after-touch gate), from Dec 2025 through the selected window's end;
    invoices are deduped by ``order_id``. Empty when the clinic has no pipeline
    sources."""
    w = _win(window, days)
    end_incl = w.end_excl - _dt.timedelta(days=1)
    if end_incl < MIN_WINDOW_DATE:
        return []
    # Both the touches and the invoice buckets span Dec 2025 → window end.
    touch = Window(MIN_WINDOW_DATE.isoformat(), end_incl.isoformat())
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")" if invoca_campaign_ids else "('')"
    sql = f"""
        WITH call_touch AS (
          SELECT pc.client_id AS client_id, MIN(DATE(t.timestamp)) AS touch_date
          FROM `{_CLINIC_DATA}.transactions` t
          JOIN `{_PATIENT_CONTACTS}` pc
            ON pc._clinic_id = @clinic_id AND LENGTH(pc.phone_norm) = 10
           AND pc.phone_norm = RIGHT(REGEXP_REPLACE(IFNULL(t.calling_phone_number, ''), r'\\D', ''), 10)
          WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
            AND {_ts_between('t.timestamp', touch)}
          GROUP BY client_id
        ),
        zoolstra_touch AS (
          SELECT CAST(patient_id AS STRING) AS client_id, MIN(appt_date) AS touch_date
          FROM `{_COUNSELEAR}.appointments`
          WHERE _clinic_id = @clinic_id AND appt_referral_type = @ztag AND patient_id IS NOT NULL
            AND {_date_between('appt_date', touch)}
          GROUP BY client_id
        ),
        touched AS (
          SELECT client_id, MIN(touch_date) AS touch_date
          FROM (SELECT * FROM call_touch UNION ALL SELECT * FROM zoolstra_touch)
          GROUP BY client_id
        ),
        inv AS (
          SELECT im.order_id,
                 SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date) AS inv_date,
                 MAX(SAFE_CAST(im.order_total_with_tax AS NUMERIC)) AS amt
          FROM `{_BP}.InvoiceMaster` im
          JOIN touched tt ON CAST(im.client_id AS STRING) = tt.client_id
          WHERE im._clinic_id = @clinic_id
            AND SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
          GROUP BY im.order_id, inv_date
        )
        SELECT
          FORMAT_DATE('%Y-%m', DATE_TRUNC(inv_date, MONTH)) AS month,
          SUM(amt)                 AS revenue,
          COUNT(DISTINCT order_id) AS invoices
        FROM inv
        WHERE {_date_between('inv_date', touch)}
        GROUP BY month
        ORDER BY month
    """
    try:
        rows = list(_client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ScalarQueryParameter("ztag", "STRING", ZOOLSTRA_REFERRAL_TAG),
        ])).result())
    except Exception as exc:
        log.warning("pipeline_revenue_by_month failed clinic=%s: %s", clinic_id, exc)
        return []
    return [{"month": r.month, "revenue": float(r.revenue or 0.0), "invoices": int(r.invoices or 0)} for r in rows]


# Acquisition channels Cortex can bring a patient through, in the order the
# breakdown lists them. ``customerio`` is a live seam, not a placeholder label —
# see the "Customer.io" paragraph in pipeline_revenue_by_source.
REVENUE_CHANNELS = ("call", "form", "portal", "customerio")

# Source label for the two channels that carry no campaign tagging of their own.
_PORTAL_SOURCE = "counselear portal referral"
_CIO_SOURCE = "customer.io campaign"


def pipeline_revenue_by_source(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
    customerio_touches: "Sequence[tuple[str, _dt.datetime]] | None" = None,
) -> dict[str, Any]:
    """Total revenue Cortex has led to the clinic in the window, split by
    ``utm_source`` and by acquisition channel.

    This is the "Revenue attributed" figure on the growth dashboard, and it is
    meant to be the WHOLE of what Cortex drove — every channel it can bring a
    patient through, counted once:

    * ``call``       — a tracked inbound call (Invoca), phone-matched to a patient
    * ``form``       — a web-form submission, matched on phone last-10 OR email
    * ``portal``     — a ``Referral - Zoolstra`` CounselEar appointment
    * ``customerio`` — a reactivation campaign send (seam; see below)

    Revenue rule, unchanged: positive ``InvoiceMaster`` rows, deduped on
    ``order_id``, invoice date inside the selected window.

    The two windows differ, intentionally:

    * **touch** window is Dec 2025 → window end. A patient who called in March
      and was invoiced in August is August revenue; requiring the touch inside a
      7-day range would report ~nothing.
    * **invoice** window is the selected range — what "in this range" means.

    **Every patient is credited to their FIRST touch across ALL channels, and
    only that one.** Two things depend on this. It makes ``by_source`` and
    ``by_channel`` genuine partitions that sum exactly to ``revenue``, so either
    can honestly be drawn as parts of a whole. And it is what stops the total
    double-counting: before the form channel was folded in here, forms were
    credited separately by :func:`webform_revenue` over the same invoices, so
    adding that figure to this one counted every patient who both called and
    submitted twice. That defect is §5 of
    ``resources/form-revenue-attribution-plan.md``; folding the channel in here
    is its fix. ``webform_revenue`` is left alone and still answers its own
    narrower question — **do not add the two together.**

    Ordering is on the full touch TIMESTAMP rather than the date, because
    same-day ties land on exactly the patients most likely to have both a call
    and a form. The portal leg is the one channel with no clock —
    ``CounselEar_PHI.appointments`` carries ``appt_date`` only — so it is ordered
    at midnight and loses same-day ties to a timestamped call or form. That is
    the correct bias: a dated-only booking is weaker evidence of first contact
    than a timestamped enquiry. Remaining ties break on channel then source, so
    the split is deterministic run to run.

    Source labels. Calls and forms use their own ``utm_source``, rolled up to
    ``direct / untagged`` when NULL, blank or noise (``nan``/``null``/``none``) —
    the same label :func:`webform_sources` uses, so the vocabulary matches across
    the app. The form leg deliberately does NOT fall back to
    ``webforms.referrer_host``: a referrer is not a campaign source, and §14a of
    the methodology contract is explicit that presenting one as the other is a
    mislabel. Portal and Customer.io are named rather than folded into
    ``direct / untagged``, because both are real revenue with a knowable origin
    and hiding them inside "direct" would misattribute it.

    **Customer.io is wired but not fed.** ``customerio_touches`` takes
    ``(client_id, sent_at)`` pairs and folds them in as a fourth channel on equal
    terms with the rest. Nothing passes it yet: the enrollment log lives in Cloud
    SQL (``customerio_enrollments``, keyed on the same PMS ``client_id`` this
    query joins on) while this runs in BigQuery, so lighting it up is a matter of
    the CALLER reading that table and handing the pairs over — not of changing
    this SQL. Until then the channel is reported at zero, which is deliberate:
    the response shape does not change on the day it starts contributing.

    Correlational, not causal — it credits a channel whose patient later
    transacted, not proof the channel caused it.
    """
    w = _win(window, days)
    end_incl = w.end_excl - _dt.timedelta(days=1)
    out: dict[str, Any] = {
        "revenue": 0.0, "invoices": 0, "patients": 0,
        "by_source": [],
        "by_channel": [{"channel": c, "revenue": 0.0, "invoices": 0, "patients": 0}
                       for c in REVENUE_CHANNELS],
    }
    if end_incl < MIN_WINDOW_DATE:
        return out
    touch = Window(MIN_WINDOW_DATE.isoformat(), end_incl.isoformat())
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")" if invoca_campaign_ids else "('')"
    noise = ", ".join(f"'{n}'" for n in _UTM_NOISE)

    # The Customer.io leg is a typed empty relation when no touches are supplied,
    # so the UNION ALL keeps one shape whether or not the channel is fed. Same
    # technique as the rc_call_facts clinic seam: absent, not special-cased.
    cio_params: list[Any] = []
    if customerio_touches:
        cio_cte = f"""
          SELECT client_id, touch_ts, 'customerio' AS channel, '{_CIO_SOURCE}' AS source
          FROM UNNEST(@cio_touches)
        """
        cio_params.append(bigquery.ArrayQueryParameter(
            "cio_touches", "RECORD",
            [bigquery.StructQueryParameter(
                "", bigquery.ScalarQueryParameter("client_id", "STRING", str(cid)),
                bigquery.ScalarQueryParameter("touch_ts", "TIMESTAMP", ts))
             for cid, ts in customerio_touches],
        ))
    else:
        # An empty TYPED array rather than `SELECT NULL … WHERE FALSE`: BigQuery
        # rejects a WHERE clause on a query with no FROM, and this shape is also
        # structurally identical to the fed branch above, so the UNION ALL sees
        # the same columns and types either way.
        cio_cte = f"""
          SELECT client_id, touch_ts, 'customerio' AS channel, '{_CIO_SOURCE}' AS source
          FROM UNNEST(ARRAY<STRUCT<client_id STRING, touch_ts TIMESTAMP>>[])
        """

    sql = f"""
        WITH call_touch AS (
          SELECT pc.client_id AS client_id,
                 t.timestamp  AS touch_ts,
                 'call'       AS channel,
                 IF(t.utm_source IS NULL
                      OR LOWER(TRIM(t.utm_source)) IN ({noise}),
                    'direct / untagged',
                    LOWER(TRIM(t.utm_source))) AS source
          FROM `{_CLINIC_DATA}.transactions` t
          JOIN `{_PATIENT_CONTACTS}` pc
            ON pc._clinic_id = @clinic_id AND LENGTH(pc.phone_norm) = 10
           AND pc.phone_norm = RIGHT(REGEXP_REPLACE(IFNULL(t.calling_phone_number, ''), r'\\D', ''), 10)
          WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
            AND {_ts_between('t.timestamp', touch)}
        ),
        -- Same submitter→patient rule as webform_revenue (phone last-10 OR
        -- email), so the two readers cannot disagree about who is a form lead.
        form_src AS (
          SELECT RIGHT(REGEXP_REPLACE(IFNULL(phone_number, ''), r'\\D', ''), 10) AS phone_norm,
                 LOWER(TRIM(IFNULL(email, '')))                                  AS email_norm,
                 submitted_at,
                 IF(utm_source IS NULL
                      OR LOWER(TRIM(utm_source)) IN ({noise}),
                    'direct / untagged',
                    LOWER(TRIM(utm_source))) AS source
          FROM `{_CLINIC_DATA}.webforms`
          WHERE clinic_id = @clinic_id
            AND {_ts_between('submitted_at', touch)}
        ),
        form_touch AS (
          SELECT p.client_id AS client_id,
                 f.submitted_at AS touch_ts,
                 'form'         AS channel,
                 f.source       AS source
          FROM form_src f
          JOIN (SELECT DISTINCT client_id, phone_norm, email_norm
                FROM `{_PATIENT_CONTACTS}` WHERE _clinic_id = @clinic_id) p
            ON (LENGTH(f.phone_norm) = 10 AND f.phone_norm = p.phone_norm)
            OR (f.email_norm != ''        AND f.email_norm = p.email_norm)
        ),
        portal_touch AS (
          SELECT CAST(patient_id AS STRING) AS client_id,
                 TIMESTAMP(appt_date)       AS touch_ts,
                 'portal'                   AS channel,
                 '{_PORTAL_SOURCE}'         AS source
          FROM `{_COUNSELEAR}.appointments`
          WHERE _clinic_id = @clinic_id AND appt_referral_type = @ztag
            AND patient_id IS NOT NULL
            AND {_date_between('appt_date', touch)}
        ),
        customerio_touch AS ({cio_cte}),
        touches AS (
          SELECT * FROM call_touch
          UNION ALL SELECT * FROM form_touch
          UNION ALL SELECT * FROM portal_touch
          UNION ALL SELECT * FROM customerio_touch
        ),
        -- One row per patient: the source AND channel of their earliest touch.
        -- This is the step that makes both breakdowns partitions rather than
        -- overlapping tallies.
        first_touch AS (
          SELECT client_id,
                 ARRAY_AGG(STRUCT(source, channel)
                           ORDER BY touch_ts, channel, source LIMIT 1)[OFFSET(0)] AS t
          FROM touches
          WHERE client_id IS NOT NULL
          GROUP BY client_id
        ),
        inv AS (
          SELECT im.order_id,
                 ANY_VALUE(ft.t.source)                             AS source,
                 ANY_VALUE(ft.t.channel)                            AS channel,
                 ANY_VALUE(CAST(im.client_id AS STRING))            AS client_id,
                 MAX(SAFE_CAST(im.order_total_with_tax AS NUMERIC)) AS amt
          FROM `{_BP}.InvoiceMaster` im
          JOIN first_touch ft ON CAST(im.client_id AS STRING) = ft.client_id
          WHERE im._clinic_id = @clinic_id
            AND SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
            AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)", w)}
          GROUP BY im.order_id
        )
        -- Grouped by BOTH so the two breakdowns are folded from one row set in
        -- Python. Querying them separately would let the source split and the
        -- channel split disagree about the total they are each parts of.
        SELECT source, channel,
               SUM(amt)                  AS revenue,
               COUNT(DISTINCT order_id)  AS invoices,
               COUNT(DISTINCT client_id) AS patients
        FROM inv
        GROUP BY source, channel
    """
    params = [
        bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
        bigquery.ScalarQueryParameter("ztag", "STRING", ZOOLSTRA_REFERRAL_TAG),
        *cio_params,
    ]
    try:
        rows = list(_client().query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())
    except Exception as exc:
        log.warning("pipeline_revenue_by_source failed clinic=%s: %s", clinic_id, exc)
        return out

    # An order belongs to one patient, who has exactly one first touch, so every
    # order lands in exactly one (source, channel) group — which is what makes
    # summing these counts across groups safe rather than double-counting.
    def _fold(rows_, key: str, seed: tuple[str, ...] = ()) -> list[dict]:
        acc: dict[str, dict] = {
            s: {key: s, "revenue": 0.0, "invoices": 0, "patients": 0} for s in seed
        }
        for r in rows_:
            k = getattr(r, key)
            slot = acc.setdefault(k, {key: k, "revenue": 0.0, "invoices": 0, "patients": 0})
            slot["revenue"] += float(r.revenue or 0.0)
            slot["invoices"] += int(r.invoices or 0)
            slot["patients"] += int(r.patients or 0)
        return sorted(acc.values(), key=lambda d: (-d["revenue"], d[key]))

    by_source = _fold(rows, "source")
    # Seeded with every channel so one that contributed nothing reports zero
    # rather than vanishing — an absent channel reads as "not configured".
    by_channel = _fold(rows, "channel", REVENUE_CHANNELS)

    # Totals are summed from the breakdown rather than queried separately, so the
    # headline figure and the charts drawn under it cannot drift apart.
    out["by_source"] = by_source
    out["by_channel"] = by_channel
    out["revenue"] = sum(s["revenue"] for s in by_source)
    out["invoices"] = sum(s["invoices"] for s in by_source)
    out["patients"] = sum(s["patients"] for s in by_source)
    return out


def form_submission_outcomes(clinic_id: str, days: int = 90,
                             window: "Window | None" = None) -> dict[str, Any]:
    """Online form submissions and their outcomes for the window.

    For Virsono, web-form submissions are self-service bookings through the
    embedded CounselEar portal — they land as appointments tagged
    ``appt_referral_type = "Referral - Zoolstra"``. This returns the submission
    count and how those appointments resolved (attended / upcoming / cancelled /
    no-show), plus how many converted to a paid invoice and the revenue (invoices
    dated within the window). Returns zeros (never raises) for clinics with no
    such bookings."""
    w = _win(window, days)
    out = {"submissions": 0, "patients": 0, "attended": 0, "upcoming": 0,
           "cancelled": 0, "no_show": 0, "converted_patients": 0, "revenue": 0.0}
    sql = f"""
        WITH subs AS (
          SELECT appt_id, CAST(patient_id AS STRING) AS patient_id, LOWER(status) AS status
          FROM `{_COUNSELEAR}.appointments`
          WHERE _clinic_id = @clinic_id AND appt_referral_type = @ztag
            AND {_date_between('appt_date', w)}
        ),
        agg AS (
          SELECT
            COUNT(*)                                                          AS submissions,
            COUNT(DISTINCT patient_id)                                        AS patients,
            COUNTIF(status IN ('completed', 'arrived'))                       AS attended,
            COUNTIF(status LIKE 'cancel%')                                    AS cancelled,
            COUNTIF(status LIKE 'no show%')                                   AS no_show,
            COUNTIF(status NOT IN ('completed', 'arrived')
                    AND status NOT LIKE 'cancel%' AND status NOT LIKE 'no show%') AS upcoming
          FROM subs
        ),
        rev AS (
          -- Invoices for the submitter, scoped to the report window (any date in
          -- window — no on/after gate), consistent with the other revenue paths.
          SELECT COUNT(DISTINCT s.patient_id) AS converted_patients,
                 SUM(SAFE_CAST(i.total_cost AS NUMERIC)) AS revenue
          FROM subs s
          JOIN `{_COUNSELEAR}.invoices` i
            ON i._clinic_id = @clinic_id AND CAST(i.patient_id AS STRING) = s.patient_id
           AND SAFE_CAST(i.total_cost AS NUMERIC) > 0
           AND {_date_between('i.invoice_date', w)}
        )
        SELECT agg.*, rev.converted_patients, rev.revenue
        FROM agg CROSS JOIN rev
    """
    try:
        rows = list(_client().query(sql, job_config=bigquery.QueryJobConfig(
            query_parameters=_params(clinic_id, ztag=ZOOLSTRA_REFERRAL_TAG))).result())
    except Exception as exc:
        log.warning("form_submission_outcomes failed clinic=%s: %s", clinic_id, exc)
        return out
    if rows:
        r = rows[0]
        out["submissions"] = int(r.submissions or 0)
        out["patients"] = int(r.patients or 0)
        out["attended"] = int(r.attended or 0)
        out["upcoming"] = int(r.upcoming or 0)
        out["cancelled"] = int(r.cancelled or 0)
        out["no_show"] = int(r.no_show or 0)
        out["converted_patients"] = int(r.converted_patients or 0)
        out["revenue"] = float(r.revenue or 0.0)
    return out


MATTHEW_CALLS_TABLE = f"{_CLINIC_DATA}.matthew_calls"
# How many days the CounselEar booking may be created from the call and still
# count as "the booking landed" (Matthew enters it during/just after the call).
MATTHEW_MATCH_DAYS = 3


def matthew_outcomes(clinic_id: str, invoca_campaign_ids: list[str], days: int = 90,
                     window: "Window | None" = None,
                     match_days: int = CALL_BOOKING_MATCH_DAYS) -> dict[str, Any]:
    """Outcomes of calls handled by the Matthew AI receptionist (Virsono).

    Built on the SAME shared call-tagging CTE (:func:`_call_tagging_cte`) as the
    call funnel and the "Booked — what was said" drawer, then intersected with
    ``ClinicData.matthew_calls`` (``answered_by_matthew = TRUE``). This guarantees
    every number here reconciles with the funnel/drawer — same call set (the
    clinic's Invoca campaigns), same "handled by Matthew" (``answered_by_matthew``)
    and same "booked" (``genuine AND connected_raw AND reconciled``) definitions.

    ``answered`` — Matthew-handled calls in the funnel's call set.
    ``booked``  — of those, the ones in the funnel's booked bucket (caller
       reconciled to a PMS appointment). Identical, by construction, to the count
       of ``booked_by_matthew`` rows in the Booked drawer. We do NOT require the
       transcript ``appointment_confirmed`` flag — Matthew triages + transfers
       rather than confirming a date, so most booked calls read as
       ``engaged_no_conversion`` (requiring ``appointment_confirmed`` undercounted
       bookings ~90%).
    ``not_booked`` = ``answered − booked``.

    ``booked`` is further split — using the per-call reasoning (callscoring's
    ``appointment_booked``, i.e. "a NEW appointment was booked live during the
    call; confirming/rescheduling an existing one does not count") — into:

    ``booked_on_call``   — reconciled AND the transcript shows Matthew (or the
       staffer he transferred to, on the same call) booked a NEW appointment
       live. This is the tightest "what the AI actually converted on the call".
    ``booked_in_window`` — reconciled but NOT booked live on the call: the PMS
       appointment appeared within the 10-day match window without a live booking
       on the transcript (booked after the hand-off, or the caller already had
       an appointment). ``booked_on_call + booked_in_window = booked``.

    New-patient acquisition vs existing-patient service (``existing`` = the
    caller has an appointment dated before the call OR the transcript identifies
    them as a current patient):

    ``connected`` — genuine, connected Matthew calls (real conversations).
    ``connected_new`` / ``connected_existing`` — that set split by ``existing``;
       the existing share is the "service-rebooking line" character.
    ``booked_new_patient`` / ``booked_existing_patient`` — ``booked`` split by
       patient type (booked is orthogonal to type). They sum to ``booked``; the
       existing share is bookings from patients the clinic already had.

    The transcript-outcome breakdown (``hung_up_immediately`` /
    ``engaged_no_conversion`` / ``confirmed``) is descriptive colour;
    ``confirmed_not_landed`` (Matthew told the caller they're booked but the call
    is NOT in the booked bucket) is the write-failure signal worth surfacing.

    Returns zeros (never raises) for clinics with no Matthew/Invoca data.
    Aggregate-only.
    """
    w = _win(window, days)
    out = {"answered": 0, "booked": 0, "not_booked": 0,
           "booked_on_call": 0, "booked_in_window": 0,
           "connected": 0, "connected_new": 0, "connected_existing": 0,
           "booked_new_patient": 0, "booked_existing_patient": 0,
           "hung_up_immediately": 0, "engaged_no_conversion": 0,
           "confirmed": 0, "confirmed_not_landed": 0}
    if not invoca_campaign_ids:
        return out
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    sql = _call_tagging_cte(in_iv, w) + f""",
        mc AS (
          SELECT complete_call_id, ANY_VALUE(outcome) AS outcome
          FROM `{MATTHEW_CALLS_TABLE}`
          WHERE answered_by_matthew = TRUE AND clinic_id = @clinic_id
          GROUP BY complete_call_id
        ),
        matthew AS (
          -- Matthew-handled calls within the funnel's (campaign-scoped) call set,
          -- flagged with the funnel's own booked-bucket predicate.
          SELECT t.complete_call_id, mc.outcome, t.appt_booked, t.existing,
                 (t.genuine AND t.connected_raw)               AS connected,
                 (t.genuine AND t.connected_raw AND t.reconciled) AS booked
          FROM tagged t JOIN mc USING (complete_call_id)
        )
        SELECT
          COUNT(*)                                                    AS answered,
          COUNTIF(booked)                                             AS booked,
          -- Split of `booked` by the per-call reasoning: booked LIVE on the call
          -- (a new appointment was made during the call) vs merely reconciled to
          -- an appointment in the 10-day window without a live booking.
          COUNTIF(booked AND appt_booked)                             AS booked_on_call,
          COUNTIF(booked AND NOT appt_booked)                         AS booked_in_window,
          -- New-patient acquisition vs existing-patient service (PMS ground truth:
          -- existing = caller had an appointment dated before the call).
          COUNTIF(connected)                                          AS connected,
          COUNTIF(connected AND NOT existing)                         AS connected_new,
          COUNTIF(connected AND existing)                             AS connected_existing,
          -- Booked split by patient type (booked is orthogonal to type).
          COUNTIF(booked AND NOT existing)                            AS booked_new_patient,
          COUNTIF(booked AND existing)                                AS booked_existing_patient,
          COUNTIF(outcome = 'hung_up_immediately')                    AS hung_up_immediately,
          COUNTIF(outcome = 'engaged_no_conversion')                  AS engaged_no_conversion,
          COUNTIF(outcome = 'appointment_confirmed')                  AS confirmed,
          COUNTIF(outcome = 'appointment_confirmed' AND NOT booked)   AS confirmed_not_landed
        FROM matthew
    """
    try:
        rows = list(_client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ScalarQueryParameter("match_days", "INT64", int(match_days)),
        ])).result())
    except Exception as exc:
        log.warning("matthew_outcomes failed clinic=%s: %s", clinic_id, exc)
        return out
    if rows:
        r = rows[0]
        out["answered"] = int(r.answered or 0)
        out["booked"] = int(r.booked or 0)
        out["booked_on_call"] = int(r.booked_on_call or 0)
        out["booked_in_window"] = int(r.booked_in_window or 0)
        out["connected"] = int(r.connected or 0)
        out["connected_new"] = int(r.connected_new or 0)
        out["connected_existing"] = int(r.connected_existing or 0)
        out["booked_new_patient"] = int(r.booked_new_patient or 0)
        out["booked_existing_patient"] = int(r.booked_existing_patient or 0)
        out["hung_up_immediately"] = int(r.hung_up_immediately or 0)
        out["engaged_no_conversion"] = int(r.engaged_no_conversion or 0)
        out["confirmed"] = int(r.confirmed or 0)
        out["confirmed_not_landed"] = int(r.confirmed_not_landed or 0)
        out["not_booked"] = max(out["answered"] - out["booked"], 0)
    return out


def matthew_outcomes_by_month(clinic_ids: list[str], days: int = 90,
                              window: "Window | None" = None) -> list[dict]:
    """Month-over-month Matthew call outcomes: per month, answered calls split
    into appointment_confirmed / engaged_no_conversion / hung_up_immediately.
    Pinned to start Dec 2025 (like ``connected_outcomes_by_month``); deduped by
    complete_call_id via the transactions timestamp join. Empty for no clinics."""
    w = _win(window, days)
    end_incl = w.end_excl - _dt.timedelta(days=1)
    if not clinic_ids or end_incl < MIN_WINDOW_DATE:
        return []
    span = Window(MIN_WINDOW_DATE.isoformat(), end_incl.isoformat())
    sql = f"""
        WITH mc AS (
          SELECT complete_call_id, ANY_VALUE(outcome) AS outcome
          FROM `{MATTHEW_CALLS_TABLE}`
          WHERE answered_by_matthew = TRUE AND clinic_id IN UNNEST(@clinic_ids)
          GROUP BY complete_call_id
        ),
        ts AS (
          SELECT complete_call_id, MIN(timestamp) AS call_ts
          FROM `{_CLINIC_DATA}.transactions`
          WHERE {_ts_between('timestamp', span)}
          GROUP BY complete_call_id
        ),
        m AS (SELECT mc.outcome, ts.call_ts FROM mc JOIN ts USING (complete_call_id))
        SELECT
          FORMAT_DATE('%Y-%m', DATE(TIMESTAMP_TRUNC(call_ts, MONTH))) AS month,
          COUNTIF(outcome = 'appointment_confirmed') AS appointment_confirmed,
          COUNTIF(outcome = 'engaged_no_conversion') AS engaged_no_conversion,
          COUNTIF(outcome = 'hung_up_immediately')   AS hung_up_immediately,
          COUNT(*)                                   AS answered
        FROM m
        GROUP BY month
        ORDER BY month
    """
    try:
        rows = list(_client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("clinic_ids", "STRING", list(clinic_ids)),
        ])).result())
    except Exception as exc:
        log.warning("matthew_outcomes_by_month failed clinics=%s: %s", clinic_ids, exc)
        return []
    return [{
        "month": r.month,
        "appointment_confirmed": int(r.appointment_confirmed or 0),
        "engaged_no_conversion": int(r.engaged_no_conversion or 0),
        "hung_up_immediately": int(r.hung_up_immediately or 0),
        "answered": int(r.answered or 0),
    } for r in rows]


def matthew_leak_calls(clinic_id: str, invoca_campaign_ids: list[str], window: "Window",
                       limit: int = 500,
                       match_days: int = CALL_BOOKING_MATCH_DAYS) -> list[dict]:
    """Per-call detail for Matthew's NON-CONVERTED calls — the appendix drill-down
    counterpart to :func:`leak_calls`, returned in the same row shape (+``outcome``).

    A Matthew-handled call counts as *booked* when the caller reconciles to a PMS
    appointment created within ``match_days`` on/after the call — the SAME
    reconciliation as :func:`matthew_outcomes` and the call funnel (regardless of
    transcript outcome, since Matthew triages + transfers rather than confirming a
    date). Everything else Matthew answered is non-converted and listed here:
    hang-ups, engaged-but-no-booking, and ``appointment_confirmed`` calls whose
    booking never reached the PMS. This is exactly ``answered − booked`` from
    :func:`matthew_outcomes`, so the counts reconcile.

    Each row: ``complete_call_id`` (→ transcript), ``date`` (local), ``phone``,
    ``reasoning``, ``outcome`` (human label). PHI — gated + audited at the endpoint.
    Empty for no clinics."""
    if not invoca_campaign_ids:
        return []
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    # Same shared tagging CTE as the funnel + drawer, intersected with Matthew's
    # handled calls; list the ones NOT in the funnel's booked bucket. This is
    # exactly ``answered − booked`` from matthew_outcomes, so the counts reconcile.
    sql = _call_tagging_cte(in_iv, window) + f""",
        mc AS (
          SELECT complete_call_id,
                 ANY_VALUE(outcome)   AS outcome,
                 ANY_VALUE(reasoning) AS reasoning
          FROM `{MATTHEW_CALLS_TABLE}`
          WHERE answered_by_matthew = TRUE AND clinic_id = @clinic_id
          GROUP BY complete_call_id
        )
        SELECT t.complete_call_id, t.start_time_local, t.phone_raw,
               mc.reasoning, mc.outcome
        FROM tagged t JOIN mc USING (complete_call_id)
        WHERE NOT (t.genuine AND t.connected_raw AND t.reconciled)
        ORDER BY t.call_ts DESC
        LIMIT @limit
    """
    try:
        rows = list(_client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ScalarQueryParameter("match_days", "INT64", int(match_days)),
            bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
        ])).result())
    except Exception as exc:
        log.warning("matthew_leak_calls failed clinic=%s: %s", clinic_id, exc)
        return []
    labels = {
        "hung_up_immediately":   "Hung up immediately",
        "engaged_no_conversion": "Engaged, no conversion",
        "appointment_confirmed": "Confirmed — not in PMS",
    }
    return [{
        "complete_call_id": r.get("complete_call_id"),
        "date":     r.get("start_time_local"),
        "phone":    r.get("phone_raw"),
        "reasoning": r.get("reasoning") or "",
        "outcome":  labels.get(r.get("outcome"), r.get("outcome")),
    } for r in rows]


def leak_calls(clinic_id: str, invoca_campaign_ids: list[str], bucket: str,
               window: "Window", limit: int = 500,
               match_days: int = CALL_BOOKING_MATCH_DAYS) -> list[dict]:
    """Per-call sample rows for one call-funnel bucket — the calls behind
    "Booked" (``booked``), "Existing customer" (``existing_customer``),
    "Never connected" (``never_connected``) and "Qualified - No Conversion"
    (``qualified_no_conversion``). Uses the SAME shared tagging CTE + bucket
    predicate as ``call_outcomes_funnel``, so these rows are exactly the calls
    the funnel counts in that bucket (the buckets are mutually exclusive, so each
    call appears under one only — e.g. a qualified call that reconciled to a
    booking counts as *booked*, not *qualified*). ``limit`` caps how many sample
    rows come back (newest first). Each row: ``complete_call_id`` (→ transcript),
    ``date`` (local), ``phone`` (caller), ``reasoning``. PHI — gated
    admin/super_admin + audited at the endpoint. Empty for unknown bucket / no
    Invoca campaigns."""
    predicate = _BUCKET_PREDICATE.get(bucket)
    if not predicate or not invoca_campaign_ids:
        return []
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    sql = _call_tagging_cte(in_iv, window) + f"""
            SELECT complete_call_id, start_time_local, phone_raw, reasoning
            FROM tagged
            WHERE {predicate}
            ORDER BY call_ts DESC
            LIMIT @limit
    """
    try:
        rows = list(_client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ScalarQueryParameter("match_days", "INT64", int(match_days)),
            bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
        ])).result())
    except Exception as exc:
        log.warning("leak_calls failed clinic=%s bucket=%s: %s", clinic_id, bucket, exc)
        return []
    return [{
        "complete_call_id": r.get("complete_call_id"),
        "date": r.get("start_time_local"),
        "phone": r.get("phone_raw"),
        "reasoning": r.get("reasoning") or "",
    } for r in rows]


def _matthew_booked_call_ids(clinic_id: str, call_ids: list[str]) -> set[str]:
    """Of ``call_ids`` (all already in the PMS-reconciled *booked* bucket), which
    were handled BY Matthew — the Matthew AI receptionist answered the call
    (``matthew_calls.answered_by_matthew = TRUE``).

    "Handled by Matthew" is defined identically here, in :func:`matthew_outcomes`,
    and in the funnel — just ``answered_by_matthew`` — so the Booked drawer flag,
    the "Calls handled by Matthew" section, and the funnel all agree. We do NOT
    require ``outcome = 'appointment_confirmed'``: Matthew triages the caller,
    collects details and *transfers to the clinic* rather than confirming a date,
    so the transcript outcome is almost always ``engaged_no_conversion`` even when
    the call drives a booking (requiring ``appointment_confirmed`` undercounted
    Matthew-driven bookings by ~90%). Booked-bucket membership (these ``call_ids``)
    already proves the appointment landed.

    Kept as a separate, self-contained query (rather than joined into the booked
    tagging CTE) so a missing/empty ``matthew_calls`` table — every non-Virsono
    clinic — can never break the Booked drill-down; it just returns an empty set.
    """
    if not call_ids:
        return set()
    sql = f"""
        SELECT DISTINCT complete_call_id
        FROM `{MATTHEW_CALLS_TABLE}`
        WHERE clinic_id = @clinic_id
          AND complete_call_id IN UNNEST(@call_ids)
          AND answered_by_matthew = TRUE
    """
    try:
        rows = list(_client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ArrayQueryParameter("call_ids", "STRING", list(call_ids)),
        ])).result())
    except Exception as exc:
        log.warning("_matthew_booked_call_ids failed clinic=%s: %s", clinic_id, exc)
        return set()
    return {r.complete_call_id for r in rows}


def booked_sample_calls(clinic_id: str, invoca_campaign_ids: list[str],
                        window: "Window", limit: int = 4,
                        match_days: int = CALL_BOOKING_MATCH_DAYS) -> list[dict]:
    """Sample "Booked" calls, each enriched with the appointment it reconciled to.

    Same rows as ``leak_calls(bucket="booked")`` — genuine, connected calls whose
    caller has a ``PMS_Unified.Appointments`` row CREATED within ``match_days``
    on/after the call — but each row carries the booking it drove under
    ``booking`` (the earliest such appointment's ``date``/``title``/``type``/
    ``status``). Reuses the SAME shared tagging CTE + booked-reconciliation join as
    ``call_outcomes_funnel``, so these are exactly the calls the funnel counts as
    booked. PHI (caller phone + booking detail) — gated admin/super_admin +
    audited at the endpoint. Empty for no Invoca campaigns.

    Each row also carries ``booked_by_matthew`` — TRUE when the Matthew AI
    receptionist handled the call (answered + engaged; see
    :func:`_matthew_booked_call_ids`). Virsono only; always FALSE elsewhere."""
    if not invoca_campaign_ids:
        return []
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    predicate = _BUCKET_PREDICATE["booked"]
    # Append a `booking` CTE to the shared tagging WITH block, then join it to the
    # booked rows. `c` and `patients` are exposed by _call_tagging_cte; the join
    # here mirrors its `booked_calls` reconciliation exactly (so every booked call
    # matches), picking the earliest-created reconciling appointment per call.
    sql = _call_tagging_cte(in_iv, window) + f""",
        booking AS (
            SELECT * EXCEPT(rn) FROM (
                SELECT
                  c.complete_call_id,
                  SUBSTR(a.start_time, 1, 16)        AS appt_start_time,
                  a.title                            AS appt_title,
                  a.event_type                       AS appt_type,
                  a.status_2                         AS appt_status,
                  ROW_NUMBER() OVER (
                      PARTITION BY c.complete_call_id
                      ORDER BY SAFE_CAST(a.created_time AS TIMESTAMP) ASC
                  ) AS rn
                FROM c
                JOIN patients p ON p.phone_norm = c.phone_norm AND LENGTH(c.phone_norm) = 10
                JOIN `{_BP}.Appointments` a
                  ON a._clinic_id = @clinic_id AND a.client_id = p.client_id
                WHERE DATE_DIFF(DATE(SAFE_CAST(a.created_time AS TIMESTAMP)), DATE(c.call_ts), DAY)
                      BETWEEN 0 AND @match_days
            )
            WHERE rn = 1
        )
        SELECT
          t.complete_call_id, t.start_time_local, t.phone_raw, t.reasoning,
          b.appt_start_time, b.appt_title, b.appt_type, b.appt_status
        FROM tagged t
        LEFT JOIN booking b USING (complete_call_id)
        WHERE {predicate}
        ORDER BY t.call_ts DESC
        LIMIT @limit
    """
    try:
        rows = list(_client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ScalarQueryParameter("match_days", "INT64", int(match_days)),
            bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
        ])).result())
    except Exception as exc:
        log.warning("booked_sample_calls failed clinic=%s: %s", clinic_id, exc)
        return []
    out: list[dict] = []
    for r in rows:
        booking = None
        if r.get("appt_start_time") or r.get("appt_title") or r.get("appt_status"):
            booking = {
                "date":   r.get("appt_start_time"),
                "title":  r.get("appt_title"),
                "type":   r.get("appt_type"),
                "status": r.get("appt_status"),
            }
        out.append({
            "complete_call_id": r.get("complete_call_id"),
            "date":      r.get("start_time_local"),
            "phone":     r.get("phone_raw"),
            "reasoning": r.get("reasoning") or "",
            "booking":   booking,
        })
    matthew_ids = _matthew_booked_call_ids(
        clinic_id, [o["complete_call_id"] for o in out if o["complete_call_id"]])
    for o in out:
        o["booked_by_matthew"] = o["complete_call_id"] in matthew_ids
    return out


def line_item_calls(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    window: "Window",
    limit: int = 5000,
    match_days: int = CALL_BOOKING_MATCH_DAYS,
    lookback_days: int = CALL_BOOKING_LOOKBACK_DAYS,
    clinic_tz: str | None = None,
) -> list[dict]:
    """Full per-call table for the line-item calls report — one row per call.

    Built on the SHARED ``_call_tagging_cte`` so each row's ``outcome`` matches
    the funnel/tree exactly. Each row carries: caller (phone + name), location,
    marketing_channel/campaign, duration, the mutually-exclusive ``outcome``, the
    callscoring ``reasoning``, ``has_transcript``, Matthew handling + outcome, the
    matched PMS patient, and aggregated appointments (created within
    ``match_days`` of the call — same rule as ``booked``) and attributed revenue
    (the matched patient's invoices, ANY date, order-deduped, positive only —
    the SAME method as the Overview's ``pipeline_revenue_by_month``, so the two
    reconcile). ``datetime`` is in the clinic's own timezone when ``clinic_tz``
    (IANA) is given. PHI — gated admin/super_admin + audited at the endpoint.
    Empty Invoca list → []."""
    if not invoca_campaign_ids:
        return []
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    # Datetime in the clinic's own timezone (UTC call_ts → IANA tz); human
    # formatting happens client-side. Fallback: Invoca's start_time_local string.
    dt_sql = ("FORMAT_DATETIME('%Y-%m-%d %H:%M', DATETIME(t.call_ts, @clinic_tz))"
              if clinic_tz else "t.start_time_local")
    sql = _call_tagging_cte(in_iv, window) + f""",
        pm AS (   -- call ↔ matched PMS patient(s), by last-10 phone
            SELECT DISTINCT c.complete_call_id, c.call_ts, c.call_date_local, p.client_id
            FROM c JOIN patients p
              ON p.phone_norm = c.phone_norm AND LENGTH(c.phone_norm) = 10
        ),
        appt_agg AS (
            SELECT complete_call_id,
                   COUNT(*) AS appointment_count,
                   ARRAY_AGG(STRUCT(start_time, event_type, status, practitioner,
                                    location_name, created_time, sales_opportunity)
                             ORDER BY start_time LIMIT 100) AS appointments
            FROM (
              SELECT pm.complete_call_id,
                     SUBSTR(a.start_time, 1, 16) AS start_time,
                     a.event_type                AS event_type,
                     a.status_2                  AS status,
                     a.practitioner              AS practitioner,
                     a.location_name             AS location_name,
                     a.created_time              AS created_time,
                     a.sales_opportunity         AS sales_opportunity
              FROM pm
              JOIN `{_BP}.Appointments` a
                ON a._clinic_id = @clinic_id AND a.client_id = pm.client_id
              WHERE DATE_DIFF(DATE(SAFE_CAST(a.created_time AS TIMESTAMP)),
                              pm.call_date_local, DAY) BETWEEN 0 AND @match_days
            )
            GROUP BY complete_call_id
        ),
        first_call AS (   -- each matched patient's EARLIEST call owns their revenue
            SELECT client_id, complete_call_id FROM (
              SELECT client_id, complete_call_id,
                     ROW_NUMBER() OVER (PARTITION BY client_id ORDER BY call_ts ASC) AS rn
              FROM pm
            ) WHERE rn = 1
        ),
        inv_agg AS (
            -- Attributed revenue, matching the Overview's pipeline_revenue method:
            -- the patient's invoices, ANY date, deduped by order_id (MAX per
            -- order), positive only. Each order is attributed to the patient's
            -- FIRST call so the same invoice never appears on two call rows —
            -- the column is additive and reconciles with the Overview's per-order
            -- dedup. No date surfaced (attribution is patient-level).
            SELECT fc.complete_call_id,
                   COUNT(*) AS invoice_count,
                   SUM(o.amt) AS revenue_total,
                   ARRAY_AGG(STRUCT(o.invoice_number AS invoice_number, o.amt AS total,
                                    o.provider AS provider, o.location AS location)
                             ORDER BY o.amt DESC LIMIT 100) AS invoices
            FROM (
              SELECT im.client_id                                     AS client_id,
                     im.order_id                                      AS order_id,
                     ANY_VALUE(im.invoice_number)                     AS invoice_number,
                     MAX(SAFE_CAST(im.order_total_with_tax AS FLOAT64)) AS amt,
                     ANY_VALUE(im.provider)                           AS provider,
                     ANY_VALUE(im.location)                           AS location
              FROM `{_BP}.InvoiceMaster` im
              WHERE im._clinic_id = @clinic_id
                AND SAFE_CAST(im.order_total_with_tax AS FLOAT64) > 0
                AND im.client_id IN (SELECT DISTINCT client_id FROM pm)
              GROUP BY im.client_id, im.order_id
            ) o
            JOIN first_call fc ON fc.client_id = o.client_id
            GROUP BY fc.complete_call_id
        ),
        pid AS (
            SELECT complete_call_id, ANY_VALUE(client_id) AS patient_id
            FROM pm GROUP BY complete_call_id
        ),
        bp_name AS (   -- matched patient's name from Blueprint/PMS demographics
            SELECT pm.complete_call_id,
                   ANY_VALUE(NULLIF(TRIM(CONCAT(IFNULL(d.given_name, ''), ' ',
                                              IFNULL(d.surname, ''))), '')) AS name
            FROM pm
            JOIN `{_BP}.ClientDemographics` d
              ON d._clinic_id = @clinic_id AND d.client_id = pm.client_id
            GROUP BY pm.complete_call_id
        ),
        mx AS (
            SELECT complete_call_id,
                   ANY_VALUE(answered_by_matthew) AS handled_by_matthew,
                   ANY_VALUE(outcome)             AS matthew_outcome,
                   ANY_VALUE(caller_name)         AS caller_name
            FROM `{MATTHEW_CALLS_TABLE}`
            GROUP BY complete_call_id
        ),
        booked_ep AS (   -- the booking calls (closest-to-appt), with phone + time
            SELECT c.complete_call_id AS booking_call_id, c.phone_norm, c.call_ts AS booking_ts
            FROM c
            WHERE c.complete_call_id IN (SELECT complete_call_id FROM booked_calls)
        ),
        led AS (   -- non-booking calls from the SAME number that preceded a
                   -- booking within the lookback window → "led to booking"
            SELECT c.complete_call_id,
                   ARRAY_AGG(b.booking_call_id ORDER BY b.booking_ts ASC LIMIT 1)[OFFSET(0)] AS booking_call_id
            FROM c
            JOIN booked_ep b
              ON b.phone_norm = c.phone_norm AND LENGTH(c.phone_norm) = 10
             AND b.booking_ts > c.call_ts
             AND TIMESTAMP_DIFF(b.booking_ts, c.call_ts, DAY) <= @lookback_days
            WHERE c.complete_call_id NOT IN (SELECT complete_call_id FROM booked_calls)
            GROUP BY c.complete_call_id
        ),
        ep_count AS (   -- preceding-call count per booking (episode size = this + 1)
            SELECT booking_call_id, COUNT(*) AS n_preceding FROM led GROUP BY booking_call_id
        )
        SELECT
          t.complete_call_id,
          tx.transaction_id,
          {dt_sql}                         AS call_datetime,
          t.phone_raw,
          -- Blueprint patient name when the call has an associated appointment,
          -- OR when it led to a booking (same patient who booked) — those are
          -- confident patient matches; else the Matthew-stated name.
          COALESCE(
            IF(IFNULL(appt_agg.appointment_count, 0) > 0 OR led.complete_call_id IS NOT NULL,
               bp_name.name, NULL),
            mx.caller_name
          )                                AS caller_name,
          tx.city, tx.region,
          tx.marketing_channel,
          NULLIF(NULLIF(tx.utm_campaign, 'nan'), '') AS utm_campaign,
          tx.duration, tx.connect_duration,
          -- Outcome = booking RESULT for connected calls (new/existing and
          -- looking-to-book are their own orthogonal columns now, so
          -- existing-customer is NOT an outcome value anymore). A manual
          -- relabel wins outright (its flags are already rewritten upstream,
          -- but the explicit branch keeps derived labels like led_to_booking
          -- from re-tagging an overridden call).
          CASE
            WHEN t.override_outcome IS NOT NULL THEN t.override_outcome
            WHEN t.no_transcript THEN 'no_transcript'
            WHEN t.spam          THEN 'spam'
            WHEN t.is_wrong      THEN 'wrong_number'
            WHEN t.connected_raw AND t.reconciled THEN 'booked'
            WHEN t.connected_raw AND t.existing   THEN 'existing_patient'
            WHEN t.connected_raw AND led.complete_call_id IS NOT NULL THEN 'led_to_booking'
            WHEN t.connected_raw AND t.looking_to_book THEN 'qualified_no_conversion'
            WHEN t.connected_raw                  THEN 'other'
            ELSE 'no_conversation'
          END                              AS outcome,
          (t.override_outcome IS NOT NULL) AS outcome_overridden,
          -- Axis 1: new vs existing patient (genuine connected calls only).
          CASE WHEN t.genuine AND t.connected_raw THEN IF(t.existing, 'existing', 'new') END AS customer_type,
          -- Axis 2: booking intent, independent of new/existing and of result.
          t.looking_to_book                AS looking_to_book,
          t.reasoning,
          t.has_content                    AS has_transcript,
          IFNULL(mx.handled_by_matthew, FALSE) AS handled_by_matthew,
          mx.matthew_outcome,
          (pid.patient_id IS NOT NULL)     AS patient_matched,
          pid.patient_id,
          IFNULL(appt_agg.appointment_count, 0) AS appointment_count,
          appt_agg.appointments,
          IFNULL(inv_agg.invoice_count, 0) AS invoice_count,
          IFNULL(inv_agg.revenue_total, 0.0)   AS revenue_total,
          inv_agg.invoices,
          -- Booking touchpoint history: this call preceded a booking from the
          -- same number (led_to_booking), the booking it led to, and the episode
          -- size (touchpoints = preceding calls + the booking call itself).
          (led.complete_call_id IS NOT NULL)   AS led_to_booking,
          COALESCE(led.booking_call_id, IF(t.reconciled, t.complete_call_id, NULL)) AS booking_call_id,
          CASE
            WHEN t.reconciled THEN 1 + IFNULL(ep_self.n_preceding, 0)
            WHEN led.complete_call_id IS NOT NULL THEN 1 + IFNULL(ep_led.n_preceding, 0)
            ELSE 0
          END                                  AS touchpoints
        FROM tagged t
        LEFT JOIN (
          -- dedupe: transactions can carry duplicate complete_call_id rows;
          -- one row per call keeps this join from fanning out the table.
          SELECT complete_call_id, transaction_id, city, region, marketing_channel,
                 utm_campaign, duration, connect_duration
          FROM `{_CLINIC_DATA}.transactions`
          QUALIFY ROW_NUMBER() OVER (PARTITION BY complete_call_id ORDER BY timestamp DESC) = 1
        ) tx ON tx.complete_call_id = t.complete_call_id
        LEFT JOIN mx       ON mx.complete_call_id       = t.complete_call_id
        LEFT JOIN pid      ON pid.complete_call_id      = t.complete_call_id
        LEFT JOIN bp_name  ON bp_name.complete_call_id  = t.complete_call_id
        LEFT JOIN appt_agg ON appt_agg.complete_call_id = t.complete_call_id
        LEFT JOIN inv_agg  ON inv_agg.complete_call_id  = t.complete_call_id
        LEFT JOIN led      ON led.complete_call_id      = t.complete_call_id
        LEFT JOIN ep_count ep_self ON ep_self.booking_call_id = t.complete_call_id
        LEFT JOIN ep_count ep_led  ON ep_led.booking_call_id  = led.booking_call_id
        ORDER BY t.call_ts DESC
        LIMIT @limit
    """
    params = [
        bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
        bigquery.ScalarQueryParameter("match_days", "INT64", int(match_days)),
        bigquery.ScalarQueryParameter("lookback_days", "INT64", int(lookback_days)),
        bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
    ]
    if clinic_tz:
        params.append(bigquery.ScalarQueryParameter("clinic_tz", "STRING", clinic_tz))
    try:
        rows = list(_client().query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())
    except Exception as exc:
        log.warning("line_item_calls failed clinic=%s: %s", clinic_id, exc)
        return []

    def _list(v):
        return [dict(x) for x in v] if v else []

    return [{
        "call_id":            r.get("complete_call_id"),
        "transaction_id":     r.get("transaction_id"),
        "datetime":           r.get("call_datetime"),
        "caller_phone":       r.get("phone_raw"),
        "caller_name":        r.get("caller_name"),
        "location":           ", ".join(x for x in [r.get("city"), r.get("region")] if x),
        "channel":            r.get("marketing_channel"),
        "campaign":           r.get("utm_campaign"),
        "duration_sec":       int(r.get("duration") or 0),
        "connect_sec":        int(r.get("connect_duration") or 0),
        "outcome":            r.get("outcome"),
        "outcome_overridden": bool(r.get("outcome_overridden")),
        "customer_type":      r.get("customer_type"),
        "looking_to_book":    bool(r.get("looking_to_book")),
        "reasoning":          r.get("reasoning") or "",
        "has_transcript":     bool(r.get("has_transcript")),
        "handled_by_matthew": bool(r.get("handled_by_matthew")),
        "matthew_outcome":    r.get("matthew_outcome"),
        "patient_matched":    bool(r.get("patient_matched")),
        "patient_id":         r.get("patient_id"),
        "appointment_count":  int(r.get("appointment_count") or 0),
        "appointments":       _list(r.get("appointments")),
        "invoice_count":      int(r.get("invoice_count") or 0),
        "revenue_total":      float(r.get("revenue_total") or 0.0),
        "invoices":           _list(r.get("invoices")),
        "led_to_booking":     bool(r.get("led_to_booking")),
        "booking_call_id":    r.get("booking_call_id"),
        "touchpoints":        int(r.get("touchpoints") or 0),
    } for r in rows]


def call_belongs_to_clinic(invoca_campaign_ids: list[str], complete_call_id: str) -> bool:
    """TRUE when the call id exists in ``transactions`` under one of the
    clinic's active Invoca campaigns — the ownership gate for the relabel
    endpoint (covers transcript-less calls, which have no callscoring row)."""
    if not invoca_campaign_ids:
        return False
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    rows = list(_client().query(
        f"""SELECT 1 FROM `{_CLINIC_DATA}.transactions`
            WHERE complete_call_id = @ccid
              AND CAST(invoca_campaign_id AS STRING) IN {in_iv}
            LIMIT 1""",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("ccid", "STRING", complete_call_id)]),
    ).result())
    return bool(rows)


_overrides_table_ready = False


def _ensure_overrides_table(client: bigquery.Client) -> None:
    """Create the relabel table on first write in a fresh environment. Reads
    reference the table unconditionally, so production keeps it pre-created;
    this guard is a safety net, run once per process."""
    global _overrides_table_ready
    if _overrides_table_ready:
        return
    client.query(f"""
        CREATE TABLE IF NOT EXISTS `{_OVERRIDES_TABLE}` (
          complete_call_id STRING NOT NULL,
          clinic_id        STRING NOT NULL,
          outcome          STRING,            -- NULL = override cleared
          set_by           STRING,
          set_at           TIMESTAMP NOT NULL
        )""").result()
    _overrides_table_ready = True


def set_call_outcome_override(
    clinic_id: str, complete_call_id: str, outcome: str | None, set_by: str,
) -> None:
    """Record a manual relabel of one call's outcome (``outcome=None`` clears a
    prior override). Append-only INSERT — ``_call_tagging_cte`` reads the latest
    row per call, so history is retained and no DELETE/UPDATE is needed.
    Ownership + role checks happen at the endpoint. Raises on invalid label."""
    if outcome is not None and outcome not in RELABEL_OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(RELABEL_OUTCOMES)} or None")
    client = _client()
    _ensure_overrides_table(client)
    client.query(
        f"""INSERT INTO `{_OVERRIDES_TABLE}`
              (complete_call_id, clinic_id, outcome, set_by, set_at)
            VALUES (@ccid, @clinic_id, @outcome, @set_by, CURRENT_TIMESTAMP())""",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("ccid", "STRING", complete_call_id),
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ScalarQueryParameter("outcome", "STRING", outcome),
            bigquery.ScalarQueryParameter("set_by", "STRING", set_by),
        ]),
    ).result()


_TRANSCRIPTS_BUCKET = "transcripts-json"


def get_call_transcript(clinic_id: str, complete_call_id: str) -> dict | None:
    """Fetch one call's transcript ("what was said") for the leads drill-down.

    PHI: the transcript contains caller identity + health context, so the caller
    must already be admin/super_admin (gated at the endpoint) and the access is
    audited there. This verifies the call belongs to ``clinic_id`` (via its
    ``callscoring`` row) before reading ``gs://transcripts-json/<ccid>.json`` —
    so a caller can't pull an arbitrary clinic's transcript by guessing an id.

    Returns ``{"complete_call_id", "turns": [{"speaker", "text"}, …]}`` or
    ``None`` when the call isn't this clinic's or no transcript exists."""
    owned = list(_client().query(
        f"""SELECT 1 FROM `{_CLINIC_DATA}.callscoring`
            WHERE clinic_id = @clinic_id AND complete_call_id = @ccid LIMIT 1""",
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id, ccid=complete_call_id)),
    ).result())
    if not owned:
        return None

    from google.cloud import storage
    try:
        blob = storage.Client(project="project-demo-2-482101").bucket(
            _TRANSCRIPTS_BUCKET).blob(f"{complete_call_id}.json")
        raw = json.loads(blob.download_as_text())
    except Exception as exc:
        log.warning("transcript fetch failed clinic=%s ccid=%s: %s", clinic_id, complete_call_id, exc)
        return None

    # Stored as a list of single-key {speaker: utterance} turns.
    turns: list[dict] = []
    for seg in (raw if isinstance(raw, list) else []):
        if isinstance(seg, dict):
            for role, text in seg.items():
                if text:
                    turns.append({"speaker": str(role), "text": str(text)})
    return {"complete_call_id": complete_call_id, "turns": turns}


def _zoolstra_form_submissions(clinic_id: str, w: "Window") -> tuple[int, int]:
    """Virsono web-form submissions sourced from CounselEar: appointments tagged
    ``appt_referral_type = "Referral - Zoolstra"`` (forms book directly into
    CounselEar). Returns ``(submissions, booked)`` for the window — ``submissions``
    = distinct such appointments by ``appt_date``; ``booked`` = those with a kept
    status (Completed/Arrived). Returns ``(0, 0)`` (never raises) for clinics not
    on CounselEar or when the feed is absent."""
    booked_in = ", ".join(f"'{s}'" for s in _ZOOLSTRA_BOOKED_STATUSES)
    try:
        rows = list(_client().query(f"""
            SELECT
              COUNT(DISTINCT appt_id) AS submissions,
              COUNT(DISTINCT IF(LOWER(status) IN ({booked_in}), appt_id, NULL)) AS booked
            FROM `{_COUNSELEAR}.appointments`
            WHERE _clinic_id = @clinic_id
              AND appt_referral_type = @tag
              AND {_date_between('appt_date', w)}
        """, job_config=bigquery.QueryJobConfig(
            query_parameters=_params(clinic_id, tag=ZOOLSTRA_REFERRAL_TAG))).result())
    except Exception as exc:
        log.warning("zoolstra form submissions failed clinic=%s: %s", clinic_id, exc)
        return 0, 0
    if not rows:
        return 0, 0
    return int(rows[0].submissions or 0), int(rows[0].booked or 0)


def form_capture(
    clinic_id: str,
    days: int = 90,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """Web-form volume + capture for the window: ``submissions``,
    ``form_bookings`` (submitter matched to a PMS patient with an appointment
    on/after submission) and ``form_rate`` = form_bookings / submissions.

    Covers two form sources, which are **counted separately and also summed**:

    ``web`` — ``ClinicData.webforms``. A submission we observe at submit time,
      carrying the full web-analytics block (gclid / utm_* / landing_page). This
      is the only form population that can ever be tied to a campaign.

    ``portal`` — for CounselEar (Virsono) clinics, the site's CounselEar booking
      embed writes straight into the PMS; we never see a submission, only the
      resulting ``appt_referral_type = "Referral - Zoolstra"`` appointment. No
      gclid, no utm, no landing page — **structurally unattributable**, not
      merely unattributed.

    The top-level ``submissions`` / ``form_bookings`` / ``form_rate`` remain the
    SUM of both, so existing consumers are unchanged. They are, however, a sum
    of unlike things and only ``submissions`` is a clean total:

    * ``form_rate`` mixes two metrics. On the web side it is submissions that
      led to an appointment ÷ submissions (a BOOKING rate). On the portal side
      every row is already an appointment, so its rate is kept ÷ booked (a SHOW
      rate). Use ``web.form_rate`` when the question is "how many enquiries
      converted"; the combined figure answers neither question.
    * The two are windowed on different events — web on ``submitted_at``, portal
      on ``appt_date`` — so a portal booking made in one month for a visit in the
      next lands in the later window.
    * Only the web block can ever carry a campaign, so any attribution-coverage
      rate must use ``web.submissions`` as its denominator; the combined figure
      depresses coverage by the size of a population that can never be covered.

    See methodology-contract.md §14."""
    w = _win(window, days)
    out = {
        "submissions": 0, "form_bookings": 0, "form_rate": None,
        "web":    {"submissions": 0, "form_bookings": 0, "form_rate": None},
        "portal": {"submissions": 0, "form_bookings": 0, "form_rate": None},
    }
    try:
        rows = list(_client().query(f"""
            WITH forms AS (
              SELECT
                ROW_NUMBER() OVER (ORDER BY submitted_at, email, phone_number) AS form_id,
                DATE(submitted_at)                                            AS sd,
                RIGHT(REGEXP_REPLACE(IFNULL(phone_number,''), r'\\D',''), 10)  AS phone_norm,
                LOWER(TRIM(IFNULL(email,'')))                                 AS email_norm
              FROM `{_CLINIC_DATA}.webforms`
              WHERE clinic_id = @clinic_id AND {_ts_between('submitted_at', w)}
            ),
            patients AS (
              SELECT DISTINCT client_id, phone_norm, email_norm
              FROM `{_PATIENT_CONTACTS}` WHERE _clinic_id = @clinic_id
            ),
            fc AS (
              SELECT DISTINCT f.form_id, f.sd, p.client_id
              FROM forms f JOIN patients p
                ON (LENGTH(f.phone_norm)=10 AND f.phone_norm=p.phone_norm)
                OR (f.email_norm != '' AND f.email_norm=p.email_norm)
            ),
            booked AS (
              SELECT DISTINCT fc.form_id
              FROM fc JOIN `{_BP}.Appointments` a
                ON a._clinic_id = @clinic_id AND a.client_id = fc.client_id
              WHERE SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(a.start_time,1,10)) >= fc.sd
            )
            SELECT
              (SELECT COUNT(*) FROM forms)  AS submissions,
              (SELECT COUNT(*) FROM booked) AS form_bookings
        """, job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id))).result())
    except Exception as exc:
        log.warning("form_capture failed clinic=%s: %s", clinic_id, exc)
        rows = []   # webforms unavailable; still fold in the CounselEar source below
    if rows:
        r = rows[0]
        out["web"]["submissions"]   = int(r.submissions or 0)
        out["web"]["form_bookings"] = int(r.form_bookings or 0)
    # Virsono: the site's CounselEar embed books directly into the PMS as
    # 'Referral - Zoolstra' appointments. No-op for non-CounselEar clinics.
    z_sub, z_booked = _zoolstra_form_submissions(clinic_id, w)
    out["portal"]["submissions"]   = z_sub
    out["portal"]["form_bookings"] = z_booked

    def _rate(block: dict[str, int]) -> float | None:
        return (block["form_bookings"] / block["submissions"]) if block["submissions"] else None

    out["web"]["form_rate"]    = _rate(out["web"])
    out["portal"]["form_rate"] = _rate(out["portal"])
    out["submissions"]   = out["web"]["submissions"] + out["portal"]["submissions"]
    out["form_bookings"] = out["web"]["form_bookings"] + out["portal"]["form_bookings"]
    out["form_rate"]     = _rate(out)
    return out


def patient_contacts(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """Total patient contacts in the window = non-spam calls + form submissions.
    (Channel-level volume; cross-channel de-dup is intentionally not applied.)"""
    w = _win(window, days)
    calls = call_capture(clinic_id, invoca_campaign_ids, window=w)
    forms = form_capture(clinic_id, window=w)
    return {
        "calls": calls["calls"],
        "forms": forms["submissions"],
        # Split per §14: `forms_web` are observed submissions (attributable),
        # `forms_portal` are CounselEar-embed bookings we only see downstream.
        "forms_web": forms["web"]["submissions"],
        "forms_portal": forms["portal"]["submissions"],
        "total": calls["calls"] + forms["submissions"],
    }


def _prev_period(w: "Window") -> "Window":
    """The equal-length period immediately before ``w`` (month-over-month for a
    ~monthly window)."""
    span = w.span_days
    prev_end_incl = w.start - _dt.timedelta(days=1)
    prev_start = prev_end_incl - _dt.timedelta(days=span - 1)
    return Window(prev_start.isoformat(), prev_end_incl.isoformat())


def headline_yoy(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 365,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """Headline comparison: the selected window vs a prior period.

    Basis is **year-over-year** (same span one year earlier) when a full year of
    data exists behind the window; otherwise — when the year-ago period falls
    entirely before the ``MIN_WINDOW_DATE`` cutoff — it falls back to
    **month-over-month** (the equal-length period immediately before). ``basis``
    is one of ``"yoy" | "mom" | "none"`` so the UI can label it. Returns
    ``current`` / ``prior`` blocks (contacts, capture_rate, form_rate, revenue,
    plus raw counts) and ``deltas`` (pct change for contacts/revenue;
    percentage-point change for capture_rate)."""
    w = _win(window, days)

    # Prefer YoY; if there isn't a full year of data behind us (the year-ago
    # window is entirely before the cutoff), compare to the previous period.
    prior_win = _year_ago(w).floored()
    if prior_win is not None:
        basis = "yoy"
    else:
        prior_win = _prev_period(w).floored()
        basis = "mom" if prior_win is not None else "none"

    def _period(win: "Window | None") -> dict[str, Any]:
        if win is None:
            return {"contacts": 0, "calls": 0, "forms": 0, "forms_web": 0,
                    "forms_portal": 0, "connected": 0, "booked": 0,
                    "capture_rate": None, "form_rate": None,
                    "form_rate_web": None, "revenue": 0.0}
        calls = call_capture(clinic_id, invoca_campaign_ids, window=win)
        forms = form_capture(clinic_id, window=win)
        rev = invoice_revenue(clinic_id, window=win)
        return {
            "contacts": calls["calls"] + forms["submissions"],
            "calls": calls["calls"],
            "forms": forms["submissions"],
            # Per-source split (§14). `forms` stays the sum so the contacts
            # total and every existing reader are unchanged.
            "forms_web": forms["web"]["submissions"],
            "forms_portal": forms["portal"]["submissions"],
            "connected": calls["connected"],
            "booked": calls["booked"],
            "capture_rate": calls["capture_rate"],
            "form_rate": forms["form_rate"],
            # Web-only capture rate — the only one that means "submissions that
            # led to a booking". The combined `form_rate` above is NOT that:
            # every portal row is already an appointment, so its own rate is a
            # SHOW rate (kept / booked), and summing the two mixes two different
            # numerators over two different denominators. Prefer this one.
            "form_rate_web": forms["web"]["form_rate"],
            "revenue": rev["revenue"],
        }

    cur, prev = _period(w), _period(prior_win)

    def _pct(c, p):
        return ((c - p) / p) if p else None

    def _pp(c, p):
        return (c - p) if (c is not None and p is not None) else None

    def _span(win: "Window | None"):
        if win is None:
            return None
        return {"start": win.start_date,
                "end": (win.end_excl - _dt.timedelta(days=1)).isoformat()}

    return {
        "basis": basis,
        "current": cur,
        "prior": prev,
        "deltas": {
            "contacts": _pct(cur["contacts"], prev["contacts"]),
            "revenue": _pct(cur["revenue"], prev["revenue"]),
            "capture_rate": _pp(cur["capture_rate"], prev["capture_rate"]),
        },
        "window": _span(w),
        "prior_window": _span(prior_win),
    }


def monthly_contact_trend(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    months: int = 13,
    window: "Window | None" = None,
) -> list[dict]:
    """Per-month contacts (non-spam calls + form submissions) and invoiced
    revenue for the trailing ``months`` (default 13 → a full YoY trendline).
    Window-independent: always trails from the current month so the line shows
    the long direction, not the filtered slice."""
    today = max_window_date()
    # Last month shown is the current (in-progress) month, trailing back from today.
    last_month = today.replace(day=1)
    # Earliest month start, ``months-1`` months before the last month.
    y, m = last_month.year, last_month.month - (int(months) - 1)
    while m <= 0:
        m += 12
        y -= 1
    # Never trail before the hard data cutoff.
    earliest = max(_dt.date(y, m, 1), MIN_WINDOW_DATE.replace(day=1))
    y, m = earliest.year, earliest.month
    span = Window(earliest.isoformat(), today.isoformat())
    labels = {}
    yy, mm = y, m
    while (yy, mm) <= (last_month.year, last_month.month):
        labels[f"{yy:04d}-{mm:02d}"] = {"month": f"{yy:04d}-{mm:02d}", "calls": 0,
                                        "forms": 0, "contacts": 0, "revenue": 0.0,
                                        "connected": 0, "booked": 0, "capture_rate": None}
        mm += 1
        if mm > 12:
            mm, yy = 1, yy + 1
    client = _client()

    def _run(sql, params=None):
        try:
            cfg = bigquery.QueryJobConfig(query_parameters=params) if params else None
            return list(client.query(sql, job_config=cfg).result())
        except Exception as exc:
            log.warning("monthly_contact_trend sub-query failed clinic=%s: %s", clinic_id, exc)
            return []

    if invoca_campaign_ids:
        in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
        for r in _run(f"""
            WITH c AS (
              SELECT
                FORMAT_TIMESTAMP('%Y-%m', t.timestamp)        AS mo,
                IFNULL(cs.spam_or_solicitor, FALSE)           AS is_spam,
                (cs.complete_call_id IS NOT NULL)             AS has_cs,
                IFNULL(cs.no_conversation, FALSE)             AS no_conv,
                IFNULL(cs.appointment_booked, FALSE)          AS booked
              FROM `{_CLINIC_DATA}.transactions` t
              LEFT JOIN `{_CLINIC_DATA}.callscoring` cs
                ON cs.complete_call_id = t.complete_call_id
              WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
                AND {_ts_between('t.timestamp', span)}
            )
            SELECT mo,
                   COUNTIF(NOT is_spam)                                       AS calls,
                   COUNTIF(NOT is_spam AND has_cs AND NOT no_conv)            AS connected,
                   COUNTIF(NOT is_spam AND has_cs AND NOT no_conv AND booked) AS booked
            FROM c GROUP BY mo
        """):
            if r.mo in labels:
                labels[r.mo]["calls"] = int(r.calls or 0)
                labels[r.mo]["connected"] = int(r.connected or 0)
                labels[r.mo]["booked"] = int(r.booked or 0)

    for r in _run(f"""
        SELECT FORMAT_TIMESTAMP('%Y-%m', submitted_at) AS mo, COUNT(*) AS forms
        FROM `{_CLINIC_DATA}.webforms`
        WHERE clinic_id = @clinic_id AND {_ts_between('submitted_at', span)}
        GROUP BY mo
    """, _params(clinic_id)):
        if r.mo in labels:
            labels[r.mo]["forms"] = int(r.forms or 0)

    # Virsono: form submissions arrive as CounselEar 'Referral - Zoolstra'
    # appointments. Add them per month (no-op for non-CounselEar clinics).
    for r in _run(f"""
        SELECT FORMAT_DATE('%Y-%m', appt_date) AS mo, COUNT(DISTINCT appt_id) AS forms
        FROM `{_COUNSELEAR}.appointments`
        WHERE _clinic_id = @clinic_id AND appt_referral_type = @tag
          AND {_date_between('appt_date', span)}
        GROUP BY mo
    """, _params(clinic_id, tag=ZOOLSTRA_REFERRAL_TAG)):
        if r.mo in labels:
            labels[r.mo]["forms"] += int(r.forms or 0)

    for r in _run(f"""
        SELECT FORMAT_DATE('%Y-%m', SAFE.PARSE_DATE('%Y-%m-%d', invoice_date)) AS mo,
               SUM(SAFE_CAST(order_total_with_tax AS NUMERIC)) AS revenue
        FROM `{_BP}.InvoiceMaster`
        WHERE _clinic_id = @clinic_id
          AND SAFE_CAST(order_total_with_tax AS NUMERIC) > 0
          AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', invoice_date)", span)}
        GROUP BY mo
    """, _params(clinic_id)):
        if r.mo in labels:
            labels[r.mo]["revenue"] = float(r.revenue or 0.0)

    for v in labels.values():
        v["contacts"] = v["calls"] + v["forms"]
        v["capture_rate"] = (v["booked"] / v["connected"]) if v["connected"] else None
    return [labels[k] for k in sorted(labels)]


def front_desk_capture(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """Front-desk capture rate: share of inbound non-spam calls that either
    connected (real conversation) OR where the same caller called back within
    24h. ``(connected ∪ returned) / total``."""
    w = _win(window, days)
    out = {"total": 0, "connected": 0, "returned": 0, "captured": 0, "capture_rate": None}
    if not invoca_campaign_ids:
        return out
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    try:
        rows = list(_client().query(f"""
            WITH calls AS (
              SELECT
                t.complete_call_id AS id,
                t.timestamp        AS ts,
                RIGHT(REGEXP_REPLACE(IFNULL(t.calling_phone_number,''), r'\\D',''), 10) AS phone,
                IFNULL(cs.spam_or_solicitor, FALSE) AS is_spam,
                (cs.complete_call_id IS NOT NULL AND NOT IFNULL(cs.no_conversation, FALSE)) AS connected
              FROM `{_CLINIC_DATA}.transactions` t
              LEFT JOIN `{_CLINIC_DATA}.callscoring` cs
                ON cs.complete_call_id = t.complete_call_id
              WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
                AND {_ts_between('t.timestamp', w)}
            ),
            nonspam AS (SELECT * FROM calls WHERE NOT is_spam),
            flagged AS (
              SELECT a.id,
                     ANY_VALUE(a.connected)          AS connected,
                     LOGICAL_OR(b.id IS NOT NULL)    AS returned
              FROM nonspam a
              LEFT JOIN calls b
                ON LENGTH(a.phone) = 10 AND b.phone = a.phone
               AND b.ts > a.ts AND b.ts <= TIMESTAMP_ADD(a.ts, INTERVAL 24 HOUR)
              GROUP BY a.id
            )
            SELECT
              COUNT(*)                                AS total,
              COUNTIF(connected)                      AS connected,
              COUNTIF(NOT connected AND returned)     AS returned,
              COUNTIF(connected OR returned)          AS captured
            FROM flagged
        """).result())
    except Exception as exc:
        log.warning("front_desk_capture failed clinic=%s: %s", clinic_id, exc)
        return out
    if rows:
        r = rows[0]
        out.update(total=int(r.total or 0), connected=int(r.connected or 0),
                   returned=int(r.returned or 0), captured=int(r.captured or 0))
        out["capture_rate"] = (out["captured"] / out["total"]) if out["total"] else None
    return out


def fitting_rate(
    clinic_id: str,
    days: int = 365,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """Patient Quality Index proxy: distinct patients with a fitting appointment
    in the window ÷ distinct patients with any appointment in the window."""
    w = _win(window, days)
    out = {"fittings": 0, "contacts": 0, "rate": None}
    try:
        rows = list(_client().query(f"""
            SELECT
              COUNT(DISTINCT client_id) AS contacts,
              COUNT(DISTINCT IF(LOWER(event_type) LIKE '{_FITTING_EVENT_LIKE}', client_id, NULL)) AS fittings
            FROM `{_BP}.Appointments`
            WHERE _clinic_id = @clinic_id
              AND {_ts_between('SAFE_CAST(start_time AS TIMESTAMP)', w)}
        """, job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id))).result())
    except Exception as exc:
        log.warning("fitting_rate failed clinic=%s: %s", clinic_id, exc)
        return out
    if rows:
        r = rows[0]
        out["fittings"] = int(r.fittings or 0)
        out["contacts"] = int(r.contacts or 0)
        out["rate"] = (out["fittings"] / out["contacts"]) if out["contacts"] else None
    return out


def fitting_no_purchase_count(
    clinic_id: str,
    days: int = 365,
    window: "Window | None" = None,
) -> int:
    """Count of patients fitted-but-not-sold in the window (tested-not-sold)."""
    try:
        return len(fitting_no_purchase_detail(clinic_id, days=days, window=window))
    except Exception as exc:
        log.warning("fitting_no_purchase_count failed clinic=%s: %s", clinic_id, exc)
        return 0


def slow_form_followup_count(
    clinic_id: str,
    follow_up_days: int = 2,
    days: int = 90,
    window: "Window | None" = None,
) -> int:
    """Form submissions in the window with no matched PMS appointment booked
    within ``follow_up_days`` of submitting (a follow-up-speed leak)."""
    w = _win(window, days)
    try:
        rows = list(_client().query(f"""
            WITH forms AS (
              SELECT
                ROW_NUMBER() OVER (ORDER BY submitted_at, email, phone_number) AS form_id,
                DATE(submitted_at)                                            AS sd,
                RIGHT(REGEXP_REPLACE(IFNULL(phone_number,''), r'\\D',''), 10)  AS phone_norm,
                LOWER(TRIM(IFNULL(email,'')))                                 AS email_norm
              FROM `{_CLINIC_DATA}.webforms`
              WHERE clinic_id = @clinic_id AND {_ts_between('submitted_at', w)}
            ),
            patients AS (
              SELECT DISTINCT client_id, phone_norm, email_norm
              FROM `{_PATIENT_CONTACTS}` WHERE _clinic_id = @clinic_id
            ),
            fc AS (
              SELECT DISTINCT f.form_id, f.sd, p.client_id
              FROM forms f JOIN patients p
                ON (LENGTH(f.phone_norm)=10 AND f.phone_norm=p.phone_norm)
                OR (f.email_norm != '' AND f.email_norm=p.email_norm)
            ),
            quick AS (
              SELECT DISTINCT fc.form_id
              FROM fc JOIN `{_BP}.Appointments` a
                ON a._clinic_id = @clinic_id AND a.client_id = fc.client_id
              WHERE SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(a.start_time,1,10))
                    BETWEEN fc.sd AND DATE_ADD(fc.sd, INTERVAL {int(follow_up_days)} DAY)
            )
            SELECT (SELECT COUNT(*) FROM forms) AS total,
                   (SELECT COUNT(*) FROM quick) AS quick
        """, job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id))).result())
    except Exception as exc:
        log.warning("slow_form_followup_count failed clinic=%s: %s", clinic_id, exc)
        return 0
    if rows:
        r = rows[0]
        return max(0, int(r.total or 0) - int(r.quick or 0))
    return 0


def revenue_leakage(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    window: "Window | None" = None,
    follow_up_days: int = 2,
) -> dict[str, Any]:
    """Estimated revenue leakage = lost contacts × average invoice value.

    Lost contacts = missed calls (no conversation) + appointment no-shows +
    tested-not-sold patients + slow web-form follow-ups. ``intercept_*`` is the
    Cortex Intercept missed-call recovery surface — ``intercept_recovered`` is a
    placeholder (``None``) until that feed exists."""
    w = _win(window, days)
    missed = no_conversation_count(clinic_id, invoca_campaign_ids, window=w)
    appts = appointment_outcomes(clinic_id, window=w)
    no_shows = int(appts.get("by_status", {}).get("No show", 0))
    tested_not_sold = fitting_no_purchase_count(clinic_id, window=w)
    slow_forms = slow_form_followup_count(clinic_id, follow_up_days=follow_up_days, window=w)
    rev = invoice_revenue(clinic_id, window=w)
    avg_invoice = (rev["revenue"] / rev["invoice_count"]) if rev["invoice_count"] else 0.0
    lost = missed + no_shows + tested_not_sold + slow_forms
    return {
        "avg_invoice": avg_invoice,
        "components": {
            "missed_calls": missed,
            "no_shows": no_shows,
            "tested_not_sold": tested_not_sold,
            "slow_form_followup": slow_forms,
        },
        "lost_contacts": lost,
        "estimated_leakage": lost * avg_invoice,
        "intercept_missed": missed,
        "intercept_recovered": None,   # placeholder — no Cortex Intercept feed yet
    }


def lifecycle_summary(
    clinic_id: str,
    days: int = 365,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """Current lifecycle worklist sizes for the Lifecycle Performance section.
    ``review_velocity`` is a placeholder (no review_snapshots feed in the
    backend yet)."""
    w = _win(window, days)

    def _safe_len(fn) -> int | None:
        try:
            return len(fn())
        except Exception as exc:
            log.warning("lifecycle_summary part failed clinic=%s: %s", clinic_id, exc)
            return None

    return {
        "reactivation_candidates": _safe_len(lambda: lapsed_patients_detail(clinic_id)),
        "tested_not_sold": fitting_no_purchase_count(clinic_id, window=w),
        "warranty_expiring": _safe_len(lambda: warranty_expiring_detail(clinic_id)),
        "upgrade_candidates": _safe_len(lambda: upgrade_candidates_detail(clinic_id)),
        "review_velocity": None,   # placeholder — no review_snapshots feed yet
    }


# ════════════════════════════════════════════════════════════════════════════
# Patient Journey (PHI — every read is scoped to @clinic_id)
#
# Powers the patient-level marketing-stream → PMS-status view. The endpoint
# layer role-gates (admin/super_admin) and writes a PHI access-log row for every
# search and journey view; these readers only ever query within one clinic's
# _clinic_id scope. ``patient_search`` returns masked identifiers (enough to pick
# the right person); ``patient_journey`` returns the full record for one client.
# ════════════════════════════════════════════════════════════════════════════

def _mask_phone(raw: str | None) -> str | None:
    digits = "".join(c for c in str(raw or "") if c.isdigit())
    return ("•••-" + digits[-4:]) if len(digits) >= 4 else None


def _mask_email(raw: str | None) -> str | None:
    e = str(raw or "").strip()
    if "@" not in e:
        return None
    name, _, domain = e.partition("@")
    head = name[0] if name else ""
    return f"{head}•••@{domain}"


def patient_search(clinic_id: str, q: str, limit: int = 25) -> list[dict]:
    """Find patients in a clinic by name, phone, or email. Returns masked
    identifiers (name + status + masked phone/email + client_id)."""
    term = (q or "").strip()
    if len(term) < 2:
        return []
    digits = "".join(c for c in term if c.isdigit())
    phone10 = digits[-10:] if len(digits) >= 7 else ""
    email = term.lower() if "@" in term else ""
    name = f"%{term.lower()}%" if any(c.isalpha() for c in term) else ""
    params = [
        bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
        bigquery.ScalarQueryParameter("phone10", "STRING", phone10),
        bigquery.ScalarQueryParameter("email", "STRING", email),
        bigquery.ScalarQueryParameter("name", "STRING", name),
    ]
    try:
        rows = list(_client().query(f"""
            SELECT client_id, given_name, surname, status,
                   home_telephone_no, mobile_telephone_no, work_telephone_no, email_address
            FROM `{_BP}.ClientDemographics`
            WHERE _clinic_id = @clinic_id
              AND (
                (@phone10 != '' AND @phone10 IN (
                   RIGHT(REGEXP_REPLACE(IFNULL(home_telephone_no,''),   r'\\D',''), 10),
                   RIGHT(REGEXP_REPLACE(IFNULL(mobile_telephone_no,''), r'\\D',''), 10),
                   RIGHT(REGEXP_REPLACE(IFNULL(work_telephone_no,''),   r'\\D',''), 10)))
                OR (@email != '' AND LOWER(TRIM(IFNULL(email_address,''))) = @email)
                OR (@name  != '' AND LOWER(CONCAT(IFNULL(given_name,''),' ',IFNULL(surname,''))) LIKE @name)
              )
            LIMIT {int(max(1, min(limit, 100)))}
        """, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())
    except Exception as exc:
        log.warning("patient_search failed clinic=%s: %s", clinic_id, exc)
        return []
    return [
        {
            "client_id": r.client_id or "",
            "given_name": r.given_name or "",
            "surname": r.surname or "",
            "status": r.status,
            "phone": _mask_phone(r.mobile_telephone_no or r.home_telephone_no or r.work_telephone_no),
            "email": _mask_email(r.email_address),
        }
        for r in rows
    ]


def _call_outcome(spam, no_conv, booked, qualified, existing, wrong) -> str:
    if _truthy_flag(spam):
        return "Spam / solicitor"
    if _truthy_flag(wrong):
        return "Wrong number"
    if _truthy_flag(booked):
        return "Appointment booked"
    if _truthy_flag(no_conv):
        return "No conversation"
    if _truthy_flag(qualified):
        return "Qualified lead — no conversion"
    if _truthy_flag(existing):
        return "Existing customer"
    return "Other"


def patient_journey(
    clinic_id: str,
    client_id: str,
    invoca_campaign_ids: list[str] | None = None,
    call_limit: int = 50,
) -> dict[str, Any]:
    """Full marketing-stream → PMS-status record for one patient.

    Assembles: demographics, marketing touches (tracked calls matched on this
    patient's phone, with call-scoring outcome; web-form submissions matched on
    phone/email), PMS appointments, invoices, and devices. Every sub-query is
    scoped to ``@clinic_id`` and fail-safe.

    ``ClinicData.transactions`` is a *network-wide* table with no clinic column,
    so the call match is bounded BOTH by this patient's phones (derived from the
    clinic's ``patient_contacts``) AND by the clinic's own Invoca campaign ids —
    without the campaign filter a recycled/shared phone could surface another
    clinic's calls. With no active Invoca campaigns there are no tracked calls to
    show. Returned patient name (given/surname) is intentionally unmasked: this
    endpoint is admin/super_admin-only and every view is PHI-audited, and the
    front desk needs the name to confirm identity."""
    client = _client()
    params = _params(clinic_id, client_id=client_id)
    out: dict[str, Any] = {
        "client_id": client_id, "patient": None,
        "calls": [], "forms": [], "appointments": [], "invoices": [], "devices": [],
    }

    def _run(sql, p=None):
        try:
            cfg = bigquery.QueryJobConfig(query_parameters=p) if p else None
            return list(client.query(sql, job_config=cfg).result())
        except Exception as exc:
            log.warning("patient_journey sub-query failed clinic=%s client=%s: %s",
                        clinic_id, client_id, exc)
            return []

    # ── Demographics ──
    for r in _run(f"""
        SELECT given_name, surname, status,
               home_telephone_no, mobile_telephone_no, work_telephone_no, email_address
        FROM `{_BP}.ClientDemographics`
        WHERE _clinic_id = @clinic_id AND client_id = @client_id
        LIMIT 1
    """, params):
        out["patient"] = {
            "given_name": r.given_name or "", "surname": r.surname or "",
            "status": r.status,
            "phone": _mask_phone(r.mobile_telephone_no or r.home_telephone_no or r.work_telephone_no),
            "email": _mask_email(r.email_address),
        }

    # ── Marketing touches: tracked calls matched on this patient's phone(s) ──
    # Scoped to BOTH the patient's phones AND the clinic's Invoca campaigns
    # (transactions is network-wide; campaign filter prevents cross-clinic leak).
    if invoca_campaign_ids:
        in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
        for r in _run(f"""
            WITH phones AS (
                SELECT DISTINCT phone_norm
                FROM `{_PATIENT_CONTACTS}`
                WHERE _clinic_id = @clinic_id AND client_id = @client_id
                  AND LENGTH(phone_norm) = 10
            )
            SELECT
              t.timestamp AS ts, t.utm_source, t.utm_medium, t.marketing_channel, t.gclid,
              cs.spam_or_solicitor, cs.no_conversation, cs.appointment_booked,
              cs.qualified_lead_no_conversion, cs.existing_customer, cs.wrong_number
            FROM `{_CLINIC_DATA}.transactions` t
            JOIN phones ON phones.phone_norm =
                 RIGHT(REGEXP_REPLACE(IFNULL(t.calling_phone_number,''), r'\\D',''), 10)
            LEFT JOIN `{_CLINIC_DATA}.callscoring` cs ON cs.complete_call_id = t.complete_call_id
            WHERE CAST(t.invoca_campaign_id AS STRING) IN {in_iv}
            ORDER BY t.timestamp DESC
            LIMIT {int(max(1, min(call_limit, 200)))}
        """, params):
            out["calls"].append({
                "timestamp": str(r.ts) if r.ts else None,
                "utm_source": r.utm_source, "utm_medium": r.utm_medium,
                "marketing_channel": r.marketing_channel, "gclid": bool(r.gclid),
                "outcome": _call_outcome(r.spam_or_solicitor, r.no_conversation,
                                         r.appointment_booked, r.qualified_lead_no_conversion,
                                         r.existing_customer, r.wrong_number),
            })

    # ── Web-form submissions matched on phone/email ──
    for r in _run(f"""
        WITH contact AS (
            SELECT
              ARRAY_AGG(DISTINCT phone_norm IGNORE NULLS) AS phones,
              ARRAY_AGG(DISTINCT email_norm IGNORE NULLS) AS emails
            FROM `{_PATIENT_CONTACTS}`
            WHERE _clinic_id = @clinic_id AND client_id = @client_id
        )
        SELECT wf.submitted_at, wf.utm_source, wf.utm_medium, wf.utm_campaign,
               wf.landing_page, wf.customer_type
        FROM `{_CLINIC_DATA}.webforms` wf, contact
        WHERE wf.clinic_id = @clinic_id
          AND (
            RIGHT(REGEXP_REPLACE(IFNULL(wf.phone_number,''), r'\\D',''),10) IN UNNEST(contact.phones)
            OR LOWER(TRIM(IFNULL(wf.email,''))) IN UNNEST(contact.emails)
          )
        ORDER BY wf.submitted_at DESC
        LIMIT 50
    """, params):
        out["forms"].append({
            "submitted_at": str(r.submitted_at) if r.submitted_at else None,
            "utm_source": r.utm_source, "utm_medium": r.utm_medium,
            "utm_campaign": r.utm_campaign, "landing_page": r.landing_page,
            "customer_type": r.customer_type,
        })

    # ── PMS appointments ──
    # ``event_type`` is the controlled appointment-type label; ``title`` is
    # deliberately NOT selected — it's a free-text column that may carry
    # staff-entered clinical notes (PHI).
    for r in _run(f"""
        SELECT event_type, start_time, status_2
        FROM `{_BP}.Appointments`
        WHERE _clinic_id = @clinic_id AND client_id = @client_id
        ORDER BY SAFE_CAST(start_time AS TIMESTAMP) DESC
        LIMIT 100
    """, params):
        out["appointments"].append({
            "event_type": r.event_type, "start_time": r.start_time,
            "status": r.status_2,
        })

    # ── Invoices ──
    for r in _run(f"""
        SELECT invoice_date,
               SAFE_CAST(order_total_with_tax AS NUMERIC) AS total
        FROM `{_BP}.InvoiceMaster`
        WHERE _clinic_id = @clinic_id AND client_id = @client_id
          AND SAFE_CAST(order_total_with_tax AS NUMERIC) > 0
        ORDER BY SAFE.PARSE_DATE('%Y-%m-%d', invoice_date) DESC
        LIMIT 100
    """, params):
        out["invoices"].append({
            "invoice_date": r.invoice_date,
            "total": float(r.total) if r.total is not None else 0.0,
        })

    # ── Devices ──
    for r in _run(f"""
        SELECT m.model_name, a.side, a.purchase_date, a.status, a.warranty_expiry_date
        FROM `{_BP}.ClientAids` a
        {_hearing_aid_join_sql("a", "m")}
        WHERE a._clinic_id = @clinic_id AND a.client_id = @client_id
        ORDER BY SAFE.PARSE_DATE('%Y-%m-%d', a.purchase_date) DESC
        LIMIT 50
    """, params):
        out["devices"].append({
            "model_name": r.model_name, "side": r.side,
            "purchase_date": r.purchase_date, "status": r.status,
            "warranty_expiry_date": r.warranty_expiry_date,
        })

    return out


# ════════════════════════════════════════════════════════════════════════════
# Active-lead sourcing & enrichment (powers the scored recovery inbox)
# ════════════════════════════════════════════════════════════════════════════

def open_form_leads(
    clinic_id: str,
    days: int = 90,
    window: "Window | None" = None,
) -> list[dict]:
    """Web-form submissions in the window that have NOT resulted in a booked
    appointment since (still-open leads). Unmatched submitters (not yet in the
    PMS) are kept — they're the freshest brand-new leads."""
    w = _win(window, days)
    try:
        rows = list(_client().query(f"""
            WITH forms AS (
              SELECT
                ROW_NUMBER() OVER (ORDER BY submitted_at, email, phone_number) AS form_id,
                first_name, last_name, phone_number, email, customer_type, message,
                utm_source, utm_medium, utm_campaign, landing_page, submitted_at,
                DATE(submitted_at)                                           AS sd,
                RIGHT(REGEXP_REPLACE(IFNULL(phone_number,''), r'\\D',''), 10) AS phone_norm,
                LOWER(TRIM(IFNULL(email,'')))                                AS email_norm
              FROM `{_CLINIC_DATA}.webforms`
              WHERE clinic_id = @clinic_id AND {_ts_between('submitted_at', w)}
            ),
            patients AS (
              SELECT DISTINCT client_id, phone_norm, email_norm
              FROM `{_PATIENT_CONTACTS}` WHERE _clinic_id = @clinic_id
            ),
            matched AS (
              SELECT f.form_id, f.sd, p.client_id
              FROM forms f JOIN patients p
                ON (LENGTH(f.phone_norm)=10 AND f.phone_norm=p.phone_norm)
                OR (f.email_norm != '' AND f.email_norm=p.email_norm)
            ),
            booked AS (
              SELECT DISTINCT m.form_id
              FROM matched m JOIN `{_BP}.Appointments` a
                ON a._clinic_id = @clinic_id AND a.client_id = m.client_id
              WHERE SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(a.start_time,1,10)) >= m.sd
            )
            SELECT first_name, last_name, phone_number, email, customer_type, message,
                   utm_source, utm_medium, utm_campaign, landing_page, submitted_at,
                   phone_norm, email_norm
            FROM forms
            WHERE form_id NOT IN (SELECT form_id FROM booked)
            ORDER BY submitted_at DESC
        """, job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id))).result())
    except Exception as exc:
        log.warning("open_form_leads failed clinic=%s: %s", clinic_id, exc)
        return []
    return [{
        "first_name": r.first_name or "", "last_name": r.last_name or "",
        "phone_number": r.phone_number, "email": r.email,
        "customer_type": r.customer_type, "message": r.message,
        "utm_source": r.utm_source, "utm_medium": r.utm_medium,
        "utm_campaign": r.utm_campaign, "landing_page": r.landing_page,
        "submitted_at": str(r.submitted_at) if r.submitted_at else None,
        "phone_norm": r.phone_norm or "", "email_norm": r.email_norm or "",
    } for r in rows]


def lead_pms_enrichment(clinic_id: str, phones: list[str], emails: list[str]) -> dict[str, Any]:
    """For a batch of normalized phones/emails, resolve PMS matches and per-client
    enrichment used to value & auto-resolve leads.

    Returns ``{"by_phone": {phone_norm: client_id}, "by_email": {email_norm:
    client_id}, "clients": {client_id: {...}}}`` where each client carries names,
    status, opt-out flags, invoice history (count / total / avg), and the latest
    appointment & invoice dates (for booked-since auto-resolution)."""
    out: dict[str, Any] = {"by_phone": {}, "by_email": {}, "clients": {}}
    phones = [p for p in (phones or []) if p and len(p) == 10]
    emails = [e for e in (emails or []) if e]
    if not phones and not emails:
        return out
    client = _client()
    params = [
        bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
        bigquery.ArrayQueryParameter("phones", "STRING", phones),
        bigquery.ArrayQueryParameter("emails", "STRING", emails),
    ]
    try:
        rows = list(client.query(f"""
            SELECT phone_norm, email_norm, client_id
            FROM `{_PATIENT_CONTACTS}`
            WHERE _clinic_id = @clinic_id
              AND ((LENGTH(phone_norm)=10 AND phone_norm IN UNNEST(@phones))
                   OR (email_norm != '' AND email_norm IN UNNEST(@emails)))
        """, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())
    except Exception as exc:
        log.warning("lead_pms_enrichment match failed clinic=%s: %s", clinic_id, exc)
        return out

    client_ids = set()
    for r in rows:
        cid = r.client_id
        if not cid:
            continue
        client_ids.add(cid)
        if r.phone_norm and len(r.phone_norm) == 10:
            out["by_phone"].setdefault(r.phone_norm, cid)
        if r.email_norm:
            out["by_email"].setdefault(r.email_norm, cid)
    if not client_ids:
        return out

    cid_params = [
        bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
        bigquery.ArrayQueryParameter("client_ids", "STRING", list(client_ids)),
    ]
    try:
        crows = list(client.query(f"""
            WITH inv AS (
              SELECT client_id,
                     COUNT(*) AS invoice_count,
                     SUM(SAFE_CAST(order_total_with_tax AS NUMERIC)) AS total_revenue,
                     MAX(SAFE.PARSE_DATE('%Y-%m-%d', invoice_date)) AS max_invoice_date
              FROM `{_BP}.InvoiceMaster`
              WHERE _clinic_id = @clinic_id AND client_id IN UNNEST(@client_ids)
                AND SAFE_CAST(order_total_with_tax AS NUMERIC) > 0
              GROUP BY client_id
            ),
            appt AS (
              SELECT client_id,
                     MAX(SAFE_CAST(start_time AS TIMESTAMP)) AS max_appt_ts
              FROM `{_BP}.Appointments`
              WHERE _clinic_id = @clinic_id AND client_id IN UNNEST(@client_ids)
              GROUP BY client_id
            )
            SELECT
              cd.client_id, cd.given_name, cd.surname, cd.status,
              cd.do_not_send_commercial_messages, cd.do_not_text,
              inv.invoice_count, inv.total_revenue, inv.max_invoice_date,
              appt.max_appt_ts
            FROM `{_BP}.ClientDemographics` cd
            LEFT JOIN inv  ON inv.client_id  = cd.client_id
            LEFT JOIN appt ON appt.client_id = cd.client_id
            WHERE cd._clinic_id = @clinic_id AND cd.client_id IN UNNEST(@client_ids)
        """, job_config=bigquery.QueryJobConfig(query_parameters=cid_params)).result())
    except Exception as exc:
        log.warning("lead_pms_enrichment clients failed clinic=%s: %s", clinic_id, exc)
        crows = []

    for r in crows:
        count = int(r.invoice_count or 0)
        total = float(r.total_revenue or 0.0)
        out["clients"][r.client_id] = {
            "given_name": r.given_name or "", "surname": r.surname or "",
            "status": r.status,
            "do_not_contact": _truthy_flag(r.do_not_send_commercial_messages),
            "do_not_text": _truthy_flag(r.do_not_text),
            "invoice_count": count,
            "total_revenue": total,
            "avg_invoice": (total / count) if count else 0.0,
            "max_invoice_date": str(r.max_invoice_date) if r.max_invoice_date else None,
            "max_appt_date": (r.max_appt_ts.date().isoformat() if r.max_appt_ts else None),
        }
    return out


def lifecycle_client_ids(clinic_id: str) -> dict[str, set]:
    """Current-state lifecycle membership sets keyed by ``client_id`` — used to
    bump a lead's value when the caller is also a warranty / upgrade / tested-
    not-sold opportunity. Best-effort; a failing segment yields an empty set."""
    def _ids(fn) -> set:
        try:
            return {r.get("client_id") for r in fn() if r.get("client_id")}
        except Exception as exc:
            log.warning("lifecycle_client_ids part failed clinic=%s: %s", clinic_id, exc)
            return set()
    return {
        "warranty": _ids(lambda: warranty_expiring_detail(clinic_id)),
        "upgrade": _ids(lambda: upgrade_candidates_detail(clinic_id)),
        "tested_not_sold": _ids(lambda: fitting_no_purchase_detail(clinic_id)),
    }
