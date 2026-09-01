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
    # A window ending before MIN_WINDOW_DATE (2025-12-04) floors to None.
    assert Window("2024-01-01", "2024-12-31").floored() is None


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
    # Per-source split (§14): 2 observed web submissions + 1 CounselEar-embed
    # booking. Top-level fields stay the sum, so `contacts` is unchanged at 8.
    monkeypatch.setattr(q, "form_capture",
                        lambda *a, **k: {
                            "submissions": 3, "form_bookings": 1, "form_rate": 0.33,
                            "web":    {"submissions": 2, "form_bookings": 1, "form_rate": 0.5},
                            "portal": {"submissions": 1, "form_bookings": 0, "form_rate": 0.0},
                        })
    monkeypatch.setattr(q, "invoice_revenue",
                        lambda *a, **k: {"revenue": 1000.0, "invoice_count": 4})
    out = headline_yoy("C1", ["1"], window=Window("2026-06-01", "2026-06-30"))
    assert out["basis"] == "mom"
    assert out["prior_window"]["end"] == "2026-05-31"   # the previous month
    assert out["current"]["contacts"] == 8
    # The split rides alongside the sum rather than replacing it.
    assert out["current"]["forms"] == 3
    assert out["current"]["forms_web"] == 2
    assert out["current"]["forms_portal"] == 1
    # form_rate_web is the web-only booking rate, NOT the blended figure.
    assert out["current"]["form_rate_web"] == 0.5


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


# ── PMS coverage disclosure ──────────────────────────────────────────────────
#
# A clinic with no PMS integration gets ZEROS from every PMS-derived reader (by
# design — a missing integration must never blank a page), so the payload has to
# distinguish "measured zero" from "not measurable" or the report reads as a
# clinic that booked nothing and earned nothing.

class _StubQueries:
    """Every reader returns a benign empty result, so the builders run without
    BigQuery — the same stub shape test_group_intelligence uses."""
    @staticmethod
    def _empty_dict(*a, **k):
        return {}

    @staticmethod
    def _empty_list(*a, **k):
        return []

    def __getattr__(self, name):
        if name.endswith(("_by_month", "_monthly", "_trend", "_roi", "_mix")):
            return self._empty_list
        return self._empty_dict


@pytest.fixture
def stub_queries(monkeypatch):
    monkeypatch.setattr(payloads, "q", _StubQueries())


def _overview(pms_type, **kw):
    return payloads.build_overview(
        clinic_id="C1", clinic_name="Test",
        invoca_campaign_ids=["1"], ga_campaign_ids=["2"],
        window=Window("2026-01-01", "2026-05-31"),
        pms_type=pms_type, with_recommendations=False, **kw)


@pytest.mark.parametrize("pms_type", ["blueprint", "counselear"])
def test_overview_marks_pms_backed_clinics_integrated(pms_type, stub_queries):
    payload = _overview(pms_type)
    assert payload["pms_type"] == pms_type
    assert payload["pms_integrated"] is True
    assert payload["pms_caveat"] is None


@pytest.mark.parametrize("pms_type", ["none", "audit_data"])
def test_overview_discloses_missing_pms_integration(pms_type, stub_queries):
    """``audit_data`` counts as missing until that feed actually lands in
    PMS_Unified — the report can only claim what it can read."""
    payload = _overview(pms_type)
    assert payload["pms_integrated"] is False
    assert "revenue" in payload["pms_caveat"]
    assert "not zero" in payload["pms_caveat"]


def test_no_pms_caveat_reaches_the_llm_copy(monkeypatch, stub_queries):
    """Both LLM writers must be told, or they narrate the zeros as a collapse."""
    seen: list[str] = []
    monkeypatch.setattr(payloads, "_one_thing_sentence",
                        lambda name, payload: seen.append(payload["pms_caveat"]))
    monkeypatch.setattr(payloads, "forward_recommendations",
                        lambda name, metrics, caveat=None: seen.append(caveat) or [])
    payloads.build_overview(
        clinic_id="C1", clinic_name="Test", invoca_campaign_ids=["1"],
        ga_campaign_ids=["2"], window=Window("2026-01-01", "2026-05-31"),
        pms_type="none", with_recommendations=True)
    assert seen and all(c and "PMS" in c for c in seen)


def test_biweekly_carries_the_same_flag(stub_queries):
    payload = payloads.build_biweekly(
        clinic_id="C1", clinic_name="Test", invoca_campaign_ids=["1"],
        window=Window("2026-01-01", "2026-01-14"), pms_type="none")
    assert payload["pms_integrated"] is False
    payload = payloads.build_biweekly(
        clinic_id="C1", clinic_name="Test", invoca_campaign_ids=["1"],
        window=Window("2026-01-01", "2026-01-14"), pms_type="counselear")
    assert payload["pms_integrated"] is True


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
        # `scalar` answers the clinic-count query behind the overview payload's
        # `group_intelligence` field (api/core/grouping.py). 1 = single location.
        yield type("S", (), {"get": lambda self, m, i: clinic,
                             "scalar": lambda self, *a, **k: 1,
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


def _isolate_cache(monkeypatch):
    """Unit tests must not touch the shared GCS cache tier: objects written by a
    previous run would leak in as hits, making results depend on run order and
    on real cloud state. Also clears the in-process tier."""
    from api import intelligence as _I
    monkeypatch.setattr(_I, "_SHARED_CACHE_ENABLED", False)
    _I._json_cache.clear()
    _I._data_version_cache.clear()
    monkeypatch.setattr(_I, "_data_version", lambda *_a, **_k: "test-version")


def test_overview_skip_llm_disables_recommendations_and_bypasses_cache(client, monkeypatch):
    """?skip_llm=1 (parity-harness param) must pass with_recommendations=False
    and never read from or write to the JSON cache — a copy-less payload must
    not be served to (or poison the cache for) real users."""
    _isolate_cache(monkeypatch)
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeClinic())
    calls = []

    def _fake_build(**k):
        calls.append(k)
        return {"clinic_id": "C_SKIP", "with_recs": k["with_recommendations"]}

    monkeypatch.setattr("intelligence_report.payloads.build_overview", _fake_build)

    # 1. skip_llm request: builder invoked with with_recommendations=False.
    r = client.get("/intelligence/C_SKIP/overview?days=30&skip_llm=1")
    assert r.status_code == 200
    assert calls[-1]["with_recommendations"] is False

    # 2. Its result must NOT have been cached: a normal request rebuilds
    #    (with_recommendations=True) instead of serving the copy-less payload.
    r = client.get("/intelligence/C_SKIP/overview?days=30")
    assert r.status_code == 200
    assert len(calls) == 2
    assert calls[-1]["with_recommendations"] is True

    # 3. The normal result IS cached; a skip_llm request must not read it
    #    (cache bypassed in both directions → builder runs a third time).
    r = client.get("/intelligence/C_SKIP/overview?days=30&skip_llm=1")
    assert r.status_code == 200
    assert len(calls) == 3
    assert calls[-1]["with_recommendations"] is False


def test_overview_default_has_no_skip(client, monkeypatch):
    """Without ?skip_llm, the endpoint passes with_recommendations=True —
    the declared 'zero behavior change unless passed' guarantee."""
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeClinic())
    seen = {}
    monkeypatch.setattr("intelligence_report.payloads.build_overview",
                        lambda **k: (seen.update(k), {"clinic_id": "C_DEFAULT"})[1])
    r = client.get("/intelligence/C_DEFAULT/overview?days=30&nocache=1")
    assert r.status_code == 200
    assert seen["with_recommendations"] is True
