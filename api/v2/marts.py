"""
Serve-time queries over the Cloud SQL `marts` database (MySQL 8.4).

Ported from `marts/reconcile_funnel.py`, whose BigQuery version was verified to
reproduce v1's funnel exactly across 3 clinics and 5 windows. Dialect changes
only — BigQuery `COUNTIF(x)` becomes MySQL `SUM(x)`, `FORMAT_DATE` becomes
`DATE_FORMAT`, and booleans arrive as TINYINT(1) so they are already truthy.

No second SQLAlchemy engine: the hypervisor's existing `clients` connection is
reused and the mart tables are schema-qualified (`marts.call_facts`). The service
account holds SELECT on `marts.*`, so cross-database reads work and we avoid a
second connection pool on a 1-vCPU instance.

Design invariants carried from the contract:
  * Booking credit is WINDOW-RELATIVE and POST-OVERRIDE — ranked per appointment
    `event_id` within the queried window, over override-rewritten flags (§4).
  * The rank is joined as a SEMI-join, never a LEFT JOIN. One call can win credit
    for several appointments, so a join fans out and inflates every count. That
    bug corrupted our own impact report before being caught.
  * ONE shared tagging CTE feeds every consumer, for the same reason v1 has
    `_call_tagging_cte`: per-reader copies drift.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Data floor — must match MIN_WINDOW_DATE in intelligence_report/queries.py
# (contract §1). Monthly trends pin their start here regardless of the window.
MIN_WINDOW_DATE = dt.date(2025, 12, 4)

# Must match CALL_BOOKING_MATCH_DAYS in intelligence_report/queries.py — the
# mart and the v1 readers have to agree on what "booked" means or the parity
# harness diverges. Duplicated rather than imported for the same reason
# MIN_WINDOW_DATE above is: importing queries.py pulls the BigQuery client into
# this SQLAlchemy-only layer. tests/test_group_intelligence.py pins the two
# together so they cannot drift silently.
CALL_BOOKING_MATCH_DAYS = 10

# ── Shared tagging CTE ───────────────────────────────────────────────────────
# Exposes a `tagged` CTE of per-call flags; callers append their own SELECT.
# Bind params: clinic_id, start_ts, end_ts, match_days.
_TAGGED_CTE = """
WITH cf AS (
  SELECT * FROM marts.call_facts
  WHERE clinic_id = :clinic_id
    AND call_ts >= :start_ts AND call_ts < :end_ts
),
ov AS (
  SELECT complete_call_id, outcome
  FROM marts.call_outcome_overrides_current
  WHERE clinic_id = :clinic_id
),
c AS (
  SELECT
    cf.complete_call_id, cf.call_ts, cf.has_prior_appt, cf.last_prior_appt_ts,
    cf.call_date_local,
    IF(ov.outcome IS NOT NULL, ov.outcome = 'spam',                    cf.spam)        AS spam,
    IF(ov.outcome IS NOT NULL, ov.outcome = 'wrong_number',            cf.wrong)       AS wrong,
    (cf.has_cs OR ov.outcome IS NOT NULL)                                              AS has_cs,
    IF(ov.outcome IS NOT NULL, 0,                                      cf.empty_transcript) AS empty_transcript,
    IF(ov.outcome IS NOT NULL, ov.outcome = 'no_conversation',         cf.no_conv)     AS no_conv,
    cf.no_conv_type,
    IF(ov.outcome IS NOT NULL, ov.outcome = 'qualified_no_conversion', cf.qualified)   AS qualified,
    IF(ov.outcome IS NOT NULL, ov.outcome = 'existing_patient',        cf.tx_existing) AS tx_existing,
    cf.looking_to_book, cf.answered_by_matthew,
    ov.outcome AS override_outcome
  FROM cf LEFT JOIN ov ON ov.complete_call_id = cf.complete_call_id
),
booked AS (
  SELECT complete_call_id FROM (
    SELECT bc.complete_call_id,
           ROW_NUMBER() OVER (PARTITION BY bc.event_id ORDER BY bc.call_ts DESC) rn
    FROM marts.booking_candidates bc
    JOIN c ON c.complete_call_id = bc.complete_call_id
    WHERE bc.clinic_id = :clinic_id
      AND bc.created_minus_call_local_days BETWEEN 0 AND :match_days
      AND c.has_cs AND NOT c.empty_transcript
      AND NOT c.spam AND NOT c.wrong AND NOT c.no_conv
  ) r WHERE rn = 1
),
tagged AS (
  SELECT
    c.complete_call_id, c.call_ts,
    (c.has_cs AND NOT c.empty_transcript)                                AS has_content,
    (NOT (c.has_cs AND NOT c.empty_transcript))                          AS no_transcript,
    c.spam,
    (NOT c.spam AND c.wrong)                                             AS is_wrong,
    (c.has_cs AND NOT c.empty_transcript AND NOT c.spam AND NOT c.wrong) AS genuine,
    (c.complete_call_id IN (SELECT complete_call_id FROM booked)
     AND c.override_outcome IS NULL)                                     AS reconciled,
    (c.has_cs AND NOT c.empty_transcript AND NOT c.no_conv)              AS connected_raw,
    (c.no_conv AND c.no_conv_type = 'voicemail')                         AS is_voicemail,
    (c.no_conv AND (c.no_conv_type IS NULL OR c.no_conv_type <> 'voicemail')) AS is_hangup,
    IF(c.override_outcome IS NOT NULL,
       c.override_outcome = 'existing_patient',
       COALESCE(c.has_prior_appt, 0) OR c.tx_existing)                   AS existing,
    (c.last_prior_appt_ts IS NOT NULL
     AND DATEDIFF(c.call_date_local, DATE(c.last_prior_appt_ts)) <= 365) AS appt_active,
    (c.last_prior_appt_ts IS NOT NULL
     AND DATEDIFF(c.call_date_local, DATE(c.last_prior_appt_ts)) BETWEEN 366 AND 730) AS appt_lapsing,
    (c.last_prior_appt_ts IS NOT NULL
     AND DATEDIFF(c.call_date_local, DATE(c.last_prior_appt_ts)) > 730)  AS appt_deep_dormant,
    (c.last_prior_appt_ts IS NULL)                                       AS appt_none,
    c.qualified, c.looking_to_book, c.answered_by_matthew
  FROM c
)
"""


def _params(clinic_id: str, start: str, end_incl: str) -> dict[str, Any]:
    """Bind params from an INCLUSIVE end date, matching v1's Window contract."""
    end_excl = (dt.date.fromisoformat(end_incl) + dt.timedelta(days=1)).isoformat()
    return {"clinic_id": clinic_id,
            "start_ts": f"{start} 00:00:00",
            "end_ts": f"{end_excl} 00:00:00",
            "match_days": CALL_BOOKING_MATCH_DAYS}


# ── call_outcomes_funnel (contract §3) ───────────────────────────────────────
_FUNNEL_SQL = text(_TAGGED_CTE + """
SELECT
  COUNT(*)                                                        AS total,
  SUM(no_transcript)                                              AS no_transcript,
  SUM(spam)                                                       AS spam,
  SUM(is_wrong)                                                   AS wrong_number,
  SUM(genuine)                                                    AS genuine,
  SUM(genuine AND NOT connected_raw)                              AS missed,
  SUM(genuine AND NOT connected_raw AND is_voicemail)             AS voicemail,
  SUM(genuine AND NOT connected_raw AND is_hangup)                AS hangup,
  SUM(genuine AND connected_raw)                                  AS connected,
  SUM(genuine AND connected_raw AND NOT existing)                 AS connected_new,
  SUM(genuine AND connected_raw AND existing)                     AS connected_existing,
  SUM(genuine AND connected_raw AND reconciled)                   AS booked,
  SUM(genuine AND connected_raw AND reconciled AND NOT existing)  AS booked_new,
  SUM(genuine AND connected_raw AND reconciled AND existing)      AS booked_existing,
  -- Keys on looking_to_book, NOT the `qualified` flag: the funnel and the
  -- drill-down predicates deliberately use different flags (contract §7).
  SUM(genuine AND connected_raw AND NOT reconciled AND NOT existing
      AND looking_to_book)                                        AS qualified_not_booked,
  SUM(genuine AND connected_raw AND NOT reconciled AND NOT existing
      AND NOT looking_to_book)                                    AS other,
  SUM(genuine AND connected_raw AND existing AND appt_active)          AS existing_active,
  SUM(genuine AND connected_raw AND existing AND appt_lapsing)         AS existing_lapsing,
  SUM(genuine AND connected_raw AND existing AND appt_deep_dormant)    AS existing_deep_dormant,
  SUM(genuine AND connected_raw AND existing AND appt_none)            AS existing_dormant_never
FROM tagged
""")


def call_outcomes_funnel(db: Session, clinic_id: str,
                         start: str, end_incl: str) -> dict[str, Any]:
    """Mart-backed equivalent of queries.call_outcomes_funnel."""
    row = db.execute(_FUNNEL_SQL, _params(clinic_id, start, end_incl)).mappings().first()
    out = {k: int(v or 0) for k, v in (row or {}).items()}
    connected = out.get("connected", 0)
    # Derived, never stored: None when the denominator is zero, as v1 does.
    out["booked_rate"] = (out.get("booked", 0) / connected) if connected else None
    return out


# ── connected_outcomes_by_month (queries.py:4505) ────────────────────────────
# Three quirks reproduced deliberately:
#   1. The trend is PINNED to start at MIN_WINDOW_DATE regardless of the selected
#      window's start; only the end follows the range (contract §1).
#   2. `qualified` is checked BEFORE `existing` — the opposite of the funnel's
#      connected partition, so this chart does NOT sum to the funnel's
#      per-segment splits. That inconsistency is shipped behaviour (contract §16).
#   3. It uses the `qualified` flag, not `looking_to_book` (contract §7).
# NOTE: the month label is built with CONCAT/LPAD rather than DATE_FORMAT.
# A literal '%' inside text() is subject to paramstyle escaping with the pymysql
# driver, which silently corrupted the label — first collapsing every row into a
# single '%Y-%m' bucket, then mislabelling months entirely. CONCAT has no % and
# is driver-independent.
_MONTHLY_SQL = text(_TAGGED_CTE + """
SELECT
  CONCAT(YEAR(call_ts), '-', LPAD(MONTH(call_ts), 2, '0'))   AS month,
  SUM(reconciled)                                            AS booked,
  SUM(NOT reconciled AND qualified)                          AS qualified_not_booked,
  SUM(NOT reconciled AND NOT qualified AND existing)          AS existing_customer,
  SUM(NOT reconciled AND NOT qualified AND NOT existing)      AS other,
  COUNT(*)                                                   AS connected
FROM tagged
WHERE genuine AND connected_raw
GROUP BY month
ORDER BY month
""")


def mart_covers_full_history(db: Session, clinic_id: str) -> bool:
    """True when the last call_facts build SCANNED back to the data floor.

    Readers pinned to MIN_WINDOW_DATE cannot be served from a delta-built mart:
    `marts.build --rebuild-days 60` loads only a trailing window, so the series
    would come back silently truncated. Callers must fall back to v1 when this is
    False — a short series reads as a real decline, which is worse than slow.

    The signal is the BUILD SCOPE recorded in `mart_meta.source_watermarks`
    (`history_floor`), not `MIN(call_ts)`. The data extent cannot answer this
    question: a clinic onboarded in June has no December calls, so its complete
    mart is indistinguishable from a 60-day delta build — and the earlier
    extent-based check therefore pinned 9 of 13 clinics to v1 permanently, even
    straight after a full rebuild.

    Fails CLOSED (False) on any error or missing metadata, including marts built
    before `history_floor` was recorded. False only costs a v1 fallback: correct
    numbers, slower. True on a truncated mart would ship a fake decline.
    """
    try:
        wm = db.execute(text(
            "SELECT source_watermarks FROM marts.mart_meta "
            "WHERE mart_name = 'call_facts' AND clinic_id = :c "
            "ORDER BY built_at DESC LIMIT 1"
        ), {"c": clinic_id}).scalar()
        if not wm:
            return False
        if isinstance(wm, (str, bytes, bytearray)):
            wm = json.loads(wm)
        floor = (wm.get("history_floor") or {}).get("call_facts")
        if not floor:
            log.info("v2: no history_floor in mart_meta for clinic=%s "
                     "(pre-upgrade build) — deferring to v1", clinic_id)
            return False
        # Stored as an ISO timestamp; compare on the date part only.
        return dt.date.fromisoformat(str(floor)[:10]) <= MIN_WINDOW_DATE
    except Exception as exc:  # noqa: BLE001 — fail-safe per contract §18
        log.warning("v2 mart_covers_full_history failed clinic=%s: %s",
                    clinic_id, exc)
        return False


def connected_outcomes_by_month(db: Session, clinic_id: str,
                                end_incl: str) -> list[dict[str, Any]] | None:
    """Mart-backed queries.connected_outcomes_by_month.

    Takes only the window END: the start is pinned to MIN_WINDOW_DATE (quirk 1).

    Returns None when the mart lacks full history, signalling the caller to use
    v1 instead. Returns [] when the window ends before the floor, and on error —
    matching v1's fail-safe convention (contract §18).
    """
    if dt.date.fromisoformat(end_incl) < MIN_WINDOW_DATE:
        return []
    if not mart_covers_full_history(db, clinic_id):
        log.info("v2 monthly: call_facts lacks full history for clinic=%s "
                 "(delta build) — deferring to v1", clinic_id)
        return None
    try:
        rows = db.execute(
            _MONTHLY_SQL,
            _params(clinic_id, MIN_WINDOW_DATE.isoformat(), end_incl),
        ).mappings().all()
    except Exception as exc:  # noqa: BLE001 — fail-safe per contract §18
        log.warning("v2 connected_outcomes_by_month failed clinic=%s: %s",
                    clinic_id, exc)
        return []
    return [{
        "month": r["month"],
        "booked": int(r["booked"] or 0),
        "qualified_not_booked": int(r["qualified_not_booked"] or 0),
        "existing_customer": int(r["existing_customer"] or 0),
        "other": int(r["other"] or 0),
        "connected": int(r["connected"] or 0),
    } for r in rows]


# ── call_funnel_matthew_split (queries.py:4444) ──────────────────────────────
# Per-bucket share of connected calls answered by Matthew (the AI receptionist)
# rather than clinic staff. Uses the SELECTED window, so unlike the monthly
# series it needs no full-history mart.
#
# Two deliberate details carried over:
#   1. `qualified` is checked BEFORE `existing` here — same ordering as the
#      monthly chart and the OPPOSITE of call_outcomes_funnel's connected
#      partition (contract §16). It also uses the `qualified` flag, not
#      `looking_to_book` (§7).
#   2. Kept separate from the funnel on purpose: a clinic with no Matthew data
#      must not zero out the main funnel. Returns None so the caller omits the
#      section entirely.
#
# `call_facts.answered_by_matthew` is a LOGICAL_OR over matthew_calls, which is
# exactly right for this reader: v1 tests membership with `WHERE
# answered_by_matthew` (EXISTS-style), which LOGICAL_OR matches. See the KNOWN
# DEVIATIONS note in marts/sql.py.
_MATTHEW_SPLIT_SQL = text(_TAGGED_CTE + """
SELECT
  SUM(genuine AND connected_raw AND answered_by_matthew)                    AS connected,
  SUM(genuine AND connected_raw AND reconciled AND answered_by_matthew)     AS booked,
  SUM(genuine AND connected_raw AND NOT reconciled AND qualified
      AND answered_by_matthew)                                             AS qualified_not_booked,
  SUM(genuine AND connected_raw AND NOT reconciled AND NOT qualified
      AND existing AND answered_by_matthew)                                AS existing_customer,
  SUM(genuine AND connected_raw AND NOT reconciled AND NOT qualified
      AND NOT existing AND answered_by_matthew)                            AS other
FROM tagged
""")


def call_funnel_matthew_split(db: Session, clinic_id: str, start: str,
                              end_incl: str) -> dict[str, int] | None:
    """Mart-backed queries.call_funnel_matthew_split.

    Returns None when there is no Matthew signal (no Matthew-answered connected
    call), so the caller omits the split — matching v1, which treats an absent
    matthew_calls table as expected rather than an error.
    """
    try:
        row = db.execute(_MATTHEW_SPLIT_SQL,
                         _params(clinic_id, start, end_incl)).mappings().first()
    except Exception as exc:  # noqa: BLE001
        log.info("v2 matthew split skipped clinic=%s: %s", clinic_id, exc)
        return None
    if not row:
        return None
    split = {k: int(row[k] or 0) for k in
             ("connected", "booked", "qualified_not_booked",
              "existing_customer", "other")}
    # No Matthew-answered connected calls -> nothing to split.
    return split if split["connected"] > 0 else None


def mart_freshness(db: Session, clinic_id: str) -> dict[str, Any]:
    """Latest data_version / build time for this clinic, so the UI can show
    'data as of …' rather than implying the numbers are live."""
    row = db.execute(text("""
        SELECT data_version, MAX(built_at) AS built_at
        FROM marts.mart_meta
        WHERE clinic_id = :clinic_id AND mart_name = 'call_facts'
        GROUP BY data_version ORDER BY built_at DESC LIMIT 1
    """), {"clinic_id": clinic_id}).mappings().first()
    if not row:
        return {"data_version": None, "built_at": None}
    return {"data_version": row["data_version"],
            "built_at": row["built_at"].isoformat() if row["built_at"] else None}
