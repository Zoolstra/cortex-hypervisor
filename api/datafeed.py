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
  ``/google-ads/clicks``      click-level rows from ``ClinicData.ad_clicks_v2``

Invoca (scoped by ``instances.invoca_profile_id``):
  ``/invoca/transactions``    call-level metadata from ``ClinicData.transactions``.
                              The table is EVENT-grained (a call can emit several
                              rows); this endpoint dedupes to one row per
                              ``complete_call_id`` (latest event wins) so the
                              contract is one-row-per-call.

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

# Window / paging bounds — hard caps so one request can't scan years of data
# or pull an unbounded row count.
_MAX_WINDOW_DAYS = 366
_DEFAULT_WINDOW_DAYS = 30
_MAX_LIMIT = 10_000


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
    return _envelope(instance, w, rows, limit=limit, offset=offset)


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
    """
    profile = _invoca_profile(instance)
    if profile is None:
        return _envelope(instance, w, [], limit=limit, offset=offset)
    rows = _query(
        f"""
        SELECT * EXCEPT (
          -- tenant metadata (redundant with the path scoping)
          instance_name, invoca_profile_id,
          -- withheld from the client feed (2026-07-24): caller-history,
          -- cross-site tracking IDs, and internal QA fields. gclid stays as a
          -- pseudonymous join key to /google-ads/clicks; destination number
          -- and coarse geo (city/region) stay as agreed.
          repeat_calling_phone_number,
          _fbc, _fbp, fbclid,
          g_cid, ga_session_id, google_analytics_id, ga_measurement_id,
          wbraid, gbraid, msclkid,
          customer_id, reviewed_by, evaluated_by,
          -- withheld 2026-07-24 (second pass): all Invoca AI judgments —
          -- industry/conversion/scorecard/handling flags, AI_* flags, and
          -- sentiment — plus the sparse signal-event metadata, and
          -- has_transcript (dead column, hardcoded False at ingest).
          Appointment_Discussed__Industry_, Conversion_Likely__Industry_,
          Buying_Intent__Industry_, Existing_Appointment__Industry_,
          Existing_Customer__Industry_, Customer_Experience_Issue__Industry_,
          Appointment_Booked__Conversion_, Credit_Card_Payment__Conversion_,
          Service_Appointment_Booked__Conversion_,
          Proper_Greeting__Scorecard_, Positive_Wrap_Up__Scorecard_,
          Assume_Appointment__Scorecard_,
          Answered_by_Agent, Answered_by_Voicemail, Voicemail_Left, Short_Call,
          Opportunity, Non_Converting_Opportunity, Call_to_Review, Excellent_Call,
          AI_Appointment_Booked, AI_New_Customer, AI_Opportunity,
          call_sentiment_overall, call_sentiment_overall_label,
          signal_name, signal_occurred_at, signal_source, signal_partner_unique_id,
          revenue,
          has_transcript
        )
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
