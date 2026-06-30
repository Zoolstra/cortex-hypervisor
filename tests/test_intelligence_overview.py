"""
Tests for the Intelligence Overview / Patient Acquisition / Patient Journey
surface added in the dashboard reorg:

  * ``queries.Window`` date-range semantics (two-sided, YoY shift).
  * ``clinic_hours`` free-text hours parsing.
  * ``payloads.build_overview`` derived metrics (rev/hour, cost/contact).
  * Endpoint wiring: window parsing (422), read-access gating, and the tighter
    admin-only + PHI-audited gate on the patient-journey endpoints.

All BigQuery / LLM work is stubbed — no external calls.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

import api.deps as deps
from api import app
from api.deps import verify_token
from api.core.db import get_session
from intelligence_report import clinic_hours, payloads
from intelligence_report import queries as q
from intelligence_report.queries import MIN_WINDOW_DATE, max_window_date, Window, _year_ago, headline_yoy


# ── Window ───────────────────────────────────────────────────────────────────

def test_window_explicit_is_inclusive_end():
    w = Window("2026-05-01", "2026-05-31")
    assert w.start_date == "2026-05-01"
    assert w.end_date_excl == "2026-06-01"          # exclusive upper bound
    assert w.span_days == 31
    assert w.start_ts == "2026-05-01 00:00:00+00:00"
    assert w.end_ts == "2026-06-01 00:00:00+00:00"


def test_window_single_day_includes_whole_day():
    w = Window("2026-05-01", "2026-05-01")
    assert w.span_days == 1
    assert w.end_date_excl == "2026-05-02"


def test_window_from_days_anchors_at_today():
    w = Window.from_days(30)
    end = max_window_date()
    assert w.end_excl == end + dt.timedelta(days=1)
    assert w.start == max(end - dt.timedelta(days=30), MIN_WINDOW_DATE)


def test_year_ago_preserves_span():
    w = Window("2026-05-01", "2026-05-31")
    wy = _year_ago(w)
    assert wy.start_date == "2025-05-01"
    assert wy.end_date_excl == "2025-06-01"
    assert wy.span_days == w.span_days


def test_year_ago_leap_day():
    w = Window("2024-02-29", "2024-02-29")
    wy = _year_ago(w)
    assert wy.start_date == "2023-02-28"


# ── Hard minimum-date cutoff ─────────────────────────────────────────────────

def test_from_days_floors_to_cutoff():
    w = Window.from_days(100_000)         # far past
    assert w.start == MIN_WINDOW_DATE


def test_floored_returns_none_when_entirely_before_cutoff():
    assert Window("2025-01-01", "2025-12-31").floored() is None


def test_floored_clamps_straddling_window():
    w = Window("2025-10-01", "2026-03-31").floored()
    assert w is not None
    assert w.start == MIN_WINDOW_DATE
    assert w.end_date_excl == "2026-04-01"


def test_floored_clamps_end_to_today():
    # A range running into the far future is clamped down to today's ceiling.
    w = Window("2026-04-01", "2099-12-31").floored()
    assert w is not None
    assert w.start.isoformat() == "2026-04-01"
    assert w.end_excl == max_window_date() + dt.timedelta(days=1)


def test_overview_window_before_cutoff_422(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeClinic())
    monkeypatch.setattr("intelligence_report.payloads.build_overview", lambda **k: {"ok": True})
    r = client.get("/intelligence/C1/overview?start=2025-01-01&end=2025-06-30")
    assert r.status_code == 422


def test_headline_falls_back_to_mom_when_no_year_of_data(monkeypatch):
    # Year-ago window is entirely pre-cutoff (2025) → basis must be "mom".
    monkeypatch.setattr(q, "call_capture",
                        lambda *a, **k: {"calls": 5, "connected": 4, "booked": 2, "capture_rate": 0.5})
    monkeypatch.setattr(q, "form_capture",
                        lambda *a, **k: {"submissions": 3, "form_bookings": 1, "form_rate": 0.33})
    monkeypatch.setattr(q, "invoice_revenue",
                        lambda *a, **k: {"revenue": 1000.0, "invoice_count": 4})
    out = headline_yoy("C1", ["1"], window=Window("2026-06-01", "2026-06-30"))
    assert out["basis"] == "mom"
    assert out["prior_window"]["end"] == "2026-05-31"   # the previous month
    assert out["current"]["contacts"] == 8


# ── clinic_hours ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("9:00 AM - 5:00 PM", 8.0),
    ("9am-5pm", 8.0),
    ("9 - 5", 8.0),
    ("8 to 4", 8.0),
    ("Closed", 0.0),
    ("By appointment", 0.0),
    ("", 0.0),
    (None, 0.0),
    ("9:00 AM - 12:00 PM, 1:00 PM - 5:00 PM", 7.0),
    ("17:00 - 19:30", 2.5),
])
def test_parse_day_hours(raw, expected):
    assert clinic_hours.parse_day_hours(raw) == expected


def test_open_hours_in_window_sums_weekdays():
    loc = {
        "hours_monday": "9-5", "hours_tuesday": "9-5", "hours_wednesday": "9-5",
        "hours_thursday": "9-5", "hours_friday": "9-1", "hours_saturday": "Closed",
        "hours_sunday": None,
    }
    # Mon 2026-06-01 .. Sun 2026-06-07 → 8*4 + 4 = 36
    assert clinic_hours.open_hours_in_window(loc, "2026-06-01", "2026-06-07") == 36.0


# ── payloads.build_overview derived metrics ──────────────────────────────────

def test_build_overview_month_over_month(monkeypatch):
    # System Performance + Operational Health are month-over-month: the last
    # month in the window (May) vs the month before (April). Stubs return the
    # same values for every window, so deltas are 0.
    q = payloads.q
    monkeypatch.setattr(q, "headline_yoy", lambda *a, **k: {"basis": "mom", "current": {}, "prior": {}, "deltas": {}})
    monkeypatch.setattr(q, "monthly_contact_trend", lambda *a, **k: [])
    monkeypatch.setattr(q, "patient_contacts", lambda *a, **k: {"calls": 80, "forms": 20, "total": 100})
    monkeypatch.setattr(q, "invoice_revenue", lambda *a, **k: {"revenue": 36000.0, "invoice_count": 36})
    monkeypatch.setattr(q, "google_ads_roi", lambda *a, **k: [{"spend": 600.0}, {"spend": 400.0}])
    monkeypatch.setattr(q, "front_desk_capture", lambda *a, **k: {"capture_rate": 0.9, "captured": 9, "total": 10})
    monkeypatch.setattr(q, "revenue_leakage", lambda *a, **k: {
        "estimated_leakage": 1234.0, "avg_invoice": 3000.0, "lost_contacts": 4,
        "components": {"missed_calls": 1, "no_shows": 1, "tested_not_sold": 1, "slow_form_followup": 1},
        "intercept_missed": 1, "intercept_recovered": None})
    monkeypatch.setattr(q, "lifecycle_summary", lambda *a, **k: {"review_velocity": None})

    loc = {f"hours_{d}": "9-5" for d in
           ("monday", "tuesday", "wednesday", "thursday", "friday")}
    loc["hours_saturday"] = "Closed"
    loc["hours_sunday"] = None

    payload = payloads.build_overview(
        clinic_id="C1", clinic_name="Test",
        invoca_campaign_ids=["1"], ga_campaign_ids=["2"],
        window=Window("2026-01-01", "2026-05-31"),
        location_hours=loc, tier="growth",
        with_recommendations=False,
    )
    assert payload["mom"]["month"] == "2026-05"
    assert payload["mom"]["prior_month"] == "2026-04"

    sp = payload["system_performance"]
    may_hours = clinic_hours.open_hours_in_window(loc, "2026-05-01", "2026-05-31")
    assert sp["revenue_per_clinic_hour"]["open_hours"] == pytest.approx(may_hours)
    assert sp["revenue_per_clinic_hour"]["value"] == pytest.approx(36000.0 / may_hours)
    # rev/hr delta is non-trivial (May vs April have different open-hours counts).
    assert sp["revenue_per_clinic_hour"]["delta"] is not None
    assert len(sp["revenue_per_clinic_hour"]["series"]) >= 2               # monthly trend
    assert "cost_per_contact" not in sp                                    # removed

    oh = payload["operational_health"]
    assert oh["call_answer_rate"]["value"] == pytest.approx(0.9)
    assert oh["revenue_leakage"]["value"] == pytest.approx(1234.0)
    assert oh["revenue_leakage"]["components"]["missed_calls"] == 1
    assert payload["recommendations"] == []
    assert "cortex_intercept" in payload["placeholders"]


# ── Endpoint wiring ──────────────────────────────────────────────────────────

class _FakeClinic:
    def __init__(self, instance_id="INST_A"):
        self.instance_id = instance_id
        self.clinic_name = "Test Clinic"
        self.deleted_at = None
        self.tier = "bridge"
        self.location = None


def _use_session(clinic):
    def _override():
        yield type("S", (), {"get": lambda self, m, i: clinic,
                             "scalars": lambda self, *a, **k: []})()
    app.dependency_overrides[get_session] = _override


@pytest.fixture
def client():
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def test_overview_bad_date_range_422(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeClinic())
    monkeypatch.setattr("intelligence_report.payloads.build_overview", lambda **k: {"ok": True})
    r = client.get("/intelligence/C1/overview?start=2026-06-10&end=2026-06-01")
    assert r.status_code == 422


def test_overview_super_admin_ok(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeClinic())
    monkeypatch.setattr("intelligence_report.payloads.build_overview",
                        lambda **k: {"clinic_id": "C1", "tier": k["tier"]})
    r = client.get("/intelligence/C1/overview?days=30")
    assert r.status_code == 200
    assert r.json()["tier"] == "bridge"


def test_patient_search_viewer_denied(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "viewer", "uid": "v1"}
    monkeypatch.setattr(deps, "_is_instance_member", lambda inst, uid: True)
    _use_session(_FakeClinic())
    r = client.post("/intelligence/C1/patients/search", json={"q": "smith"})
    assert r.status_code == 403


def test_patient_search_admin_ok_and_audited(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa", "email": "a@b.com"}
    _use_session(_FakeClinic())
    monkeypatch.setattr("intelligence_report.queries.patient_search",
                        lambda cid, q, **k: [{"client_id": "1", "surname": "Smith"}])
    audited = {}
    monkeypatch.setattr("api.intelligence.log_phi_access",
                        lambda **kw: audited.update(kw))
    r = client.post("/intelligence/C1/patients/search", json={"q": "smith"})
    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["surname"] == "Smith"
    assert "query" not in body          # search term is never echoed (PHI)
    assert audited["action"] == "patient_search"
    assert audited["clinic_id"] == "C1"


def test_patient_journey_admin_ok_and_audited(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa", "email": "a@b.com"}
    _use_session(_FakeClinic())
    monkeypatch.setattr("intelligence_report.queries.patient_journey",
                        lambda cid, key, **k: {"client_id": key, "patient": {"surname": "Smith"}})
    audited = {}
    monkeypatch.setattr("api.intelligence.log_phi_access",
                        lambda **kw: audited.update(kw))
    r = client.get("/intelligence/C1/patients/ABC/journey")
    assert r.status_code == 200
    assert r.json()["client_id"] == "ABC"
    assert audited["action"] == "patient_journey"
    assert audited["patient_id"] == "ABC"
    assert audited["outcome"] == "ok"
