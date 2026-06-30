"""
Tests for the scored Active-Leads recovery inbox: clinic-hours interval parsing,
the build_active_leads orchestration (sourcing → dedup → enrichment → auto-resolve
→ scoring → ranking), and the endpoint gate. All BigQuery work is stubbed.
"""
import pytest
from fastapi.testclient import TestClient

from api import app
from api.deps import verify_token
from api.core.db import get_session
from intelligence_report import active_leads as al
from intelligence_report import clinic_hours
from intelligence_report.queries import Window


# ── clinic hours intervals ───────────────────────────────────────────────────

def test_parse_day_intervals_split_day():
    assert clinic_hours.parse_day_intervals("9:00 AM - 12:00 PM, 1:00 PM - 5:00 PM") == [(9.0, 12.0), (13.0, 17.0)]


def test_is_open_at():
    loc = {"hours_monday": "9 - 5", "hours_tuesday": "Closed", "hours_wednesday": None,
           "hours_thursday": None, "hours_friday": None, "hours_saturday": None, "hours_sunday": None}
    assert clinic_hours.is_open_at(loc, 0, 10.0) is True     # Mon 10am
    assert clinic_hours.is_open_at(loc, 0, 18.0) is False    # Mon 6pm
    assert clinic_hours.is_open_at(loc, 1, 10.0) is False    # Tue closed


# ── build_active_leads orchestration ─────────────────────────────────────────

@pytest.fixture
def stub_sources(monkeypatch):
    monkeypatch.setattr(al.q, "qualified_lead_no_conv_detail", lambda *a, **k: [
        {"calling_phone_number": "(613) 555-0001", "start_time_local": "2026-06-01 10:00:00",
         "duration": 120, "connect_duration": 60, "utm_medium": "cpc", "reasoning": "asked about pricing"},
        # C2 has booked since → must auto-resolve out
        {"calling_phone_number": "613-555-0003", "start_time_local": "2026-06-01 11:00:00",
         "duration": 90, "connect_duration": 40, "utm_medium": "cpc", "reasoning": "wanted a test"},
    ])
    monkeypatch.setattr(al.q, "_stage2_outcome_detail", lambda *a, **k: [])
    monkeypatch.setattr(al.q, "open_form_leads", lambda *a, **k: [
        {"first_name": "New", "last_name": "Lead", "phone_number": "613-555-0002",
         "email": "new@x.com", "customer_type": "new patient", "message": "want a hearing test",
         "utm_source": "google", "utm_medium": "cpc", "utm_campaign": "", "landing_page": "/",
         "submitted_at": "2026-06-10 09:00:00", "phone_norm": "6135550002", "email_norm": "new@x.com"},
    ])
    monkeypatch.setattr(al.q, "lead_pms_enrichment", lambda *a, **k: {
        "by_phone": {"6135550001": "C1", "6135550003": "C2"}, "by_email": {},
        "clients": {
            "C1": {"given_name": "Reg", "surname": "Ular", "status": "Active",
                   "do_not_contact": False, "do_not_text": False, "invoice_count": 3,
                   "total_revenue": 9000.0, "avg_invoice": 3000.0,
                   "max_invoice_date": "2026-02-01", "max_appt_date": "2026-02-15"},
            "C2": {"given_name": "Booked", "surname": "Already", "status": "Active",
                   "do_not_contact": False, "do_not_text": False, "invoice_count": 1,
                   "total_revenue": 2000.0, "avg_invoice": 2000.0,
                   "max_invoice_date": None, "max_appt_date": "2026-06-20"},  # after touch → resolved
        },
    })
    monkeypatch.setattr(al.q, "lifecycle_client_ids",
                        lambda *a, **k: {"warranty": set(), "upgrade": set(), "tested_not_sold": set()})
    monkeypatch.setattr(al.q, "invoice_revenue", lambda *a, **k: {"revenue": 30000.0, "invoice_count": 10})


def _build():
    return al.build_active_leads(
        clinic_id="CL", clinic_name="Test", invoca_campaign_ids=["1"],
        window=Window("2026-06-01", "2026-06-26"), location_hours=None)


def test_auto_resolves_booked_since(stub_sources):
    out = _build()
    keys = {l["client_id"] for l in out["leads"]}
    assert "C2" not in keys            # booked after the touch → resolved out
    assert out["lead_count"] == 2      # C1 call + the unmatched form


def test_value_and_ranking(stub_sources):
    out = _build()
    assert out["value_known"] is True
    assert out["base_value"] == 3000.0
    for lead in out["leads"]:
        assert lead["expected_recoverable_revenue"] is not None
        assert lead["band"] in ("hot", "warm", "cooling")
    # Sorted by expected recoverable revenue, descending.
    revs = [l["expected_recoverable_revenue"] for l in out["leads"]]
    assert revs == sorted(revs, reverse=True)
    assert out["total_recoverable"] == pytest.approx(sum(revs))


def test_new_form_lead_is_unmatched_and_valued_at_base(stub_sources):
    out = _build()
    form = next(l for l in out["leads"] if l["source"] == "form")
    assert form["matched"] is False
    assert form["returning"] is False
    assert form["value"] == 3000.0     # base
    assert "new-patient" in form["suggested_action"]


def test_dedup_merges_same_phone(monkeypatch, stub_sources):
    # Same phone via both a call and a form → one merged lead with 2 touches.
    monkeypatch.setattr(al.q, "open_form_leads", lambda *a, **k: [
        {"first_name": "Reg", "last_name": "Ular", "phone_number": "613-555-0001",
         "email": "", "customer_type": "returning", "message": "callback please",
         "utm_source": "google", "utm_medium": "cpc", "utm_campaign": "", "landing_page": "/",
         "submitted_at": "2026-06-12 09:00:00", "phone_norm": "6135550001", "email_norm": ""},
    ])
    out = _build()
    c1 = [l for l in out["leads"] if l["client_id"] == "C1"]
    assert len(c1) == 1
    assert c1[0]["touches"] == 2


# ── endpoint gate ────────────────────────────────────────────────────────────

class _FakeClinic:
    instance_id = "INST_A"
    clinic_name = "Test Clinic"
    deleted_at = None
    location = None


def test_active_leads_endpoint_ok(monkeypatch):
    class _Session:
        def get(self, _m, _i):
            return _FakeClinic()

        def scalars(self, *a, **k):
            return []

    def _session():
        yield _Session()

    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr("intelligence_report.active_leads.build_active_leads",
                        lambda **k: {"lead_count": 0, "leads": [], "total_recoverable": 0.0})
    try:
        c = TestClient(app)
        r = c.get("/intelligence/CL/active-leads?days=90")
        assert r.status_code == 200
        assert r.json()["lead_count"] == 0
    finally:
        app.dependency_overrides.clear()
