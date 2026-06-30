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
import logging
from typing import Any

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
MIN_WINDOW_DATE = _dt.date(2026, 1, 1)

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
        """Start snapped to the *first of its month*. ``ad_groups`` is
        monthly-grained (its ``timestamp`` is always the first of the month),
        so a window starting mid-month must still include that whole month —
        otherwise spend drops a month whose clicks/calls ARE counted, which
        would distort CPC/ROAS."""
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

def google_ads_roi(clinic_id: str, ga_campaign_ids: list[str], days: int = 90, window: "Window | None" = None) -> list[dict]:
    """Per-campaign cascade for the clinic's linked Google Ads campaigns.

    Pulls clicks from ``ad_clicks_v2``, monthly spend from ``ad_groups`` (cost
    derived as ``metrics_clicks * metrics_average_cpc / 1e6`` — Google Ads
    reports CPC in micros), and the calls-and-bookings tied back to those
    clicks via GCLID matching against ``transactions``. GCLID-matched callers
    are further phone-joined to ``Blueprint_PHI.ClientDemographics`` and their
    invoices summed to give per-campaign revenue and ROAS.

    Returns one dict per campaign with derived ratios (CPC, cost-per-call,
    cost-per-booking, ROAS). Per-campaign revenue dedups invoices within each
    campaign — the same patient counted against two campaigns will have their
    invoice show up in both columns (Virsono attribution convention).
    Skips campaigns with zero clicks in the window.
    """
    if not ga_campaign_ids:
        return []
    w = _win(window, days)
    in_list = "(" + ", ".join(f"'{c}'" for c in ga_campaign_ids) + ")"
    sql = f"""
        WITH clicks AS (
            SELECT
              google_ads_campaign_id,
              ANY_VALUE(campaign_name) AS campaign_name,
              COUNT(*) AS clicks
            FROM `{_CLINIC_DATA}.ad_clicks_v2`
            WHERE google_ads_campaign_id IN {in_list}
              AND {_ts_between("timestamp", w)}
            GROUP BY google_ads_campaign_id
        ),
        spend AS (
            SELECT
              google_ads_campaign_id,
              SUM(SAFE_CAST(metrics_clicks AS FLOAT64))                                AS ad_group_clicks,
              SUM(SAFE_CAST(metrics_clicks AS FLOAT64) * metrics_average_cpc / 1e6)    AS spend
            FROM `{_CLINIC_DATA}.ad_groups`
            WHERE google_ads_campaign_id IN {in_list}
              AND timestamp >= TIMESTAMP('{w.start_month_ts}') AND timestamp < TIMESTAMP('{w.end_ts}')
            GROUP BY google_ads_campaign_id
        ),
        gads_call_detail AS (
            -- Booking comes from LLM-scored callscoring.appointment_booked,
            -- matching the rest of the funnel. Calls without a callscoring
            -- row contribute 0 to `booked`.
            SELECT
              ac.google_ads_campaign_id,
              t.transaction_id,
              RIGHT(REGEXP_REPLACE(IFNULL(t.calling_phone_number, ''), r'\\D', ''), 10) AS phone_norm,
              CASE WHEN IFNULL(cs.appointment_booked, FALSE) THEN 1 ELSE 0 END AS booked
            FROM `{_CLINIC_DATA}.transactions` t
            INNER JOIN `{_CLINIC_DATA}.ad_clicks_v2` ac
              ON ac.click_view_gclid = t.gclid
             AND ac.google_ads_campaign_id IN {in_list}
            LEFT JOIN `{_CLINIC_DATA}.callscoring` cs
              ON cs.complete_call_id = t.complete_call_id
            WHERE {_ts_between("t.timestamp", w)}
              AND t.gclid IS NOT NULL AND t.gclid != ''
        ),
        gads_calls AS (
            SELECT
              google_ads_campaign_id,
              COUNT(DISTINCT transaction_id) AS calls,
              SUM(booked)                    AS booked
            FROM gads_call_detail
            GROUP BY google_ads_campaign_id
        ),
        patients AS (
            SELECT DISTINCT client_id, phone_norm
            FROM `{_PATIENT_CONTACTS}`
            WHERE _clinic_id = @clinic_id
              AND LENGTH(phone_norm) = 10
        ),
        -- Distinct patients touched by each campaign (via GCLID-matched call
        -- whose phone matches a Blueprint patient).
        campaign_clients AS (
            SELECT DISTINCT
              gc.google_ads_campaign_id,
              p.client_id
            FROM gads_call_detail gc
            JOIN patients p
              ON p.phone_norm = gc.phone_norm
             AND LENGTH(gc.phone_norm) = 10
        ),
        campaign_revenue AS (
            SELECT
              cc.google_ads_campaign_id,
              SUM(SAFE_CAST(im.order_total_with_tax AS NUMERIC)) AS revenue,
              COUNT(DISTINCT im.order_id) AS invoice_count
            FROM campaign_clients cc
            JOIN `{_BP}.InvoiceMaster` im
              ON im._clinic_id = @clinic_id
             AND im.client_id = cc.client_id
            WHERE SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
              AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)", w)}
            GROUP BY cc.google_ads_campaign_id
        )
        SELECT
          cl.google_ads_campaign_id AS campaign_id,
          cl.campaign_name,
          cl.clicks,
          COALESCE(gc.calls, 0)     AS calls,
          COALESCE(gc.booked, 0)    AS booked,
          COALESCE(s.spend, 0)      AS spend,
          COALESCE(cr.revenue, 0)   AS revenue,
          COALESCE(cr.invoice_count, 0) AS invoice_count
        FROM clicks cl
        LEFT JOIN spend s USING (google_ads_campaign_id)
        LEFT JOIN gads_calls gc USING (google_ads_campaign_id)
        LEFT JOIN campaign_revenue cr USING (google_ads_campaign_id)
        ORDER BY cl.clicks DESC
    """
    client = _client()
    out: list[dict] = []
    job_config = bigquery.QueryJobConfig(query_parameters=_params(clinic_id))
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
        out.append({
            "campaign_id":       r.campaign_id,
            "campaign_name":     r.campaign_name or r.campaign_id,
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
              COALESCE(NULLIF(TRIM(utm_source), ''), 'Direct / untagged')      AS source,
              RIGHT(REGEXP_REPLACE(IFNULL(phone_number, ''), r'\\D', ''), 10)  AS phone_norm,
              LOWER(TRIM(IFNULL(email, '')))                                   AS email_norm,
              DATE(submitted_at)                                              AS submitted_date
            FROM `{_CLINIC_DATA}.webforms`
            WHERE clinic_id = @clinic_id
              AND {_ts_between("submitted_at", w)}
        ),
        submissions AS (
            SELECT source, COUNT(*) AS submissions FROM forms GROUP BY source
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
# covered without enumerating every clinic's spelling.
_FITTING_EVENT_LIKE = "%fitting%"

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


def fitting_no_purchase_detail(
    clinic_id: str,
    days: int = 365,
    limit: int | None = None,
    window: "Window | None" = None,
) -> list[dict]:
    """Patients who had a fitting appointment but no hearing-aid purchase.

    A "capitalize" worklist: someone came in for a fitting but never bought a
    hearing aid (no `item_type IN _HA_ITEM_TYPES` invoice line for that client).
    Compliance flags are surfaced (not silently dropped) so the front desk can
    see opt-outs before acting.
    """
    w = _win(window, days)
    client = _client()
    ha_types = "(" + ", ".join(f"'{t}'" for t in _HA_ITEM_TYPES) + ")"
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    rows = list(client.query(
        f"""
            WITH fittings AS (
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
                AND LOWER(event_type) LIKE @fitting_like
                AND status_2 IN UNNEST(@completed_statuses)
                AND {_ts_between("SAFE_CAST(start_time AS TIMESTAMP)", w)}
            ),
            ha_clients AS (
              -- Exclude only patients who bought a hearing aid WITHIN the same
              -- window — a fitting that didn't convert is the signal, regardless
              -- of a purchase years ago. Also bounds the InvoiceLineItems scan.
              SELECT DISTINCT client_id
              FROM `{_BP}.InvoiceLineItems`
              WHERE _clinic_id = @clinic_id
                AND LOWER(item_type) IN {ha_types}
                AND {_date_between("SAFE.PARSE_DATE('%Y-%m-%d', invoice_date)", w)}
            )
            SELECT
              f.client_id,
              cd.given_name,
              cd.surname,
              f.event_type                          AS appt_event_type,
              f.start_time                           AS appt_start_time,
              f.status_2                             AS appt_status,
              COALESCE(NULLIF(cd.status, ''), 'Unknown') AS patient_status,
              COALESCE(cd.do_not_send_commercial_messages, '') AS do_not_send_commercial_messages,
              COALESCE(cd.do_not_text, '')           AS do_not_text
            FROM fittings f
            LEFT JOIN ha_clients h USING (client_id)
            LEFT JOIN `{_BP}.ClientDemographics` cd
              ON cd._clinic_id = @clinic_id AND cd.client_id = f.client_id
            WHERE f.rn = 1
              AND h.client_id IS NULL
            ORDER BY SAFE_CAST(f.start_time AS TIMESTAMP) DESC
            {limit_clause}
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ScalarQueryParameter("fitting_like", "STRING", _FITTING_EVENT_LIKE),
            bigquery.ArrayQueryParameter(
                "completed_statuses", "STRING", list(_STATUS_COMPLETED)),
        ]),
    ).result())
    return [
        {
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
        for r in rows
    ]


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


def form_capture(
    clinic_id: str,
    days: int = 90,
    window: "Window | None" = None,
) -> dict[str, Any]:
    """Web-form volume + capture for the window: ``submissions``,
    ``form_bookings`` (submitter matched to a PMS patient with an appointment
    on/after submission) and ``form_rate`` = form_bookings / submissions."""
    w = _win(window, days)
    out = {"submissions": 0, "form_bookings": 0, "form_rate": None}
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
        return out
    if rows:
        r = rows[0]
        out["submissions"] = int(r.submissions or 0)
        out["form_bookings"] = int(r.form_bookings or 0)
        out["form_rate"] = (out["form_bookings"] / out["submissions"]) if out["submissions"] else None
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
            return {"contacts": 0, "calls": 0, "forms": 0, "connected": 0,
                    "booked": 0, "capture_rate": None, "form_rate": None, "revenue": 0.0}
        calls = call_capture(clinic_id, invoca_campaign_ids, window=win)
        forms = form_capture(clinic_id, window=win)
        rev = invoice_revenue(clinic_id, window=win)
        return {
            "contacts": calls["calls"] + forms["submissions"],
            "calls": calls["calls"],
            "forms": forms["submissions"],
            "connected": calls["connected"],
            "booked": calls["booked"],
            "capture_rate": calls["capture_rate"],
            "form_rate": forms["form_rate"],
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
