"""
Tests for the multi-location "Group Intelligence" surface (Virsono).

Covers:
  * group_queries helpers (booked-status mapping) + zoolstra fail-safe.
  * payloads.build_group_overview roll-up math (totals, leaderboards, rollups,
    coverage) with BigQuery stubbed.
  * Endpoint wiring: the multi_location_group capability-flag gate (404 when off,
    200 when on) and read-access enforcement.

All BigQuery / LLM work is stubbed — no external calls.
"""
import pytest
from fastapi.testclient import TestClient

from api import app
from api.deps import verify_token
from api.core.db import get_session
from intelligence_report import group_queries as gq
from intelligence_report import payloads
from intelligence_report.queries import Window


# ── group_queries helpers ────────────────────────────────────────────────────

def test_booked_from_status_counts_only_kept_visits():
    by_status = {"Completed": 5, "Arrived": 2, "Cancelled": 9, "No Show": 4, "Unknown": 1}
    assert gq._booked_from_status(by_status) == 7          # Completed + Arrived only


def test_booked_from_status_is_case_insensitive_and_empty_safe():
    assert gq._booked_from_status({"completed": 3, "ARRIVED": 1}) == 4
    assert gq._booked_from_status({}) == 0
    assert gq._booked_from_status(None) == 0


def test_zoolstra_attribution_empty_clinics_returns_zero_shape():
    out = gq.zoolstra_attribution([], window=Window("2026-01-01", "2026-06-30"))
    assert out["per_location"] == []
    assert out["totals"]["bookings"] == 0
    assert out["totals"]["conversion_rate"] is None


def test_zoolstra_attribution_failsafe_on_query_error(monkeypatch):
    # A missing dataset / transient error must yield zeros, never raise.
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("dataset not found")
    monkeypatch.setattr(gq, "_client", lambda: _Boom())
    out = gq.zoolstra_attribution(["C1"], window=Window("2026-01-01", "2026-06-30"))
    assert out["per_location"] == []
    assert out["totals"]["revenue"] == 0.0


# ── payloads roll-up helpers ─────────────────────────────────────────────────

def test_leaderboard_orders_desc_and_pushes_no_data_last():
    locs = [
        {"clinic_id": "A", "has_pms_data": True, "revenue": 100.0},
        {"clinic_id": "B", "has_pms_data": True, "revenue": 300.0},
        {"clinic_id": "C", "has_pms_data": False, "revenue": 0.0},
        {"clinic_id": "D", "has_pms_data": True, "revenue": 200.0},
    ]
    assert payloads._leaderboard(locs, "revenue") == ["B", "D", "A", "C"]


def test_leaderboard_none_metric_sorts_last():
    locs = [
        {"clinic_id": "A", "has_pms_data": True, "avg_invoice": None},
        {"clinic_id": "B", "has_pms_data": True, "avg_invoice": 50.0},
    ]
    assert payloads._leaderboard(locs, "avg_invoice") == ["B", "A"]


def test_rollup_merges_and_sorts():
    locs = [
        {"product_mix": [{"item_type": "Hearing Aid", "revenue": 100.0, "line_count": 2},
                         {"item_type": "Accessory", "revenue": 10.0, "line_count": 1}]},
        {"product_mix": [{"item_type": "Hearing Aid", "revenue": 400.0, "line_count": 3}]},
    ]
    out = payloads._rollup(locs, "product_mix", "item_type", ("revenue", "line_count"))
    assert out[0] == {"item_type": "Hearing Aid", "revenue": 500.0, "line_count": 5}
    assert out[1]["item_type"] == "Accessory"


# ── build_group_overview ─────────────────────────────────────────────────────

def _fake_location(cid, revenue, invoices, booked, has=True):
    return {
        "clinic_id": cid, "clinic_name": f"Clinic {cid}", "pms_type": "counselear",
        "has_pms_data": has, "snapshot_date": "2026-06-29" if has else None,
        "appointments": {"total": booked + 5, "by_status": {}, "sales_opportunities": 1},
        "booked_appts": booked, "revenue": revenue, "invoice_count": invoices,
        "avg_invoice": (revenue / invoices) if invoices else None,
        "product_mix": [{"item_type": "Hearing Aid", "revenue": revenue, "line_count": invoices}],
        "referrals": [{"source_name": "Word of Mouth", "invoice_count": invoices, "revenue": revenue}],
        "webform_submissions": 3, "webform_attributed_revenue": 500.0, "google_ads": [],
    }


def test_build_group_overview_rollup(monkeypatch):
    clinics = [("A", "Clinic A", "counselear"), ("B", "Clinic B", "counselear"),
               ("C", "Clinic C", "none")]
    locations = [
        _fake_location("A", 1000.0, 4, 6),
        _fake_location("B", 3000.0, 6, 9),
        _fake_location("C", 0.0, 0, 0, has=False),
    ]
    monkeypatch.setattr(gq, "group_comparison", lambda *a, **k: locations)
    monkeypatch.setattr(gq, "appointment_referral_breakdown", lambda *a, **k: [
        {"source": "Referral - Zoolstra", "bookings": 7, "patients": 6},
        {"source": "Online", "bookings": 3, "patients": 3},
    ])
    monkeypatch.setattr(gq, "zoolstra_attribution", lambda *a, **k: {
        "per_location": [
            {"clinic_id": "B", "bookings": 5, "patients": 4, "invoiced_patients": 2,
             "invoice_count": 2, "revenue": 1200.0, "conversion_rate": 0.5,
             "revenue_per_patient": 300.0},
            {"clinic_id": "A", "bookings": 2, "patients": 2, "invoiced_patients": 1,
             "invoice_count": 1, "revenue": 400.0, "conversion_rate": 0.5,
             "revenue_per_patient": 200.0},
        ],
        "totals": {"bookings": 7, "patients": 6, "invoiced_patients": 3,
                   "invoice_count": 3, "revenue": 1600.0, "conversion_rate": 0.5},
    })

    payload = payloads.build_group_overview(
        instance_id="INST", instance_name="Virsono",
        clinics=clinics, ga_campaign_ids_by_clinic={},
        window=Window("2026-01-01", "2026-06-30"),
        with_recommendations=False,
    )

    assert payload["totals"]["revenue"] == 4000.0
    assert payload["totals"]["invoice_count"] == 10
    assert payload["totals"]["booked_appts"] == 15
    # leaderboards: B leads revenue + booked; C (no PMS) is last.
    assert payload["leaderboards"]["by_revenue"] == ["B", "A", "C"]
    assert payload["leaderboards"]["by_booked_appts"][0] == "B"
    assert payload["leaderboards"]["by_revenue"][-1] == "C"
    assert payload["leaderboards"]["by_zoolstra_revenue"] == ["B", "A"]
    # rollups merged across clinics.
    pm = payload["product_mix_rollup"]
    assert pm[0]["item_type"] == "Hearing Aid" and pm[0]["revenue"] == 4000.0
    # coverage reflects the missing clinic.
    assert payload["coverage"] == {"clinics_total": 3, "clinics_with_pms": 2,
                                    "clinics_missing": ["C"]}
    assert payload["zoolstra_attribution"]["totals"]["revenue"] == 1600.0
    assert payload["appt_referral_rollup"][0]["source"] == "Referral - Zoolstra"
    assert payload["recommendations"] == []


# ── Endpoint: capability-flag gate ───────────────────────────────────────────

class _FakeInstance:
    def __init__(self, flag):
        self.instance_id = "INST"
        self.instance_name = "Virsono"
        self.multi_location_group = flag


class _FakeResult:
    def all(self):
        return []


def _use_session(instance):
    def _override():
        yield type("S", (), {
            "get": lambda self, m, i: instance,
            "execute": lambda self, *a, **k: _FakeResult(),
            "scalars": lambda self, *a, **k: [],
        })()
    app.dependency_overrides[get_session] = _override


@pytest.fixture
def client():
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def test_group_overview_flag_off_404(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeInstance(flag=False))
    r = client.get("/intelligence/group/INST/overview?days=30")
    assert r.status_code == 404


def test_group_overview_missing_instance_404(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(None)
    r = client.get("/intelligence/group/NOPE/overview?days=30")
    assert r.status_code == 404


def test_group_overview_flag_on_ok(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeInstance(flag=True))
    monkeypatch.setattr("intelligence_report.payloads.build_group_overview",
                        lambda **k: {"instance_id": k["instance_id"], "ok": True})
    r = client.get("/intelligence/group/INST/overview?days=30")
    assert r.status_code == 200
    assert r.json() == {"instance_id": "INST", "ok": True}


def test_group_overview_bad_date_range_422(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeInstance(flag=True))
    monkeypatch.setattr("intelligence_report.payloads.build_group_overview", lambda **k: {"ok": True})
    r = client.get("/intelligence/group/INST/overview?start=2026-06-10&end=2026-06-01")
    assert r.status_code == 422
