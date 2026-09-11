"""
GA4 website-traffic readers — the v1 BigQuery path behind
``GET /intelligence/{clinic_id}/website`` and ``/intelligence/group/{instance_id}/website``.

Reads the ``ClinicData.ga4_*`` tables the ETL job ``ga4-ingest`` lands
(``cortex-data-ingestion/app/ga4/``; plan: ``resources/google-analytics-integration-plan.md``).
Kept out of ``queries.py`` so that module stops growing; it borrows the client,
window and dataset helpers from there.

Three rules, all verified against Agency Analytics' own figures on 2026-09-10
(plan §2.3) and a follow-up parity run the same day:

* **Totals come from the dimension-free tables.** GA4 inflates a session sum
  with every dimension you add: for CHAA Aug-2026 the API returns 3,555
  sessions with no dimensions, 3,618 with the six traffic dimensions, 3,629 with
  ``hostName`` on top. Summing ``ga4_traffic_daily`` therefore over-reads.
  ``totals``, ``previous_totals`` and ``sessions_daily`` read
  ``ga4_sessions_daily`` (property × day, no dimensions) — the figure Agency
  Analytics shows. Users are worse still (a user seen on two days is two rows'
  worth), so for whole-calendar-month windows ``total_users`` / ``new_users``
  come from ``ga4_sessions_monthly`` (``users_basis = "monthly_unique"``);
  otherwise they are the daily sum (``users_basis = "daily_sum"``).
* **Breakdowns are NOT host-filtered and may sum past the totals.** ``by_channel``,
  ``by_source_medium`` and ``by_device`` read ``ga4_traffic_daily`` unfiltered so
  they stay comparable to Agency Analytics' tables; their sums can exceed
  ``totals`` by a percent or two — that is GA4's dimension inflation, not a
  join bug. Key events are never host-filtered either: click-to-call and similar
  events fire with no page host, and filtering dropped CHAA from 37 to 22.
* **Only the page tables are host-filtered.** ``top_landing_pages`` and
  ``top_pages`` are filtered to the property's production hostname
  (``primary_hostname`` from ``ga4_properties_catalog``) so ``localhost`` /
  Vercel-preview paths never appear in a client-facing table.
  ``hostname_filter_applied`` describes exactly that.

Rates (``engagement_rate``, ``avg_engagement_seconds``) are derived here in
Python — the tables store counts only, so summing days reproduces the window.
While the two dimension-free tables have not landed yet the reader falls back
to the traffic table (unfiltered) with ``users_basis = "daily_sum"`` and a
warning, so the deployed route keeps working.
"""
from __future__ import annotations

import datetime as _dt
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from .queries import Window, _CLINIC_DATA, _client, _date_between

log = logging.getLogger(__name__)

_T_TRAFFIC = f"`{_CLINIC_DATA}.ga4_traffic_daily`"
_T_SESSIONS_DAILY = f"`{_CLINIC_DATA}.ga4_sessions_daily`"
_T_SESSIONS_MONTHLY = f"`{_CLINIC_DATA}.ga4_sessions_monthly`"
_T_LANDING = f"`{_CLINIC_DATA}.ga4_landing_pages_daily`"
_T_PAGES = f"`{_CLINIC_DATA}.ga4_pages_daily`"
_T_EVENTS = f"`{_CLINIC_DATA}.ga4_key_events_daily`"
_T_CATALOG = f"`{_CLINIC_DATA}.ga4_properties_catalog`"

TOP_SOURCE_MEDIUM = 25
TOP_PAGES = 20

USERS_DAILY_SUM = "daily_sum"
USERS_MONTHLY_UNIQUE = "monthly_unique"   # one whole calendar month: GA4's own totals row
USERS_MONTHLY_SUM = "monthly_sum"         # several whole months: monthly rows summed

_TOTAL_KEYS = ("sessions", "total_users", "new_users", "engaged_sessions",
               "engagement_rate", "avg_engagement_seconds", "screen_page_views",
               "key_events")


# ── pure helpers (unit-tested without BigQuery) ──────────────────────────────

def previous_window(w: Window) -> Window:
    """The immediately preceding window of equal length (inclusive dates).

    Not floored to ``MIN_WINDOW_DATE``: GA4 history goes back ~15 months from
    the first ingest regardless of the PMS/Invoca cutoff, and an empty prior
    window simply yields zero totals."""
    span = w.span_days
    prev_end_incl = w.start - _dt.timedelta(days=1)
    prev_start = w.start - _dt.timedelta(days=span)
    return Window(prev_start.isoformat(), prev_end_incl.isoformat())


def whole_months(w: Window) -> list[_dt.date] | None:
    """The first-of-month dates a window covers, if and only if it is exactly
    one or more whole calendar months; ``None`` otherwise. Drives the
    ``monthly_unique`` users rule."""
    if w.start.day != 1 or w.end_excl.day != 1 or w.end_excl <= w.start:
        return None
    months = []
    cur = w.start
    while cur < w.end_excl:
        months.append(cur)
        cur = (cur.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
    return months


def engagement_rate(engaged: int | float, sessions: int | float) -> float | None:
    """engaged / sessions as a percentage, 2 dp; None when there are no sessions."""
    if not sessions:
        return None
    return round(float(engaged) / float(sessions) * 100.0, 2)


def avg_engagement_seconds(duration: float, sessions: int | float) -> float | None:
    """Total engagement seconds / sessions, 1 dp; None when there are no sessions."""
    if not sessions:
        return None
    return round(float(duration or 0.0) / float(sessions), 1)


def _int(v: Any) -> int:
    return int(v or 0)


def totals_from_daily(rows: list[dict]) -> dict:
    """Collapse per-day rows into the ``totals`` block. Rates are derived from
    the summed counts, never averaged."""
    sessions = sum(_int(r.get("sessions")) for r in rows)
    engaged = sum(_int(r.get("engaged_sessions")) for r in rows)
    duration = sum(float(r.get("user_engagement_duration") or 0.0) for r in rows)
    return {
        "sessions": sessions,
        "total_users": sum(_int(r.get("total_users")) for r in rows),
        "new_users": sum(_int(r.get("new_users")) for r in rows),
        "engaged_sessions": engaged,
        "engagement_rate": engagement_rate(engaged, sessions),
        "avg_engagement_seconds": avg_engagement_seconds(duration, sessions),
        "screen_page_views": sum(_int(r.get("screen_page_views")) for r in rows),
        "key_events": sum(_int(r.get("key_events")) for r in rows),
    }


def apply_monthly_users(totals: dict, monthly_rows: list[dict] | None,
                        months: list[_dt.date] | None, n_properties: int) -> str:
    """Overwrite the headline metrics in ``totals`` from the monthly table when
    the window is whole months AND every (property, month) pair is present.
    Every metric the monthly rows carry is replaced (sessions included — GA4's
    daily rows do not sum to its monthly row); rates are re-derived from the
    replaced counts. Returns the ``users_basis`` label: ``monthly_unique`` for a
    single month (GA4's own totals row, exact), ``monthly_sum`` for several
    (unique users are then summed across months). Pure, so the rule is testable."""
    if not months or monthly_rows is None:
        return USERS_DAILY_SUM
    by_month = {_iso(r["segments_month"]): r for r in monthly_rows}
    for m in months:
        r = by_month.get(m.isoformat())
        if r is None or _int(r.get("props")) < n_properties:
            return USERS_DAILY_SUM
    rows = [by_month[m.isoformat()] for m in months]
    for key in ("sessions", "total_users", "new_users", "engaged_sessions",
                "screen_page_views", "key_events"):
        if all(r.get(key) is not None for r in rows):
            totals[key] = sum(_int(r[key]) for r in rows)
    if all(r.get("user_engagement_duration") is not None for r in rows):
        duration = sum(float(r["user_engagement_duration"] or 0.0) for r in rows)
        totals["avg_engagement_seconds"] = avg_engagement_seconds(duration, totals["sessions"])
    if all(r.get("engaged_sessions") is not None for r in rows):
        totals["engagement_rate"] = engagement_rate(totals["engaged_sessions"], totals["sessions"])
    return USERS_MONTHLY_UNIQUE if len(months) == 1 else USERS_MONTHLY_SUM


def zero_totals() -> dict:
    return {k: (None if k in ("engagement_rate", "avg_engagement_seconds") else 0)
            for k in _TOTAL_KEYS}


def empty_sections(w: Window) -> dict:
    """The payload body when no property is registered (or nothing has landed):
    every list empty, totals zero, ``data_through`` null. 200, not 404 — the SPA
    renders an empty state."""
    return {
        "window": {"start": w.start_date,
                   "end": (w.end_excl - _dt.timedelta(days=1)).isoformat()},
        "hostname_filter_applied": False,
        "users_basis": USERS_DAILY_SUM,
        "data_through": None,
        "totals": zero_totals(),
        "previous_totals": None,
        "sessions_daily": [],
        "by_channel": [],
        "by_source_medium": [],
        "by_device": [],
        "top_landing_pages": [],
        "top_pages": [],
        "key_events": [],
        "key_events_by_channel": [],
    }


# ── SQL builders ─────────────────────────────────────────────────────────────
#
# Scope is passed as two aligned arrays — ``@pids`` and ``@hosts`` ('' where the
# property has no known production host) — and joined as a CTE so the same
# fragment serves every table. The host predicate is the ONE line that differs
# between the page tables and everything else; tests pin its presence and
# absence.

_SCOPE_CTE = """
WITH scope AS (
  SELECT pid, NULLIF(host, '') AS host
  FROM UNNEST(@pids) AS pid WITH OFFSET AS i
  JOIN UNNEST(@hosts) AS host WITH OFFSET AS j ON i = j
)"""

_HOST_JOIN = "JOIN scope s ON s.pid = t.ga4_property_id AND (s.host IS NULL OR t.host_name = s.host)"
_NOHOST_JOIN = "JOIN scope s ON s.pid = t.ga4_property_id"

_DAILY_SUMS = """       SUM(t.sessions) AS sessions,
       SUM(t.total_users) AS total_users,
       SUM(t.new_users) AS new_users,
       SUM(t.engaged_sessions) AS engaged_sessions,
       SUM(t.user_engagement_duration) AS user_engagement_duration,
       SUM(t.screen_page_views) AS screen_page_views,
       SUM(t.key_events) AS key_events"""


def sessions_daily_sql(w: Window) -> str:
    """Totals source: the dimension-free per-property × day table. No host
    column exists, so no host filter."""
    return f"""{_SCOPE_CTE}
SELECT t.segments_date AS day,
{_DAILY_SUMS}
FROM {_T_SESSIONS_DAILY} t
{_NOHOST_JOIN}
WHERE {_date_between("t.segments_date", w)}
GROUP BY day
ORDER BY day"""


def sessions_monthly_sql() -> str:
    """Whole-month headline: property × month rows carrying every totals metric.
    GA4's daily rows do not sum to its monthly row (sessions spanning midnight
    are estimated per row, both directions), and Agency Analytics shows the
    monthly row — so a whole-month window takes ALL headline metrics from here.
    ``props`` lets the caller verify every scoped property has a row per month."""
    return f"""{_SCOPE_CTE}
SELECT t.segments_month AS segments_month,
       SUM(t.sessions) AS sessions,
       SUM(t.total_users) AS total_users,
       SUM(t.new_users) AS new_users,
       SUM(t.engaged_sessions) AS engaged_sessions,
       SUM(t.user_engagement_duration) AS user_engagement_duration,
       SUM(t.screen_page_views) AS screen_page_views,
       SUM(t.key_events) AS key_events,
       COUNT(DISTINCT t.ga4_property_id) AS props
FROM {_T_SESSIONS_MONTHLY} t
{_NOHOST_JOIN}
WHERE t.segments_month IN UNNEST(@months)
GROUP BY segments_month
ORDER BY segments_month"""


def traffic_daily_sql(w: Window) -> str:
    """FALLBACK totals source while ``ga4_sessions_daily`` is absent: the
    dimensioned traffic table summed per day, unfiltered (over-reads slightly)."""
    return f"""{_SCOPE_CTE}
SELECT t.segments_date AS day,
{_DAILY_SUMS}
FROM {_T_TRAFFIC} t
{_NOHOST_JOIN}
WHERE {_date_between("t.segments_date", w)}
GROUP BY day
ORDER BY day"""


def traffic_by_sql(dim_expr: str, w: Window, limit: int | None = None) -> str:
    """Traffic grouped by a dimension expression over ``t`` — NOT host-filtered,
    so the breakdown tables match Agency Analytics'."""
    lim = f"\nLIMIT {int(limit)}" if limit else ""
    return f"""{_SCOPE_CTE}
SELECT {dim_expr} AS dim,
{_DAILY_SUMS},
       SUM(t.event_count) AS event_count
FROM {_T_TRAFFIC} t
{_NOHOST_JOIN}
WHERE {_date_between("t.segments_date", w)}
GROUP BY dim
ORDER BY sessions DESC{lim}"""


def landing_pages_sql(w: Window, limit: int = TOP_PAGES) -> str:
    """Host-filtered: preview/localhost paths must not reach a client table."""
    return f"""{_SCOPE_CTE}
SELECT t.landing_page AS landing_page,
       SUM(t.sessions) AS sessions,
       SUM(t.engaged_sessions) AS engaged_sessions,
       SUM(t.key_events) AS key_events
FROM {_T_LANDING} t
{_HOST_JOIN}
WHERE {_date_between("t.segments_date", w)}
GROUP BY landing_page
ORDER BY sessions DESC
LIMIT {int(limit)}"""


def pages_sql(w: Window, limit: int = TOP_PAGES) -> str:
    """Host-filtered, same reason as ``landing_pages_sql``."""
    return f"""{_SCOPE_CTE}
SELECT t.page_path AS page_path,
       SUM(t.screen_page_views) AS screen_page_views,
       SUM(t.sessions) AS sessions,
       SUM(t.user_engagement_duration) AS user_engagement_duration
FROM {_T_PAGES} t
{_HOST_JOIN}
WHERE {_date_between("t.segments_date", w)}
GROUP BY page_path
ORDER BY screen_page_views DESC
LIMIT {int(limit)}"""


def key_events_sql(w: Window) -> str:
    """Key events by name and by (name, channel) in one pass — NO host filter."""
    return f"""{_SCOPE_CTE}
SELECT t.event_name AS event_name,
       t.session_default_channel_group AS channel,
       SUM(t.event_count) AS event_count,
       SUM(t.total_users) AS total_users
FROM {_T_EVENTS} t
{_NOHOST_JOIN}
WHERE t.is_key_event AND {_date_between("t.segments_date", w)}
GROUP BY event_name, channel
ORDER BY event_count DESC"""


def data_through_sql() -> str:
    """Freshness: latest landed day across the scoped properties (traffic
    table — always present)."""
    return f"""{_SCOPE_CTE}
SELECT MAX(t.segments_date) AS data_through
FROM {_T_TRAFFIC} t
{_NOHOST_JOIN}"""


def catalog_meta_sql() -> str:
    return f"""SELECT property_id, property_name, primary_hostname, time_zone
FROM {_T_CATALOG}
WHERE property_id IN UNNEST(@pids)"""


# ── BigQuery execution ───────────────────────────────────────────────────────

def _scope_params(pids: list[str], hosts: list[str]) -> list:
    return [bigquery.ArrayQueryParameter("pids", "STRING", pids),
            bigquery.ArrayQueryParameter("hosts", "STRING", hosts)]


def _run(sql: str, params: list, client: bigquery.Client | None = None) -> list[dict]:
    client = client or _client()
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    return [dict(r) for r in job.result()]


def _run_optional(sql: str, params: list, client: bigquery.Client | None,
                  what: str) -> list[dict] | None:
    """``_run`` that returns None (with a warning) when the table does not exist
    yet — the two dimension-free tables land after the first ETL deploy."""
    try:
        return _run(sql, params, client)
    except NotFound as exc:
        log.warning("ga4 %s table not found, falling back: %s", what, str(exc)[:120])
        return None


def catalog_meta(property_ids: list[str]) -> dict[str, dict]:
    """``{property_id: {property_name, primary_hostname, time_zone}}`` from the
    catalog table. Tolerates a missing table / failed read (→ ``{}``), which
    means no host filter and null time zones rather than a failed page."""
    if not property_ids:
        return {}
    try:
        rows = _run(catalog_meta_sql(),
                    [bigquery.ArrayQueryParameter("pids", "STRING", list(property_ids))])
    except Exception as exc:  # noqa: BLE001 — catalog is advisory
        log.warning("ga4 catalog read failed (no host filter will apply): %s", exc)
        return {}
    return {str(r["property_id"]): {
        "property_name": r.get("property_name"),
        "primary_hostname": r.get("primary_hostname") or None,
        "time_zone": r.get("time_zone"),
    } for r in rows}


def _iso(d: Any) -> str | None:
    if d is None:
        return None
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def _daily_rows(rows: list[dict]) -> list[dict]:
    return [{
        "day": _iso(r["day"]),
        "sessions": _int(r["sessions"]),
        "total_users": _int(r["total_users"]),
        "new_users": _int(r["new_users"]),
        "engaged_sessions": _int(r["engaged_sessions"]),
        "user_engagement_duration": float(r["user_engagement_duration"] or 0.0),
        "screen_page_views": _int(r["screen_page_views"]),
        "key_events": _int(r["key_events"]),
    } for r in rows]


def ga4_website(property_ids: list[str], hostnames: dict[str, str | None],
                window: Window, *, client: bigquery.Client | None = None) -> dict:
    """The website payload body for a set of GA4 properties over ``window``.

    ``hostnames`` maps property id → production hostname (or None = no filter
    for that property); it applies to the page tables only. The route layer
    wraps this with clinic/instance identity and the ``properties`` list.
    """
    if not property_ids:
        return empty_sections(window)

    pids = [str(p) for p in property_ids]
    hosts = [(hostnames.get(p) or "") for p in pids]
    params = _scope_params(pids, hosts)
    prev = previous_window(window)
    months = whole_months(window)
    prev_months = whole_months(prev)
    client = client or _client()

    def month_params(ms: list[_dt.date]) -> list:
        return params + [bigquery.ArrayQueryParameter("months", "DATE", ms)]

    with ThreadPoolExecutor(max_workers=8) as ex:
        f_daily = ex.submit(_run_optional, sessions_daily_sql(window), params, client, "sessions_daily")
        f_prev = ex.submit(_run_optional, sessions_daily_sql(prev), params, client, "sessions_daily")
        f_month = (ex.submit(_run_optional, sessions_monthly_sql(), month_params(months), client,
                             "sessions_monthly") if months else None)
        f_pmonth = (ex.submit(_run_optional, sessions_monthly_sql(), month_params(prev_months), client,
                              "sessions_monthly") if prev_months else None)
        req = {
            "channel": ex.submit(_run, traffic_by_sql("t.session_default_channel_group", window), params, client),
            "source_medium": ex.submit(_run, traffic_by_sql(
                "CONCAT(IFNULL(t.session_source, '(not set)'), ' / ', IFNULL(t.session_medium, '(not set)'))",
                window, TOP_SOURCE_MEDIUM), params, client),
            "device": ex.submit(_run, traffic_by_sql("t.device_category", window), params, client),
            "landing": ex.submit(_run, landing_pages_sql(window), params, client),
            "pages": ex.submit(_run, pages_sql(window), params, client),
            "events": ex.submit(_run, key_events_sql(window), params, client),
            "through": ex.submit(_run, data_through_sql(), params, client),
        }
        daily_rows = f_daily.result()
        prev_rows = f_prev.result()
        monthly = f_month.result() if f_month else None
        prev_monthly = f_pmonth.result() if f_pmonth else None
        res = {k: f.result() for k, f in req.items()}

        # Fallback: the dimension-free table is absent (None) OR exists but has
        # not been backfilled for these properties yet (empty while the traffic
        # table has rows) → traffic table, unfiltered. Both happen during the
        # first ETL rollout; zeros next to populated breakdowns would be worse
        # than a slightly inflated total.
        fell_back = False
        if not daily_rows:
            fb = _run(traffic_daily_sql(window), params, client)
            if fb:
                if daily_rows is not None:
                    log.warning("ga4_sessions_daily has no rows for %s in %s..%s; "
                                "using traffic table", pids, window.start_date, window.end_date_excl)
                daily_rows, fell_back = fb, True
        if not prev_rows:
            fb = _run(traffic_daily_sql(prev), params, client)
            if fb:
                prev_rows = fb
                if fell_back is False and prev_rows:
                    fell_back = fell_back or daily_rows is None or not daily_rows

    daily = _daily_rows(daily_rows or [])
    totals = totals_from_daily(daily)
    users_basis = (USERS_DAILY_SUM if fell_back
                   else apply_monthly_users(totals, monthly, months, len(pids)))
    if fell_back:
        # The traffic table's key_events column is dimension-inflated; the
        # events table (event × channel) is the closest dimension-free source.
        totals["key_events"] = sum(_int(r["event_count"]) for r in res["events"])

    prev_daily = _daily_rows(prev_rows or [])
    previous_totals: dict | None = totals_from_daily(prev_daily) if prev_daily else None
    if previous_totals is not None and not fell_back:
        apply_monthly_users(previous_totals, prev_monthly, prev_months, len(pids))

    # Key events by channel ride onto the channel rows (never host-filtered).
    ke_by_channel: dict[str, int] = {}
    key_events_by_channel = []
    key_events_by_name: dict[str, dict] = {}
    for r in res["events"]:
        ch = r.get("channel") or "(not set)"
        ke_by_channel[ch] = ke_by_channel.get(ch, 0) + _int(r["event_count"])
        key_events_by_channel.append({"event_name": r["event_name"], "channel": ch,
                                      "event_count": _int(r["event_count"])})
        agg = key_events_by_name.setdefault(r["event_name"], {"event_name": r["event_name"],
                                                               "event_count": 0, "total_users": 0})
        agg["event_count"] += _int(r["event_count"])
        agg["total_users"] += _int(r["total_users"])
    key_events = sorted(key_events_by_name.values(), key=lambda e: -e["event_count"])

    by_channel = [{
        "channel": r["dim"] or "(not set)",
        "sessions": _int(r["sessions"]),
        "total_users": _int(r["total_users"]),
        "engaged_sessions": _int(r["engaged_sessions"]),
        "key_events": ke_by_channel.get(r["dim"] or "(not set)", 0),
    } for r in res["channel"]]

    by_source_medium = [{
        "source_medium": r["dim"],
        "sessions": _int(r["sessions"]),
        "total_users": _int(r["total_users"]),
        "new_users": _int(r["new_users"]),
        "engaged_sessions": _int(r["engaged_sessions"]),
        "avg_engagement_seconds": avg_engagement_seconds(r["user_engagement_duration"], r["sessions"]),
        "screen_page_views": _int(r["screen_page_views"]),
        "key_events": _int(r["key_events"]),
        "event_count": _int(r["event_count"]),
    } for r in res["source_medium"]]

    by_device = [{"device": r["dim"] or "(not set)", "sessions": _int(r["sessions"])}
                 for r in res["device"]]

    top_landing_pages = [{
        "landing_page": r["landing_page"],
        "sessions": _int(r["sessions"]),
        "engaged_sessions": _int(r["engaged_sessions"]),
        "key_events": _int(r["key_events"]),
    } for r in res["landing"]]

    top_pages = [{
        "page_path": r["page_path"],
        "screen_page_views": _int(r["screen_page_views"]),
        "sessions": _int(r["sessions"]),
        "avg_engagement_seconds": avg_engagement_seconds(r["user_engagement_duration"], r["sessions"]),
    } for r in res["pages"]]

    through = res["through"][0]["data_through"] if res["through"] else None

    # Public daily rows carry the sessions_daily shape only.
    public_daily = [{k: d[k] for k in ("day", "sessions", "total_users", "new_users",
                                        "engaged_sessions", "key_events")} for d in daily]

    return {
        "window": {"start": window.start_date,
                   "end": (window.end_excl - _dt.timedelta(days=1)).isoformat()},
        "hostname_filter_applied": any(h for h in hosts),
        "users_basis": users_basis,
        "data_through": _iso(through),
        "totals": totals,
        "previous_totals": previous_totals,
        "sessions_daily": public_daily,
        "by_channel": by_channel,
        "by_source_medium": by_source_medium,
        "by_device": by_device,
        "top_landing_pages": top_landing_pages,
        "top_pages": top_pages,
        "key_events": key_events,
        "key_events_by_channel": key_events_by_channel,
    }
