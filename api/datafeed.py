"""
Client data feed — read-only REST access to an instance's own advertising and
call data, for external consumption (first consumer: Virsono Hearing Centres).

Endpoints (all GET, all under ``/datafeed/v1/{instance_id}``):

Google Ads (scoped by ``instances.google_ads_customer_id``):
  ``/google-ads/campaigns``   daily per-campaign rollup from ``ClinicData.ad_groups``
                              (spend from ``metrics_cost_micros``, Google's true
                              billed cost), campaign names joined from
                              ``ad_clicks_v2`` (``ad_groups`` doesn't carry them)
  ``/google-ads/ad-groups``   daily ad-group rows (typed: cost in dollars, ints)
  ``/google-ads/clicks``      click-level rows from ``ClinicData.ad_clicks_v2``.
                              Envelope carries ``settled_through`` (today −
                              _CLICKS_SETTLE_DAYS): rows on/before it are final,
                              younger rows may be restated by the ETL's settle
                              window — the anchor for clients' additive pulls
                              (request from previous settled_through, upsert on
                              gclid).

Invoca (scoped by ``instances.invoca_profile_id``):
  ``/invoca/transactions``    call-level metadata from ``ClinicData.transactions``.
                              The table is EVENT-grained (a call can emit several
                              rows); this endpoint dedupes to one row per
                              ``complete_call_id`` (latest event wins) so the
                              contract is one-row-per-call.
  ``/invoca/callscoring``     the settled, compute-on layer: per-call
                              classification (Claude transcript scoring), manual
                              relabels applied (``call_outcome_overrides``), and
                              PMS-reconciled ``booked_verified`` (same 10-day
                              phone-match rule as the intelligence funnel /
                              Virsono scorecard, so the numbers reconcile).

Reference:
  ``/dictionary``             machine-readable data dictionary: schema version,
                              layer guidance (settled vs operational mirror),
                              field definitions, and stated assumptions.

Transcript endpoints existed briefly (batch + per-call, served from
``gs://transcripts-json``) but were removed 2026-07-24 at Will's request
before client handoff — the feed is metadata-only.

Auth
----
Per-instance API key in the ``X-API-Key`` header, checked against the Secret
Manager secret ``datafeed-api-key-{instance_id}``. No secret provisioned means
the instance has no feed — 403 either way, so probing can't distinguish
"wrong key" from "no feed". Every query is additionally WHERE-scoped to the
instance's own profile IDs, so a valid key can only ever see its own tenant.

Enabling a new client = create the secret:
  gcloud secrets create datafeed-api-key-<instance_id> --replication-policy automatic
  python -c "import secrets; print(secrets.token_urlsafe(32))" | \
    gcloud secrets versions add datafeed-api-key-<instance_id> --data-file=-

Caller phone number (BAA gate)
------------------------------
``calling_phone_number`` is withheld from ``/invoca/transactions`` by default.
It ships only when the per-instance flag secret
``datafeed-caller-number-enabled-{instance_id}`` exists with a truthy value —
to be created ONLY once the receiving system is designated in writing under
the BAA. Enabling takes effect on the next request; disabling requires a
restart (secrets are cached at first use, same as key rotation).
"""
from __future__ import annotations

import decimal
import hmac
import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from google.api_core import exceptions as gcp_exc
from google.cloud import bigquery
from sqlalchemy.orm import Session

from api.core.db import get_session
from api.core.orm import Instance
from api.core.secrets import get_secret
from api.deps import PROJECT, bq_client

log = logging.getLogger(__name__)

router = APIRouter(prefix="/datafeed/v1/{instance_id}")

_CLINIC_DATA = f"{PROJECT}.ClinicData"
_PMS_UNIFIED = f"{PROJECT}.PMS_Unified"

# Version of the feed's response schemas, surfaced via /dictionary. Bump on
# any field addition/removal/redefinition so clients can pin against drift.
SCHEMA_VERSION = "1.4"

# How many trailing days of ad_clicks_v2 the ETL restates on each run
# (DELETE+APPEND settle window — mirrors big-query-ingestion
# build_schemas.CLICKS_SETTLE_DAYS). Rows dated on/before
# today − _CLICKS_SETTLE_DAYS are final; anything younger may still change.
# Surfaced as ``settled_through`` on the /google-ads/clicks envelope so
# clients can pull additively: request from their previous settled_through,
# upsert on gclid, store the new settled_through.
_CLICKS_SETTLE_DAYS = 7

# Window / paging bounds — hard caps so one request can't scan years of data
# or pull an unbounded row count.
_MAX_WINDOW_DAYS = 366
_DEFAULT_WINDOW_DAYS = 30
_MAX_LIMIT = 10_000

# PMS reconciliation window for booked_verified: an appointment CREATED within
# this many days on/after the call credits the call. Also why the dictionary
# says outcomes mature over ~10 days.
#
# CLIENT-VISIBLE. Must match CALL_BOOKING_MATCH_DAYS in
# intelligence_report/queries.py, and it is echoed to clients as
# `booked_match_days` on /dictionary. Widening it raises historical
# `booked_verified` counts for anyone pulling additively, so SCHEMA_VERSION is
# bumped alongside it.
_MATCH_DAYS = 10

# transactions columns withheld from the client feed. gclid stays as a
# pseudonymous join key to /google-ads/clicks; destination number and coarse
# geo (city/region) stay as agreed.
_TX_WITHHELD = (
    # tenant metadata (redundant with the path scoping)
    "instance_name", "invoca_profile_id",
    # caller-history, cross-site tracking IDs, internal QA (2026-07-24)
    "repeat_calling_phone_number",
    "_fbc", "_fbp", "fbclid",
    "g_cid", "ga_session_id", "google_analytics_id", "ga_measurement_id",
    "wbraid", "gbraid", "msclkid",
    "customer_id", "reviewed_by", "evaluated_by",
    # all Invoca AI judgments + sparse signal-event metadata + dead column
    # (2026-07-24, second pass)
    "Appointment_Discussed__Industry_", "Conversion_Likely__Industry_",
    "Buying_Intent__Industry_", "Existing_Appointment__Industry_",
    "Existing_Customer__Industry_", "Customer_Experience_Issue__Industry_",
    "Appointment_Booked__Conversion_", "Credit_Card_Payment__Conversion_",
    "Service_Appointment_Booked__Conversion_",
    "Proper_Greeting__Scorecard_", "Positive_Wrap_Up__Scorecard_",
    "Assume_Appointment__Scorecard_",
    "Answered_by_Agent", "Answered_by_Voicemail", "Voicemail_Left", "Short_Call",
    "Opportunity", "Non_Converting_Opportunity", "Call_to_Review", "Excellent_Call",
    "AI_Appointment_Booked", "AI_New_Customer", "AI_Opportunity",
    "call_sentiment_overall", "call_sentiment_overall_label",
    "signal_name", "signal_occurred_at", "signal_source", "signal_partner_unique_id",
    "revenue",
    "has_transcript",
)


# ── Auth ──────────────────────────────────────────────────────────────────────

def feed_instance(
    instance_id: str,
    x_api_key: str = Header(None, alias="X-API-Key"),
    db: Session = Depends(get_session),
) -> Instance:
    """Resolve the instance iff the caller presents its data-feed API key.

    The key lives in Secret Manager as ``datafeed-api-key-{instance_id}`` —
    absence of the secret means the feed isn't enabled for that instance.
    All failure modes return the same 403 so the endpoint doesn't leak which
    instance IDs exist or have feeds.
    """
    try:
        expected = (get_secret(f"datafeed-api-key-{instance_id}") or "").strip()
    except gcp_exc.NotFound:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    if not expected or not hmac.compare_digest((x_api_key or "").strip(), expected):
        raise HTTPException(status_code=403, detail="Invalid or missing API key")

    instance = db.get(Instance, instance_id)
    if instance is None:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    return instance


# ── Shared helpers ────────────────────────────────────────────────────────────

def window(
    start_date: date | None = Query(None, description="inclusive, YYYY-MM-DD; default end_date − 29d"),
    end_date: date | None = Query(None, description="inclusive, YYYY-MM-DD; default today"),
) -> tuple[date, date]:
    end = end_date or date.today()
    start = start_date or end - timedelta(days=_DEFAULT_WINDOW_DAYS - 1)
    if start > end:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date")
    if (end - start).days > _MAX_WINDOW_DAYS:
        raise HTTPException(status_code=422, detail=f"window may span at most {_MAX_WINDOW_DAYS} days")
    return start, end


def _query(sql: str, params: list[bigquery.ScalarQueryParameter]) -> list[dict]:
    """Run a parameterized query and return JSON-safe row dicts."""
    job = bq_client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    rows = []
    for r in job.result():
        d = {}
        for k, v in dict(r).items():
            if isinstance(v, (datetime, date)):
                v = v.isoformat()
            elif isinstance(v, decimal.Decimal):
                v = float(v)
            d[k] = v
        rows.append(d)
    return rows


def _envelope(instance: Instance, w: tuple[date, date], rows: list[dict], **extra) -> dict:
    return {
        "instance_id": instance.instance_id,
        "start_date": w[0].isoformat(),
        "end_date": w[1].isoformat(),
        "count": len(rows),
        **extra,
        "rows": rows,
    }


def _date_params(w: tuple[date, date]) -> list[bigquery.ScalarQueryParameter]:
    # segments_date is a STRING column in ISO form, so lexicographic BETWEEN on
    # the ISO strings is a correct date comparison.
    return [
        bigquery.ScalarQueryParameter("start", "STRING", w[0].isoformat()),
        bigquery.ScalarQueryParameter("end", "STRING", w[1].isoformat()),
    ]


def _ads_profile(instance: Instance) -> str | None:
    return (instance.google_ads_customer_id or "").strip() or None


def _invoca_profile(instance: Instance) -> int | None:
    raw = (instance.invoca_profile_id or "").strip()
    return int(raw) if raw.isdigit() else None


def _caller_number_enabled(instance_id: str) -> bool:
    """BAA gate for ``calling_phone_number`` in /invoca/transactions.

    OFF unless the per-instance flag secret exists with a truthy value. Create
    it only once the receiving system is designated in writing under the BAA.
    Enabling takes effect on the next request (a missing secret is never
    cached); disabling requires a restart, same as key rotation.
    """
    try:
        value = get_secret(f"datafeed-caller-number-enabled-{instance_id}")
    except gcp_exc.NotFound:
        return False
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


# ── Google Ads ────────────────────────────────────────────────────────────────

@router.get("/google-ads/campaigns")
def google_ads_campaigns(
    instance: Instance = Depends(feed_instance),
    w: tuple[date, date] = Depends(window),
):
    """Daily per-campaign performance, aggregated from the ad-group rows.

    ``ad_groups`` doesn't carry campaign names, so they're joined (best-effort)
    from ``ad_clicks_v2``; campaigns that never produced a tracked click report
    ``campaign_name: null``.
    """
    profile = _ads_profile(instance)
    if profile is None:
        return _envelope(instance, w, [])
    rows = _query(
        f"""
        WITH names AS (
            SELECT google_ads_campaign_id, ANY_VALUE(campaign_name) AS campaign_name
            FROM `{_CLINIC_DATA}.ad_clicks_v2`
            WHERE google_ads_profile_id = @profile
            GROUP BY google_ads_campaign_id
        )
        SELECT
          g.segments_date                                            AS date,
          g.google_ads_campaign_id                                   AS campaign_id,
          n.campaign_name,
          SUM(SAFE_CAST(g.metrics_clicks AS INT64))                  AS clicks,
          SUM(SAFE_CAST(g.metrics_interactions AS INT64))            AS interactions,
          SUM(SAFE_CAST(g.metrics_cost_micros AS FLOAT64)) / 1e6     AS cost,
          SUM(g.metrics_conversions)                                 AS conversions,
          SUM(g.metrics_conversions_value)                           AS conversions_value
        FROM `{_CLINIC_DATA}.ad_groups` g
        LEFT JOIN names n USING (google_ads_campaign_id)
        WHERE g.google_ads_profile_id = @profile
          AND g.segments_date BETWEEN @start AND @end
        GROUP BY date, campaign_id, campaign_name
        ORDER BY date, campaign_id
        """,
        [bigquery.ScalarQueryParameter("profile", "STRING", profile), *_date_params(w)],
    )
    log.info("datafeed %s google-ads/campaigns %s..%s -> %d rows",
             instance.instance_id, w[0], w[1], len(rows))
    return _envelope(instance, w, rows)


@router.get("/google-ads/ad-groups")
def google_ads_ad_groups(
    instance: Instance = Depends(feed_instance),
    w: tuple[date, date] = Depends(window),
):
    """Daily ad-group rows (one per ad group × date × device). Monetary values
    are converted from micros to account-currency units."""
    profile = _ads_profile(instance)
    if profile is None:
        return _envelope(instance, w, [])
    rows = _query(
        f"""
        SELECT
          segments_date                                              AS date,
          segments_device                                            AS device,
          google_ads_campaign_id                                     AS campaign_id,
          ad_group_id,
          ad_group_name,
          SAFE_CAST(metrics_clicks AS INT64)                         AS clicks,
          SAFE_CAST(metrics_interactions AS INT64)                   AS interactions,
          metrics_interaction_rate                                   AS interaction_rate,
          SAFE_CAST(metrics_cost_micros AS FLOAT64) / 1e6            AS cost,
          metrics_average_cpc / 1e6                                  AS average_cpc,
          metrics_average_cpm / 1e6                                  AS average_cpm,
          metrics_conversions                                        AS conversions,
          metrics_conversions_value                                  AS conversions_value,
          metrics_search_impression_share                            AS search_impression_share,
          metrics_search_top_impression_share                        AS search_top_impression_share,
          metrics_search_absolute_top_impression_share               AS search_absolute_top_impression_share,
          metrics_search_budget_lost_top_impression_share            AS search_budget_lost_top_impression_share,
          metrics_search_rank_lost_impression_share                  AS search_rank_lost_impression_share
        FROM `{_CLINIC_DATA}.ad_groups`
        WHERE google_ads_profile_id = @profile
          AND segments_date BETWEEN @start AND @end
        ORDER BY date, campaign_id, ad_group_id, device
        """,
        [bigquery.ScalarQueryParameter("profile", "STRING", profile), *_date_params(w)],
    )
    log.info("datafeed %s google-ads/ad-groups %s..%s -> %d rows",
             instance.instance_id, w[0], w[1], len(rows))
    return _envelope(instance, w, rows)


@router.get("/google-ads/clicks")
def google_ads_clicks(
    instance: Instance = Depends(feed_instance),
    w: tuple[date, date] = Depends(window),
    limit: int = Query(1000, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    """Click-level rows (one per tracked click; GCLID-keyed)."""
    profile = _ads_profile(instance)
    if profile is None:
        return _envelope(instance, w, [], limit=limit, offset=offset)
    rows = _query(
        f"""
        SELECT
          segments_date                          AS date,
          click_view_gclid                       AS gclid,
          google_ads_campaign_id                 AS campaign_id,
          campaign_name,
          click_view_ad_group_ad                 AS ad_group_ad,
          click_view_keyword                     AS keyword,
          click_view_keyword_info_text           AS keyword_text,
          click_view_keyword_info_match_type     AS keyword_match_type,
          segments_device                        AS device,
          segments_click_type                    AS click_type,
          click_view_area_of_interest_city       AS area_of_interest_city,
          click_view_area_of_interest_region     AS area_of_interest_region,
          click_view_area_of_interest_country    AS area_of_interest_country
        FROM `{_CLINIC_DATA}.ad_clicks_v2`
        WHERE google_ads_profile_id = @profile
          AND segments_date BETWEEN @start AND @end
        ORDER BY date, gclid
        LIMIT @limit OFFSET @offset
        """,
        [
            bigquery.ScalarQueryParameter("profile", "STRING", profile),
            *_date_params(w),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
            bigquery.ScalarQueryParameter("offset", "INT64", offset),
        ],
    )
    log.info("datafeed %s google-ads/clicks %s..%s offset=%d -> %d rows",
             instance.instance_id, w[0], w[1], offset, len(rows))
    settled_through = (date.today() - timedelta(days=_CLICKS_SETTLE_DAYS)).isoformat()
    return _envelope(instance, w, rows, limit=limit, offset=offset,
                     settled_through=settled_through)


# ── Invoca ────────────────────────────────────────────────────────────────────

@router.get("/invoca/transactions")
def invoca_transactions(
    instance: Instance = Depends(feed_instance),
    w: tuple[date, date] = Depends(window),
    limit: int = Query(1000, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    """Call-level Invoca metadata, one row per call.

    The underlying table is event-grained; rows are deduped on
    ``complete_call_id`` keeping the latest event, so late signal updates
    (revenue, scorecards) are reflected rather than duplicated. Rows without a
    ``complete_call_id`` fall back to ``transaction_id`` as their identity.

    ``calling_phone_number`` ships only when the instance's BAA flag is set
    (see ``_caller_number_enabled``) — withheld by default.
    """
    profile = _invoca_profile(instance)
    if profile is None:
        return _envelope(instance, w, [], limit=limit, offset=offset)
    withheld = list(_TX_WITHHELD)
    if not _caller_number_enabled(instance.instance_id):
        withheld.append("calling_phone_number")
    rows = _query(
        f"""
        SELECT * EXCEPT ({", ".join(withheld)})
        FROM `{_CLINIC_DATA}.transactions`
        WHERE invoca_profile_id = @profile
          AND DATE(timestamp) BETWEEN @start AND @end
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY IFNULL(complete_call_id, transaction_id)
          ORDER BY timestamp DESC, transaction_id
        ) = 1
        ORDER BY timestamp, transaction_id
        LIMIT @limit OFFSET @offset
        """,
        [
            bigquery.ScalarQueryParameter("profile", "INT64", profile),
            bigquery.ScalarQueryParameter("start", "DATE", w[0].isoformat()),
            bigquery.ScalarQueryParameter("end", "DATE", w[1].isoformat()),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
            bigquery.ScalarQueryParameter("offset", "INT64", offset),
        ],
    )
    log.info("datafeed %s invoca/transactions %s..%s offset=%d -> %d rows",
             instance.instance_id, w[0], w[1], offset, len(rows))
    return _envelope(instance, w, rows, limit=limit, offset=offset)


@router.get("/invoca/callscoring")
def invoca_callscoring(
    instance: Instance = Depends(feed_instance),
    w: tuple[date, date] = Depends(window),
    limit: int = Query(1000, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    """The settled, compute-on call-outcome layer — one row per scored call.

    Sources three internal layers and reconciles them the same way the
    intelligence funnel does, so numbers derived here match ours:

    - ``classification``: the transcript-scoring label
      (``ClinicData.callscoring``, Claude-scored), bucketed with the funnel's
      canonical precedence.
    - ``verified_outcome``: ``classification`` with manual relabels applied
      (``ClinicData.call_outcome_overrides``; latest row per call wins). A
      human relabel is authoritative.
    - ``booked_verified``: PMS-reconciled booking — a distinct PMS appointment
      CREATED within ``_MATCH_DAYS`` days on/after the call, phone-matched to
      the caller, credited to the most recent eligible call per appointment
      (identical rule to ``intelligence_report.queries._call_tagging_cte`` /
      ``google_ads_roi``). Requires a genuine conversation post-override —
      spam / wrong-number / no-conversation / no-transcript calls can never
      book, so a relabel away from genuine explicitly un-credits the booking.
      This is why outcomes mature over ~``_MATCH_DAYS`` days: recent calls'
      bookings may not have landed in the PMS feed yet.

    Only calls with a scoring row or a manual label are returned — this layer
    deliberately covers less than /invoca/transactions (unscored calls have no
    settled outcome yet). ``scored_as_of`` is when the row's labels were last
    computed (scoring time, or relabel time if later).
    """
    profile = _invoca_profile(instance)
    clinic_ids = [c.clinic_id for c in instance.clinics if c.deleted_at is None]
    if profile is None or not clinic_ids:
        return _envelope(instance, w, [], limit=limit, offset=offset)
    rows = _query(
        f"""
        WITH calls AS (
          -- One row per call in the window (event-grained table → latest event),
          -- with the market (Invoca campaign name), normalized caller phone for
          -- PMS matching, and the call's CLINIC-LOCAL date. PMS timestamps are
          -- clinic-local naive strings, so call↔appointment day math uses the
          -- local date on both sides (UTC would skew evening calls a day late).
          SELECT
            complete_call_id,
            timestamp AS call_ts,
            COALESCE(SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(start_time_local, 1, 10)),
                     DATE(timestamp))                     AS call_date_local,
            advertiser_campaign_name                      AS market,
            RIGHT(REGEXP_REPLACE(IFNULL(calling_phone_number, ''), r'\\D', ''), 10) AS phone_norm
          FROM `{_CLINIC_DATA}.transactions`
          WHERE invoca_profile_id = @profile
            AND DATE(timestamp) BETWEEN @start AND @end
            AND complete_call_id IS NOT NULL
          QUALIFY ROW_NUMBER() OVER (
            PARTITION BY complete_call_id ORDER BY timestamp DESC) = 1
        ),
        cs AS (
          SELECT * FROM `{_CLINIC_DATA}.callscoring`
          WHERE clinic_id IN UNNEST(@clinic_ids)
          QUALIFY ROW_NUMBER() OVER (
            PARTITION BY complete_call_id ORDER BY scored_at DESC) = 1
        ),
        ov AS (
          -- Latest manual relabel per call; a NULL outcome row is an explicit
          -- "cleared" marker → fall back to the model label.
          SELECT complete_call_id, outcome, set_at
          FROM `{_CLINIC_DATA}.call_outcome_overrides`
          WHERE clinic_id IN UNNEST(@clinic_ids)
          QUALIFY ROW_NUMBER() OVER (
            PARTITION BY complete_call_id ORDER BY set_at DESC) = 1
        ),
        scored AS (
          -- Scoring flags with relabels applied, exactly as _call_tagging_cte
          -- does it: an override REPLACES the model's outcome flags.
          SELECT
            c.complete_call_id, c.call_ts, c.call_date_local, c.market, c.phone_norm,
            cs.reasoning, cs.scored_at,
            ov.outcome AS override_outcome, ov.set_at AS override_set_at,
            CASE
              WHEN cs.complete_call_id IS NULL                    THEN NULL
              WHEN IFNULL(cs.empty_transcript, FALSE)             THEN 'no_transcript'
              WHEN IFNULL(cs.spam_or_solicitor, FALSE)            THEN 'spam'
              WHEN IFNULL(cs.wrong_number, FALSE)                 THEN 'wrong_number'
              WHEN IFNULL(cs.appointment_booked, FALSE)           THEN 'appointment_booked'
              WHEN IFNULL(cs.no_conversation, FALSE)              THEN 'no_conversation'
              WHEN IFNULL(cs.qualified_lead_no_conversion, FALSE) THEN 'qualified_no_conversion'
              WHEN IFNULL(cs.existing_customer, FALSE)            THEN 'existing_patient'
              ELSE 'other'
            END AS classification,
            -- Post-override eligibility for booked reconciliation: a genuine,
            -- connected conversation. Overrides rewrite the flags, so a call
            -- relabelled to spam/wrong/no_conversation loses eligibility (and
            -- with it any booked credit); a relabel to a genuine label
            -- substitutes for a missing/empty transcript.
            IF(ov.outcome IS NOT NULL,
               ov.outcome NOT IN ('spam', 'wrong_number', 'no_conversation'),
               cs.complete_call_id IS NOT NULL
                 AND NOT IFNULL(cs.empty_transcript, FALSE)
                 AND NOT IFNULL(cs.spam_or_solicitor, FALSE)
                 AND NOT IFNULL(cs.wrong_number, FALSE)
                 AND NOT IFNULL(cs.no_conversation, FALSE)) AS genuine
          FROM calls c
          LEFT JOIN cs ON cs.complete_call_id = c.complete_call_id
          LEFT JOIN ov ON ov.complete_call_id = c.complete_call_id
          WHERE cs.complete_call_id IS NOT NULL OR ov.outcome IS NOT NULL
        ),
        patients AS (
          SELECT DISTINCT _clinic_id, client_id, phone_norm
          FROM `{_PMS_UNIFIED}.patient_contacts`
          WHERE _clinic_id IN UNNEST(@clinic_ids) AND LENGTH(phone_norm) = 10
        ),
        booked_calls AS (
          -- One credited call per PMS appointment: each appointment created
          -- within @match_days on/after a call goes to the MOST RECENT genuine
          -- call that could have produced it (local-day vs local-day compare).
          SELECT complete_call_id FROM (
            SELECT s.complete_call_id,
                   ROW_NUMBER() OVER (
                     PARTITION BY a._clinic_id, a.event_id
                     ORDER BY s.call_ts DESC
                   ) AS rn
            FROM scored s
            JOIN patients p
              ON p.phone_norm = s.phone_norm AND LENGTH(s.phone_norm) = 10
            JOIN `{_PMS_UNIFIED}.Appointments` a
              ON a._clinic_id = p._clinic_id AND a.client_id = p.client_id
            WHERE DATE_DIFF(DATE(SAFE_CAST(a.created_time AS TIMESTAMP)),
                            s.call_date_local, DAY) BETWEEN 0 AND @match_days
              AND s.genuine
          )
          WHERE rn = 1
        )
        SELECT
          s.complete_call_id,
          s.call_ts                                          AS call_started_at,
          s.market,
          s.classification,
          COALESCE(s.override_outcome, s.classification)     AS verified_outcome,
          (b.complete_call_id IS NOT NULL)                   AS booked_verified,
          s.reasoning,
          COALESCE(GREATEST(s.scored_at, s.override_set_at),
                   s.scored_at, s.override_set_at)           AS scored_as_of
        FROM scored s
        LEFT JOIN booked_calls b ON b.complete_call_id = s.complete_call_id
        ORDER BY s.call_ts, s.complete_call_id
        LIMIT @limit OFFSET @offset
        """,
        [
            bigquery.ScalarQueryParameter("profile", "INT64", profile),
            bigquery.ArrayQueryParameter("clinic_ids", "STRING", clinic_ids),
            bigquery.ScalarQueryParameter("start", "DATE", w[0].isoformat()),
            bigquery.ScalarQueryParameter("end", "DATE", w[1].isoformat()),
            bigquery.ScalarQueryParameter("match_days", "INT64", _MATCH_DAYS),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
            bigquery.ScalarQueryParameter("offset", "INT64", offset),
        ],
    )
    log.info("datafeed %s invoca/callscoring %s..%s offset=%d -> %d rows",
             instance.instance_id, w[0], w[1], offset, len(rows))
    return _envelope(instance, w, rows, limit=limit, offset=offset)


# ── Dictionary ────────────────────────────────────────────────────────────────

_LAYER_GUIDANCE = {
    "settled": {
        "endpoints": ["/invoca/callscoring"],
        "use_for": "conclusions, reporting, reconciliation — this is the layer to compute on",
        "notes": (
            "Outcomes mature over about 10 days: bookings are PMS-reconciled "
            "(an appointment created within 10 days on/after the call), and "
            "manual relabels can land at any time. Re-pull the last few days "
            "on each sync."
        ),
    },
    "operational_mirror": {
        "endpoints": ["/google-ads/campaigns", "/google-ads/ad-groups",
                      "/google-ads/clicks", "/invoca/transactions"],
        "use_for": "recency and operational monitoring — fresh but provisional",
        "notes": (
            "Google restates recent data: cost/ad-group rows settle over ~3 "
            "days, click rows over ~7 (the /google-ads/clicks envelope carries "
            "settled_through — rows dated on/before it are final). To pull "
            "clicks additively: request start_date = your previous "
            "settled_through, upsert on gclid, store the new settled_through. "
            "Raw call rows carry no verified outcome. Never draw conclusions "
            "from this layer alone."
        ),
    },
}

_ASSUMPTIONS = [
    "Market-level attribution holds while each market runs exactly one "
    "campaign; if a market ever runs multiple concurrent campaigns, "
    "market-level joins must move to campaign IDs.",
    "One row per call everywhere: transactions and callscoring are both "
    "deduplicated on complete_call_id (latest event / latest score wins).",
]

_FIELD_DEFINITIONS = {
    "/invoca/callscoring": {
        "complete_call_id": "Stable call identifier; joins to /invoca/transactions.",
        "call_started_at": "Call start (UTC). The date window filters on this.",
        "market": "Invoca campaign name for the call — the market, under the one-campaign-per-market assumption.",
        "classification": (
            "Transcript-scored outcome label, canonical precedence: "
            "no_transcript, spam, wrong_number, appointment_booked, "
            "no_conversation, qualified_no_conversion, existing_patient, other. "
            "Null only when a manual label exists without a scoring row."
        ),
        "verified_outcome": (
            "classification with manual review applied — a human relabel "
            "replaces the model label and is authoritative. Compute on this, "
            "not classification."
        ),
        "booked_verified": (
            "True when a PMS appointment was created within ~10 days on/after "
            "the call and phone-matches the caller; each appointment credits "
            "only its most recent genuine call. This is the verified 'booked', "
            "NOT the transcript's appointment_booked language and NOT any "
            "Invoca flag. Matures over ~10 days."
        ),
        "reasoning": "Model's one-paragraph rationale for the classification.",
        "scored_as_of": "When this row's labels were last computed (scoring time, or relabel time if later).",
    },
    "/invoca/transactions": {
        "complete_call_id": "Stable call identifier — the deduplication key.",
        "timestamp": "Call timestamp (UTC); the date window filters on it.",
        "gclid": "Google Click ID — pseudonymous join key to /google-ads/clicks.",
        "calling_phone_number": (
            "Caller's phone number. Withheld by default; enabled per instance "
            "only after the receiving system is designated in writing under "
            "the BAA."
        ),
        "destination_phone_number": "Number the call was routed to (your location's line).",
    },
    "/google-ads/*": {
        "cost": "Billed spend in the account currency (converted from micros).",
        "segments/impression_share": "Fractions 0-1.",
        "settling": "Google restates recent data; treat the last 2-3 days as provisional.",
    },
}


@router.get("/dictionary")
def dictionary(instance: Instance = Depends(feed_instance)):
    """Machine-readable data dictionary for the feed.

    Serves the schema version, the two-layer guidance (settled vs operational
    mirror), field definitions, stated assumptions, and this instance's
    current caller-number flag state.
    """
    return {
        "instance_id": instance.instance_id,
        "schema_version": SCHEMA_VERSION,
        "layers": _LAYER_GUIDANCE,
        "assumptions": _ASSUMPTIONS,
        "field_definitions": _FIELD_DEFINITIONS,
        "caller_number_enabled": _caller_number_enabled(instance.instance_id),
        "booked_match_days": _MATCH_DAYS,
    }
