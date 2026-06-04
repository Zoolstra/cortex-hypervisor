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

_BP = "project-demo-2-482101.Blueprint_PHI"
_CLINIC_DATA = "project-demo-2-482101.ClinicData"


def _client() -> bigquery.Client:
    return bigquery.Client(project="project-demo-2-482101")


def _params(clinic_id: str, **extra) -> list[bigquery.ScalarQueryParameter]:
    out = [bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id)]
    for k, v in extra.items():
        out.append(bigquery.ScalarQueryParameter(k, "STRING", v))
    return out


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


def _window_start_date(days: int) -> str:
    """``YYYY-MM-DD`` for midnight UTC, ``days`` days ago. Matches the old
    ``DATE_SUB(CURRENT_DATE(), INTERVAL days DAY)`` exactly."""
    start = _dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=int(days))
    return start.isoformat()


def _window_start_ts(days: int) -> str:
    """UTC timestamp literal (midnight, ``days`` days ago) for embedding in a
    ``TIMESTAMP('…')`` constructor."""
    return f"{_window_start_date(days)} 00:00:00+00:00"


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

def appointment_outcomes(clinic_id: str, days: int = 365) -> dict[str, Any]:
    """Roll up appointments by ``status_2`` over the last ``days``.

    Blueprint's numeric ``status`` codes map to ``status_2`` strings:
      7=Completed, 2=Tentative, 0=Confirmed, 3=Cancelled, 5=Arrived,
      9=Ready, 1=No show, 6=In progress, 4=Left message, 8=No answer.

    Returns ``{total, by_status: {label: count}, sales_opportunities, …}``.
    """
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
              AND SAFE_CAST(start_time AS TIMESTAMP)
                  >= TIMESTAMP('{_window_start_ts(days)}')
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
        "window_days":         days,
    }


# ── Invoices ─────────────────────────────────────────────────────────────────

def invoice_revenue(clinic_id: str, days: int = 365) -> dict[str, Any]:
    """Total invoice revenue + count over the window.

    Blueprint stores ``order_total_with_tax`` as STRING — we cast to NUMERIC.
    Zero-total invoices are excluded from the count (they're typically credit
    notes / placeholders, not actual sales).
    """
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
              AND SAFE.PARSE_DATE('%Y-%m-%d', invoice_date)
                  >= DATE '{_window_start_date(days)}'
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
        "window_days":   days,
    }


# ── Referral sources ─────────────────────────────────────────────────────────

def referral_breakdown(clinic_id: str, days: int = 365, top_n: int = 10) -> list[dict]:
    """Top referral sources by invoice revenue.

    Joins InvoiceMaster → ReferralSources on (type_id, source_id). Falls back
    to 'Unknown' when the referrer fields are blank. Aggregates over the
    rolling window and returns the top ``top_n`` plus an "Other" bucket.
    """
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
                  AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)
                      >= DATE '{_window_start_date(days)}'
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

def line_item_mix(clinic_id: str, days: int = 365) -> list[dict]:
    """Revenue + line-count by ``item_type`` over the window.

    Surfaces hearing-aid revenue vs accessories vs services etc.
    """
    client = _client()
    rows = list(client.query(
        f"""
            SELECT
              COALESCE(NULLIF(item_type, ''), 'Unknown') AS item_type,
              COUNT(*)                                   AS line_count,
              SUM(SAFE_CAST(price AS NUMERIC))           AS revenue
            FROM `{_BP}.InvoiceLineItems`
            WHERE _clinic_id = @clinic_id
              AND SAFE.PARSE_DATE('%Y-%m-%d', invoice_date)
                  >= DATE '{_window_start_date(days)}'
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

def google_ads_roi(clinic_id: str, ga_campaign_ids: list[str], days: int = 90) -> list[dict]:
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
    in_list = "(" + ", ".join(f"'{c}'" for c in ga_campaign_ids) + ")"
    sql = f"""
        WITH clicks AS (
            SELECT
              google_ads_campaign_id,
              ANY_VALUE(campaign_name) AS campaign_name,
              COUNT(*) AS clicks
            FROM `{_CLINIC_DATA}.ad_clicks_v2`
            WHERE google_ads_campaign_id IN {in_list}
              AND timestamp >= TIMESTAMP('{_window_start_ts(days)}')
            GROUP BY google_ads_campaign_id
        ),
        spend AS (
            SELECT
              google_ads_campaign_id,
              SUM(SAFE_CAST(metrics_clicks AS FLOAT64))                                AS ad_group_clicks,
              SUM(SAFE_CAST(metrics_clicks AS FLOAT64) * metrics_average_cpc / 1e6)    AS spend
            FROM `{_CLINIC_DATA}.ad_groups`
            WHERE google_ads_campaign_id IN {in_list}
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
            WHERE t.timestamp >= TIMESTAMP('{_window_start_ts(days)}')
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
            SELECT DISTINCT
              cd.client_id,
              RIGHT(REGEXP_REPLACE(phone, r'\\D', ''), 10) AS phone_norm
            FROM `{_BP}.ClientDemographics` cd,
            UNNEST([cd.home_telephone_no, cd.work_telephone_no, cd.mobile_telephone_no]) AS phone
            WHERE cd._clinic_id = @clinic_id
              AND phone IS NOT NULL
              AND LENGTH(RIGHT(REGEXP_REPLACE(phone, r'\\D', ''), 10)) = 10
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
              AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)
                  >= DATE '{_window_start_date(days)}'
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


# ── End-to-end marketing funnel ──────────────────────────────────────────────

def marketing_funnel(
    clinic_id: str,
    ga_campaign_ids: list[str],
    invoca_campaign_ids: list[str],
    days: int = 90,
) -> dict[str, int]:
    """Funnel stage counts over the window:

      clicks    — ad_clicks_v2 rows for the clinic's GA campaigns
      calls     — transactions rows for the clinic's Invoca campaigns
      answered  — calls where Answered_by_Agent fired
      discussed — calls where appointment was discussed OR AI flagged an opp
      booked    — calls where AI_Opportunity / AI_Appointment_Booked fired

    Empty Invoca list short-circuits the call funnel. Empty GA list
    short-circuits clicks. Both stages return zero independently so the
    section renders even when only one side is configured.
    """
    out = {"clicks": 0, "calls": 0, "answered": 0, "discussed": 0, "booked": 0}
    client = _client()

    if ga_campaign_ids:
        in_ga = "(" + ", ".join(f"'{c}'" for c in ga_campaign_ids) + ")"
        rows = list(client.query(f"""
            SELECT COUNT(*) AS n
            FROM `{_CLINIC_DATA}.ad_clicks_v2`
            WHERE google_ads_campaign_id IN {in_ga}
              AND timestamp >= TIMESTAMP('{_window_start_ts(days)}')
        """).result())
        out["clicks"] = int(rows[0].n or 0)

    if invoca_campaign_ids:
        in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
        rows = list(client.query(f"""
            SELECT
              COUNT(*)                                                          AS calls,
              COUNTIF(IFNULL(Answered_by_Agent, 0) >= 1)                        AS answered,
              COUNTIF(IFNULL(Appointment_Discussed__Industry_, 0) >= 1
                      OR IFNULL(AI_Opportunity, 0) >= 1
                      OR IFNULL(AI_Appointment_Booked, 0) >= 1)                 AS discussed,
              COUNTIF(IFNULL(AI_Opportunity, 0) >= 1
                      OR IFNULL(AI_Appointment_Booked, 0) >= 1)                 AS booked
            FROM `{_CLINIC_DATA}.transactions`
            WHERE CAST(invoca_campaign_id AS STRING) IN {in_iv}
              AND timestamp >= TIMESTAMP('{_window_start_ts(days)}')
        """).result())
        r = rows[0]
        out["calls"]     = int(r.calls or 0)
        out["answered"]  = int(r.answered or 0)
        out["discussed"] = int(r.discussed or 0)
        out["booked"]    = int(r.booked or 0)
    return out


# ── Inbound call volume (when clinic has linked Invoca campaigns) ────────────

def inbound_calls(clinic_id: str, campaign_ids: list[str], days: int = 90) -> dict[str, Any]:
    """Inbound call funnel from Invoca, filtered to the clinic's linked
    ``invoca_campaign_id``s in ``ClinicData.transactions``.

    Returns answered / booked / discussed counts. Returns zeros if the clinic
    has no campaigns linked yet.
    """
    if not campaign_ids:
        return {
            "calls": 0, "answered": 0, "discussed": 0, "booked": 0,
            "window_days": days, "linked_campaigns": [],
        }
    client = _client()
    in_clause = "(" + ", ".join(f"'{c}'" for c in campaign_ids) + ")"
    # Column in ClinicData.transactions is `invoca_campaign_id` (renamed from
    # `advertiser_campaign_id` in the Invoca ingest before WRITE). The
    # boolean-ish signal columns (`Answered_by_Agent`, `AI_Opportunity`, etc.)
    # are FLOAT64 in BQ with values 0.0 / 1.0 / NULL — use `>= 1` so NULLs
    # don't poison the comparison.
    rows = list(client.query(
        f"""
            SELECT
              COUNT(*)                                                          AS calls,
              COUNTIF(IFNULL(Answered_by_Agent, 0) >= 1)                        AS answered,
              COUNTIF(IFNULL(Appointment_Discussed__Industry_, 0) >= 1
                      OR IFNULL(AI_Opportunity, 0) >= 1
                      OR IFNULL(AI_Appointment_Booked, 0) >= 1)                 AS discussed,
              COUNTIF(IFNULL(AI_Opportunity, 0) >= 1
                      OR IFNULL(AI_Appointment_Booked, 0) >= 1)                 AS booked
            FROM `{_CLINIC_DATA}.transactions`
            WHERE CAST(invoca_campaign_id AS STRING) IN {in_clause}
              AND timestamp >= TIMESTAMP('{_window_start_ts(days)}')
        """,
    ).result())
    r = rows[0]
    return {
        "calls":            int(r.calls or 0),
        "answered":         int(r.answered or 0),
        "discussed":        int(r.discussed or 0),
        "booked":           int(r.booked or 0),
        "window_days":      days,
        "linked_campaigns": campaign_ids,
    }


# ── Marketing channel mix (for the Sankey preamble) ──────────────────────────

def channel_mix(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
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
    client = _client()
    scope = _spam_scope_clause(invoca_campaign_ids, days)
    join_cs = _callscoring_join_sql()
    not_spam = _non_spam_predicate_sql()
    rows = list(client.query(f"""
        SELECT
          COALESCE(NULLIF(t.marketing_channel, ''), 'Untagged') AS channel,
          COUNT(*) AS n
        FROM `{_CLINIC_DATA}.transactions` t
        {join_cs}
        WHERE {scope}
          AND {not_spam}
        GROUP BY channel
        ORDER BY n DESC
    """).result())
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
    out: dict[str, Any] = {
        "clicks": 0, "calls": 0,
        "spam": 0, "voicemail_hangup": 0,
        "answered": 0, "discussed": 0, "booked": 0,
        "matched_patient": 0, "appt_within_window": 0, "appt_completed": 0,
        "invoiced": 0, "matched_revenue": 0.0,
        "window_days": days, "booking_window_hours": booking_window_hours,
    }
    client = _client()

    # Clicks live in ClinicData; no patient join needed.
    if ga_campaign_ids:
        in_ga = "(" + ", ".join(f"'{c}'" for c in ga_campaign_ids) + ")"
        rows = list(client.query(f"""
            SELECT COUNT(*) AS n
            FROM `{_CLINIC_DATA}.ad_clicks_v2`
            WHERE google_ads_campaign_id IN {in_ga}
              AND timestamp >= TIMESTAMP('{_window_start_ts(days)}')
        """).result())
        out["clicks"] = int(rows[0].n or 0)

    if not invoca_campaign_ids:
        return out

    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    hrs = int(booking_window_hours)
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
              AND t.timestamp >= TIMESTAMP('{_window_start_ts(days)}')
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
            SELECT DISTINCT
              cd.client_id,
              RIGHT(REGEXP_REPLACE(phone, r'\\D', ''), 10) AS phone_norm
            FROM `{_BP}.ClientDemographics` cd,
            UNNEST([cd.home_telephone_no, cd.work_telephone_no, cd.mobile_telephone_no]) AS phone
            WHERE cd._clinic_id = @clinic_id
              AND phone IS NOT NULL
              AND LENGTH(RIGHT(REGEXP_REPLACE(phone, r'\\D', ''), 10)) = 10
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
                  AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)
                      >= DATE '{_window_start_date(days)}'
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
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
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


def non_booked_journey(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    booking_window_hours: int = 24,
) -> dict[str, int]:
    """Bucket each non-booked call by where the caller landed in Blueprint.

    "Non-booked" = Invoca's AI flag (AI_Opportunity / AI_Appointment_Booked)
    did not fire. The buckets are mutually exclusive — each call lands in
    exactly one — with the precedence: converted > completed_no_invoice >
    cancelled > no_show > scheduled_future > patient_no_appt > ghost.

    Returns ``{bucket_name: count, ...}`` over the rolling window. Always
    includes all seven keys, even if zero. ``ghost`` = no patient match;
    ``patient_no_appt`` = known patient but no appointment booked from this
    call (within ``booking_window_hours``); ``converted`` catches bookings
    Invoca missed where the resulting visit produced an invoice.
    """
    buckets = {
        "ghost": 0, "patient_no_appt": 0, "scheduled_future": 0,
        "cancelled": 0, "no_show": 0, "completed_no_invoice": 0,
        "converted": 0,
    }
    if not invoca_campaign_ids:
        return buckets

    client = _client()
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    hrs = int(booking_window_hours)
    sql = f"""
        WITH calls AS (
            SELECT
              transaction_id,
              RIGHT(REGEXP_REPLACE(IFNULL(calling_phone_number, ''), r'\\D', ''), 10) AS phone_norm,
              SAFE_CAST(timestamp AS TIMESTAMP) AS call_ts
            FROM `{_CLINIC_DATA}.transactions`
            WHERE CAST(invoca_campaign_id AS STRING) IN {in_iv}
              AND timestamp >= TIMESTAMP('{_window_start_ts(days)}')
              AND IFNULL(AI_Opportunity, 0) < 1
              AND IFNULL(AI_Appointment_Booked, 0) < 1
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
        call_x_patient AS (
            SELECT c.transaction_id, c.call_ts, p.client_id
            FROM calls c
            LEFT JOIN patients p
              ON p.phone_norm = c.phone_norm
              AND LENGTH(c.phone_norm) = 10
        ),
        call_state AS (
            SELECT
              cxp.transaction_id,
              LOGICAL_OR(cxp.client_id IS NOT NULL) AS matched_patient,
              LOGICAL_OR(a.event_id IS NOT NULL)    AS has_appt,
              LOGICAL_OR(a.status_2 IN UNNEST({list(_STATUS_COMPLETED)})) AS has_completed,
              LOGICAL_OR(a.status_2 IN UNNEST({list(_STATUS_CANCELLED)})) AS has_cancelled,
              LOGICAL_OR(a.status_2 IN UNNEST({list(_STATUS_NO_SHOW)}))   AS has_no_show,
              LOGICAL_OR(a.status_2 IN UNNEST({list(_STATUS_FUTURE)}))    AS has_future,
              LOGICAL_OR(im.order_id IS NOT NULL)   AS has_invoice
            FROM call_x_patient cxp
            LEFT JOIN `{_BP}.Appointments` a
              ON a._clinic_id = @clinic_id
              AND a.client_id = cxp.client_id
              AND SAFE_CAST(a.created_time AS TIMESTAMP) >= cxp.call_ts
              AND SAFE_CAST(a.created_time AS TIMESTAMP)
                  <= TIMESTAMP_ADD(cxp.call_ts, INTERVAL {hrs} HOUR)
            LEFT JOIN `{_BP}.InvoiceMaster` im
              ON im._clinic_id = @clinic_id
              AND im.client_id = cxp.client_id
              AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date) >= DATE(cxp.call_ts)
              AND SAFE_CAST(im.order_total_with_tax AS NUMERIC) > 0
            GROUP BY cxp.transaction_id
        )
        SELECT
          CASE
            WHEN has_completed AND has_invoice     THEN 'converted'
            WHEN has_completed                     THEN 'completed_no_invoice'
            WHEN has_cancelled                     THEN 'cancelled'
            WHEN has_no_show                       THEN 'no_show'
            WHEN has_future OR has_appt            THEN 'scheduled_future'
            WHEN matched_patient                   THEN 'patient_no_appt'
            ELSE                                        'ghost'
          END AS bucket,
          COUNT(*) AS n
        FROM call_state
        GROUP BY bucket
    """
    rows = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=_params(clinic_id)),
    ).result()
    for r in rows:
        if r.bucket in buckets:
            buckets[r.bucket] = int(r.n or 0)
    return buckets


# ── Attributed invoice detail (per-row drill-down) ───────────────────────────

def attributed_invoice_detail(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 365,
    booking_window_hours: int = 24,
    limit: int | None = None,
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
              AND timestamp >= TIMESTAMP('{_window_start_ts(days)}')
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
          AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)
              >= DATE '{_window_start_date(days)}'
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
) -> int:
    """Total count of (patient × invoice) rows the attributed-invoices query
    would return — used by the cohort banner without materialising every row.
    """
    if not invoca_campaign_ids:
        return 0
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
              AND timestamp >= TIMESTAMP('{_window_start_ts(days)}')
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
          AND SAFE.PARSE_DATE('%Y-%m-%d', im.invoice_date)
              >= DATE '{_window_start_date(days)}'
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
) -> dict[str, int]:
    """Total calls + ad-driven calls (gclid present) for the clinic's Invoca
    campaigns within the rolling window.

    Returns ``{"total_calls", "ad_driven_calls", "ad_driven_pct"}``. Empty
    Invoca list → all zeros (the clinic isn't tracking calls).
    """
    if not invoca_campaign_ids:
        return {"total_calls": 0, "ad_driven_calls": 0, "ad_driven_pct": 0.0}

    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    client = _client()
    rows = list(client.query(f"""
        SELECT
          COUNT(*)                                                 AS total_calls,
          COUNTIF(gclid IS NOT NULL AND gclid != '')               AS ad_driven_calls
        FROM `{_CLINIC_DATA}.transactions`
        WHERE CAST(invoca_campaign_id AS STRING) IN {in_iv}
          AND timestamp >= TIMESTAMP('{_window_start_ts(days)}')
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
            AND t.timestamp >= TIMESTAMP('{_window_start_ts(days)}')
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
) -> list[dict]:
    """Top keywords for ad-driven calls, sourced from
    ``ad_clicks_v2.click_view_keyword_info_text`` via the gclid join.

    NULL, empty, and the string ``'nan'`` all collapse into ``'(no keyword)'``.

    Returns ``[{"keyword": str, "calls": int}, ...]`` sorted by call count
    desc. Empty when either campaign list is empty.
    """
    if not invoca_campaign_ids or not ga_campaign_ids:
        return []
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
          AND t.timestamp >= TIMESTAMP('{_window_start_ts(days)}')
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


def _spam_scope_clause(invoca_campaign_ids: list[str], days: int, t_alias: str = "t") -> str:
    """Common WHERE clause — scopes a transactions row to the clinic's Invoca
    campaigns within the window. Injected verbatim into the SQL strings below;
    callers must guarantee ``invoca_campaign_ids`` is non-empty.
    """
    in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_campaign_ids) + ")"
    return (
        f"CAST({t_alias}.invoca_campaign_id AS STRING) IN {in_iv} "
        f"AND {t_alias}.timestamp >= TIMESTAMP('{_window_start_ts(days)}')"
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
) -> dict[str, Any]:
    """Total + spam-classified call counts for the window.

    Returns ``{"total_calls", "spam_calls", "spam_pct"}``. Empty Invoca list →
    all zeros.
    """
    if not invoca_campaign_ids:
        return {"total_calls": 0, "spam_calls": 0, "spam_pct": 0.0}
    scope = _spam_scope_clause(invoca_campaign_ids, days)
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
    scope = _spam_scope_clause(invoca_campaign_ids, days)
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
) -> list[dict]:
    """Calls bucketed by ``utm_medium`` (spam filtered).

    Returns ``[{"medium": str, "calls": int, "pct": float}, ...]`` sorted by
    call count desc. ``pct`` is share of all non-spam calls in the window.
    """
    if not invoca_campaign_ids:
        return []
    scope = _spam_scope_clause(invoca_campaign_ids, days)
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
) -> list[dict]:
    """Non-spam, scored calls bucketed by outcome.

    Returns ``[{"outcome": str, "calls": int}, ...]`` in the canonical order
    above. Buckets with zero calls are omitted from the result.
    """
    if not invoca_campaign_ids:
        return []
    scope = _spam_scope_clause(invoca_campaign_ids, days)
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
) -> list[dict]:
    """Crosstab of UTM medium × outcome for the Sankey's first link set.

    Each row carries the call count flowing from one medium bucket into one
    outcome bucket. Spam excluded; only calls with a callscoring row count.
    """
    if not invoca_campaign_ids:
        return []
    scope = _spam_scope_clause(invoca_campaign_ids, days)
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
) -> list[dict]:
    """Booked calls bucketed by patient type.

    Returns ``[{"patient_type": str, "calls": int}, ...]``. Buckets:
    ``Existing``, ``New``, ``Not Found``.
    """
    if not invoca_campaign_ids:
        return []
    scope = _spam_scope_clause(invoca_campaign_ids, days)
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
) -> list[dict]:
    if not invoca_campaign_ids:
        return []
    scope = _spam_scope_clause(invoca_campaign_ids, days)
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
) -> int:
    """COUNT(*) of calls in a given Stage-2 outcome bucket.

    Companion to :func:`_stage2_outcome_detail` — used to surface the cohort
    total when the detail query is LIMITed for an inline preview.
    """
    if not invoca_campaign_ids:
        return 0
    scope = _spam_scope_clause(invoca_campaign_ids, days)
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
    scope = _spam_scope_clause(invoca_campaign_ids, days)
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
) -> list[dict]:
    """Per-call rows where the funnel ends at "No Conversation"."""
    return _stage2_outcome_detail(
        clinic_id, invoca_campaign_ids, days, "No Conversation", limit=limit
    )


def no_conversation_count(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
) -> int:
    """Total count of "No Conversation" calls — for cohort banners."""
    return _stage2_outcome_count(
        clinic_id, invoca_campaign_ids, days, "No Conversation"
    )


def qualified_lead_no_conv_detail(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
    limit: int | None = None,
) -> list[dict]:
    """Per-call rows where the caller was a qualified lead but didn't book."""
    return _stage2_outcome_detail(
        clinic_id, invoca_campaign_ids, days, "Qualified Lead - No Conversion", limit=limit
    )


def qualified_lead_no_conv_count(
    clinic_id: str,
    invoca_campaign_ids: list[str],
    days: int = 90,
) -> int:
    """Total count of "Qualified Lead - No Conversion" calls."""
    return _stage2_outcome_count(
        clinic_id, invoca_campaign_ids, days, "Qualified Lead - No Conversion"
    )
