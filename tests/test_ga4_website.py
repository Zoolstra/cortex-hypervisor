"""
GA4 website reader + routes (``intelligence_report/ga4_queries.py``,
``GET /intelligence/{clinic_id}/website``, ``/intelligence/group/{instance_id}/website``).

Pure-function tests plus SQL-shape pins; BigQuery is never reached. The rules
worth their own tests (plan §2.3 + the 2026-09-10 parity follow-up):

* totals read the dimension-free ``ga4_sessions_daily`` with NO host predicate;
* whole-calendar-month windows take users from ``ga4_sessions_monthly``;
* breakdowns (traffic table) are NOT host-filtered; page tables ARE;
* key events are never host-filtered;
* the reader falls back to the traffic table when the new tables are absent.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound

from api import app
from api.deps import verify_token
from api.core.db import get_session
import api.intelligence as intel
from intelligence_report import ga4_queries as gq
from intelligence_report.queries import Window


W = Window("2026-08-01", "2026-08-31")          # one whole month
W_PARTIAL = Window("2026-08-05", "2026-08-31")  # not whole months


# ── pure math ────────────────────────────────────────────────────────────────

def test_previous_window_is_adjacent_and_same_length():
    p = gq.previous_window(W)
    assert p.span_days == W.span_days == 31
    assert p.end_excl == W.start                      # touches, no overlap
    assert p.start_date == "2026-07-01"
    assert (p.end_excl - dt.timedelta(days=1)).isoformat() == "2026-07-31"


def test_previous_window_single_day():
    p = gq.previous_window(Window("2026-08-01", "2026-08-01"))
    assert p.start_date == "2026-07-31" and p.span_days == 1


@pytest.mark.parametrize("start,end,expected", [
    ("2026-08-01", "2026-08-31", ["2026-08-01"]),
    ("2026-06-01", "2026-08-31", ["2026-06-01", "2026-07-01", "2026-08-01"]),
    ("2025-12-01", "2026-01-31", ["2025-12-01", "2026-01-01"]),        # year boundary
    ("2026-02-01", "2026-02-28", ["2026-02-01"]),                      # short month
    ("2026-08-05", "2026-08-31", None),                                # starts mid-month
    ("2026-08-01", "2026-08-30", None),                                # ends early
    ("2026-08-01", "2026-09-15", None),                                # ragged end
])
def test_whole_months(start, end, expected):
    got = gq.whole_months(Window(start, end))
    assert (None if got is None else [m.isoformat() for m in got]) == expected


def test_engagement_rate_and_avg_seconds():
    assert gq.engagement_rate(1118, 3555) == 31.45
    assert gq.engagement_rate(0, 0) is None
    assert gq.avg_engagement_seconds(7110.0, 3555) == 2.0
    assert gq.avg_engagement_seconds(100.0, 0) is None


def test_totals_from_daily_sums_counts_and_derives_rates():
    rows = [
        {"sessions": 10, "total_users": 8, "new_users": 5, "engaged_sessions": 4,
         "user_engagement_duration": 100.0, "screen_page_views": 20, "key_events": 1},
        {"sessions": 30, "total_users": 20, "new_users": 15, "engaged_sessions": 16,
         "user_engagement_duration": 300.0, "screen_page_views": 60, "key_events": 2},
    ]
    t = gq.totals_from_daily(rows)
    assert t["sessions"] == 40 and t["total_users"] == 28 and t["new_users"] == 20
    assert t["engaged_sessions"] == 20 and t["engagement_rate"] == 50.0
    assert t["avg_engagement_seconds"] == 10.0
    assert t["screen_page_views"] == 80 and t["key_events"] == 3
    assert set(t) == set(gq._TOTAL_KEYS)


def _m(month, users, new, props=1):
    return {"segments_month": dt.date.fromisoformat(month), "total_users": users,
            "new_users": new, "props": props}


def test_monthly_users_applied_only_when_every_property_month_present():
    months = gq.whole_months(Window("2026-07-01", "2026-08-31"))
    t = {"total_users": 999, "new_users": 999}
    # both months, one property → monthly_unique, users summed across months
    assert gq.apply_monthly_users(t, [_m("2026-07-01", 100, 80), _m("2026-08-01", 200, 150)],
                                  months, 1) == "monthly_sum"     # two months → summed, not unique
    assert t == {"total_users": 300, "new_users": 230}
    # a month missing → daily_sum, totals untouched
    t = {"total_users": 999, "new_users": 999}
    assert gq.apply_monthly_users(t, [_m("2026-07-01", 100, 80)], months, 1) == "daily_sum"
    assert t == {"total_users": 999, "new_users": 999}
    # two properties scoped but a month only has one → daily_sum
    t = {"total_users": 999, "new_users": 999}
    assert gq.apply_monthly_users(t, [_m("2026-07-01", 100, 80, props=2), _m("2026-08-01", 5, 5, props=1)],
                                  months, 2) == "daily_sum"
    # not a whole-month window → daily_sum regardless of rows
    assert gq.apply_monthly_users({}, [_m("2026-08-01", 1, 1)], None, 1) == "daily_sum"
    # table absent (None) → daily_sum
    assert gq.apply_monthly_users({}, None, months, 1) == "daily_sum"


def test_empty_sections_shape():
    e = gq.empty_sections(W)
    assert e["window"] == {"start": "2026-08-01", "end": "2026-08-31"}
    assert e["data_through"] is None and e["previous_totals"] is None
    assert e["hostname_filter_applied"] is False and e["users_basis"] == "daily_sum"
    assert e["totals"]["sessions"] == 0 and e["totals"]["engagement_rate"] is None
    for k in ("sessions_daily", "by_channel", "by_source_medium", "by_device",
              "top_landing_pages", "top_pages", "key_events", "key_events_by_channel"):
        assert e[k] == []


def test_ga4_website_with_no_properties_never_queries(monkeypatch):
    monkeypatch.setattr(gq, "_run", lambda *a, **k: pytest.fail("BigQuery must not be called"))
    out = gq.ga4_website([], {}, W)
    assert out == gq.empty_sections(W)


# ── SQL shape pins ───────────────────────────────────────────────────────────

HOST_PRED = "t.host_name = s.host"


def _body(sql: str) -> str:
    """Everything after the scope CTE (the CTE itself mentions `host`)."""
    return sql.split("scope AS")[1].split(")", 1)[1]


def test_totals_sql_reads_dimension_free_table_without_host_predicate():
    sql = gq.sessions_daily_sql(W)
    assert "ga4_sessions_daily" in sql and "ga4_traffic_daily" not in sql
    assert HOST_PRED not in sql and "host_name" not in _body(sql)
    assert "UNNEST(@pids)" in sql
    assert "t.segments_date >= DATE '2026-08-01'" in sql
    assert "t.segments_date < DATE '2026-09-01'" in sql
    for col in ("sessions", "total_users", "new_users", "engaged_sessions",
                "user_engagement_duration", "screen_page_views", "key_events"):
        assert f"SUM(t.{col})" in sql


def test_monthly_sql_shape():
    sql = gq.sessions_monthly_sql()
    assert "ga4_sessions_monthly" in sql and "UNNEST(@months)" in sql
    assert "COUNT(DISTINCT t.ga4_property_id) AS props" in sql
    assert HOST_PRED not in sql


@pytest.mark.parametrize("sql", [
    gq.traffic_daily_sql(W),                                        # the fallback
    gq.traffic_by_sql("t.session_default_channel_group", W),
    gq.traffic_by_sql("t.device_category", W),
])
def test_breakdowns_and_fallback_are_not_host_filtered(sql):
    assert "ga4_traffic_daily" in sql
    assert HOST_PRED not in sql and "host_name" not in _body(sql)
    assert "t.segments_date >= DATE '2026-08-01'" in sql


@pytest.mark.parametrize("sql", [gq.landing_pages_sql(W), gq.pages_sql(W)])
def test_page_tables_are_host_filtered(sql):
    assert HOST_PRED in sql
    assert "UNNEST(@pids)" in sql and "UNNEST(@hosts)" in sql
    assert "LIMIT 20" in sql


@pytest.mark.parametrize("sql", [gq.key_events_sql(W), gq.data_through_sql()])
def test_key_events_and_freshness_are_never_host_filtered(sql):
    assert HOST_PRED not in sql and "host_name" not in _body(sql)


def test_key_event_query_only_counts_key_events():
    assert "t.is_key_event" in gq.key_events_sql(W)


def test_catalog_meta_tolerates_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no such table")
    monkeypatch.setattr(gq, "_run", boom)
    assert gq.catalog_meta(["1"]) == {}
    assert gq.catalog_meta([]) == {}


# ── assembly with a fake BigQuery ────────────────────────────────────────────

CUR = "t.segments_date >= DATE '2026-08-"   # lower bound of BOTH current windows; prev windows start in July
D1, D2 = dt.date(2026, 8, 1), dt.date(2026, 8, 2)


def _fake_run(*, sessions_tables: bool):
    """A fake `_run`. With ``sessions_tables=False`` the two dimension-free
    tables raise NotFound, exercising the fallback."""
    def fake_run(sql, params, client=None):
        if "ga4_sessions_daily" in sql:
            if not sessions_tables:
                raise NotFound("ga4_sessions_daily")
            if CUR not in sql:
                return [{"day": dt.date(2026, 7, 1), "sessions": 50, "total_users": 45, "new_users": 40,
                         "engaged_sessions": 20, "user_engagement_duration": 100.0,
                         "screen_page_views": 90, "key_events": 2}]
            return [{"day": D1, "sessions": 100, "total_users": 90, "new_users": 80,
                     "engaged_sessions": 50, "user_engagement_duration": 500.0,
                     "screen_page_views": 200, "key_events": 3},
                    {"day": D2, "sessions": 0, "total_users": 0, "new_users": 0,
                     "engaged_sessions": 0, "user_engagement_duration": 0.0,
                     "screen_page_views": 0, "key_events": 2}]
        if "ga4_sessions_monthly" in sql:
            if not sessions_tables:
                raise NotFound("ga4_sessions_monthly")
            months = [p.values for p in params if p.name == "months"][0]
            if months == [dt.date(2026, 8, 1)]:
                return [_m("2026-08-01", 70, 60)]            # unique users < daily sum 90
            return []                                       # July: no monthly row yet
        if "ga4_traffic_daily" in sql and "AS day" in sql:  # fallback totals
            if CUR not in sql:
                return []
            return [{"day": D1, "sessions": 103, "total_users": 95, "new_users": 82,
                     "engaged_sessions": 51, "user_engagement_duration": 510.0,
                     "screen_page_views": 201, "key_events": 3}]
        if "ga4_key_events_daily" in sql:
            return [{"event_name": "click_to_call", "channel": "Direct", "event_count": 3, "total_users": 3},
                    {"event_name": "form_submit", "channel": "Paid Search", "event_count": 2, "total_users": 2}]
        if "MAX(t.segments_date)" in sql:
            return [{"data_through": D2}]
        if "ga4_traffic_daily" in sql:                      # breakdowns by dim
            return [{"dim": "Direct", "sessions": 62, "total_users": 57, "new_users": 50,
                     "engaged_sessions": 30, "user_engagement_duration": 300.0,
                     "screen_page_views": 120, "key_events": 1, "event_count": 400},
                    {"dim": "Paid Search", "sessions": 41, "total_users": 38, "new_users": 32,
                     "engaged_sessions": 21, "user_engagement_duration": 200.0,
                     "screen_page_views": 81, "key_events": 1, "event_count": 300}]
        if "ga4_landing_pages_daily" in sql:
            return [{"landing_page": "/", "sessions": 70, "engaged_sessions": 40, "key_events": 1}]
        if "ga4_pages_daily" in sql:
            return [{"page_path": "/", "screen_page_views": 150, "sessions": 70,
                     "user_engagement_duration": 350.0}]
        raise AssertionError(f"unexpected sql: {sql[:80]}")
    return fake_run


def test_ga4_website_whole_month_uses_dimension_free_totals_and_monthly_users(monkeypatch):
    monkeypatch.setattr(gq, "_run", _fake_run(sessions_tables=True))
    monkeypatch.setattr(gq, "_client", lambda: object())

    out = gq.ga4_website(["268146803"], {"268146803": "www.calgaryhearingaid.ca"}, W)

    assert out["hostname_filter_applied"] is True       # page tables only
    assert out["users_basis"] == "monthly_unique"
    assert out["data_through"] == "2026-08-02"
    # totals from ga4_sessions_daily (100 sessions), users from the monthly table (70/60)
    assert out["totals"] == {"sessions": 100, "total_users": 70, "new_users": 60,
                             "engaged_sessions": 50, "engagement_rate": 50.0,
                             "avg_engagement_seconds": 5.0, "screen_page_views": 200,
                             "key_events": 5}
    # July has no monthly row → previous_totals keep the daily-sum users
    assert out["previous_totals"]["sessions"] == 50 and out["previous_totals"]["total_users"] == 45
    assert out["previous_totals"]["key_events"] == 2
    assert out["sessions_daily"] == [
        {"day": "2026-08-01", "sessions": 100, "total_users": 90, "new_users": 80,
         "engaged_sessions": 50, "key_events": 3},
        {"day": "2026-08-02", "sessions": 0, "total_users": 0, "new_users": 0,
         "engaged_sessions": 0, "key_events": 2},
    ]
    # Breakdowns come from the (unfiltered) traffic table and may sum past totals: 62+41 = 103 > 100.
    assert sum(r["sessions"] for r in out["by_channel"]) == 103
    assert out["by_channel"][0] == {"channel": "Direct", "sessions": 62, "total_users": 57,
                                    "engaged_sessions": 30, "key_events": 3}
    assert out["by_channel"][1]["key_events"] == 2
    assert out["by_source_medium"][0]["avg_engagement_seconds"] == round(300.0 / 62, 1)
    assert out["by_device"] == [{"device": "Direct", "sessions": 62}, {"device": "Paid Search", "sessions": 41}]
    assert out["top_pages"][0]["avg_engagement_seconds"] == 5.0
    assert out["key_events"] == [{"event_name": "click_to_call", "event_count": 3, "total_users": 3},
                                 {"event_name": "form_submit", "event_count": 2, "total_users": 2}]
    assert out["key_events_by_channel"][1]["channel"] == "Paid Search"


def test_ga4_website_partial_month_keeps_daily_sum_users(monkeypatch):
    calls = []
    fake = _fake_run(sessions_tables=True)

    def spy(sql, params, client=None):
        calls.append(sql)
        return fake(sql, params, client)
    monkeypatch.setattr(gq, "_run", spy)
    monkeypatch.setattr(gq, "_client", lambda: object())

    out = gq.ga4_website(["268146803"], {"268146803": None}, W_PARTIAL)
    assert out["users_basis"] == "daily_sum"
    assert out["totals"]["total_users"] == 90                 # daily sum, not the monthly 70
    assert not any("ga4_sessions_monthly" in s for s in calls)  # monthly table never queried
    assert out["hostname_filter_applied"] is False


def test_ga4_website_falls_back_to_traffic_table_when_new_tables_absent(monkeypatch, caplog):
    monkeypatch.setattr(gq, "_run", _fake_run(sessions_tables=False))
    monkeypatch.setattr(gq, "_client", lambda: object())

    with caplog.at_level("WARNING"):
        out = gq.ga4_website(["268146803"], {"268146803": None}, W)

    assert out["users_basis"] == "daily_sum"
    assert out["totals"]["sessions"] == 103 and out["totals"]["total_users"] == 95
    # Fallback takes key events from the events table (click_to_call 3 + form_submit 2),
    # never from the dimension-inflated traffic column — same total the normal path gives.
    assert out["totals"]["key_events"] == 5
    assert out["previous_totals"] is None                     # fallback prev window empty
    assert out["sessions_daily"][0]["day"] == "2026-08-01"
    assert any("falling back" in r.message for r in caplog.records)


# ── routes ───────────────────────────────────────────────────────────────────

class _FakeClinic:
    def __init__(self, instance_id="INST_A"):
        self.clinic_id = "C1"
        self.instance_id = instance_id
        self.clinic_name = "Test Clinic"
        self.deleted_at = None


class _FakeInstance:
    instance_id = "INST_A"
    instance_name = "Inst"


def _session(obj, *, clinic_count=1, props=()):
    def _override():
        yield type("S", (), {
            "get": lambda self, m, i: obj,
            "scalar": lambda self, *a, **k: clinic_count,
            "scalars": lambda self, *a, **k: type("R", (), {
                "all": lambda s2: list(props), "__iter__": lambda s2: iter(props)})(),
        })()
    app.dependency_overrides[get_session] = _override


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(intel, "_SHARED_CACHE_ENABLED", False)
    monkeypatch.setattr(intel, "_data_version", lambda cid: "v")
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def test_website_no_properties_is_200_empty(client, monkeypatch):
    _session(_FakeClinic(), clinic_count=1, props=())
    monkeypatch.setattr(gq, "_run", lambda *a, **k: pytest.fail("no BigQuery for an empty registry"))
    r = client.get("/intelligence/C1/website?start=2026-08-01&end=2026-08-31")
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "clinic" and body["clinic_id"] == "C1" and body["instance_id"] == "INST_A"
    assert body["properties"] == [] and body["data_through"] is None
    assert body["totals"]["sessions"] == 0 and body["sessions_daily"] == []
    assert body["users_basis"] == "daily_sum"
    assert body["window"] == {"start": "2026-08-01", "end": "2026-08-31"}


def test_website_bad_range_422(client):
    _session(_FakeClinic())
    r = client.get("/intelligence/C1/website?start=2026-08-10&end=2026-08-01")
    assert r.status_code == 422


def test_website_missing_clinic_404(client):
    _session(None)
    assert client.get("/intelligence/C1/website").status_code == 404


def test_website_with_property_wraps_reader(client, monkeypatch):
    prop = type("P", (), {"ga4_property_id": "268146803", "property_name": "GA4 - CHAA",
                          "clinic_id": "C1", "active": True})()
    _session(_FakeClinic(), clinic_count=4, props=(prop,))
    monkeypatch.setattr(gq, "catalog_meta", lambda pids: {
        "268146803": {"property_name": "CHAA (catalog)", "primary_hostname": "www.calgaryhearingaid.ca",
                      "time_zone": "America/Edmonton"}})
    captured = {}

    def fake_reader(pids, hostnames, window, **k):
        captured.update(pids=pids, hostnames=hostnames)
        return dict(gq.empty_sections(window), data_through="2026-08-31")
    monkeypatch.setattr(gq, "ga4_website", fake_reader)

    r = client.get("/intelligence/C1/website?start=2026-08-01&end=2026-08-31")
    assert r.status_code == 200
    body = r.json()
    assert captured == {"pids": ["268146803"], "hostnames": {"268146803": "www.calgaryhearingaid.ca"}}
    assert body["properties"] == [{
        "ga4_property_id": "268146803", "property_name": "CHAA (catalog)",
        "primary_hostname": "www.calgaryhearingaid.ca", "time_zone": "America/Edmonton",
        "registered_clinic_id": "C1", "business_wide": True}]
    assert body["data_through"] == "2026-08-31"


def test_group_website_single_location_404(client):
    _session(_FakeInstance(), clinic_count=1)
    r = client.get("/intelligence/group/INST_A/website")
    assert r.status_code == 404


def test_group_website_multi_location_empty_ok(client, monkeypatch):
    _session(_FakeInstance(), clinic_count=4, props=())
    monkeypatch.setattr(gq, "_run", lambda *a, **k: pytest.fail("no BigQuery for an empty registry"))
    r = client.get("/intelligence/group/INST_A/website?start=2026-08-01&end=2026-08-31")
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "group" and body["instance_id"] == "INST_A" and body["clinic_id"] == "INST_A"
    assert body["properties"] == []
