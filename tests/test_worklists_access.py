"""
Access-control tests for the client-worklists endpoints (api/worklists.py).

These guard the PHI boundary: the worklists expose patient names / device
serials, so a caller must be a member of the clinic's instance. We assert the
gate (super_admin bypass, viewer-member allow, non-member deny, missing-clinic
404) without hitting BigQuery — the query function is stubbed and membership is
controlled via _is_instance_member.
"""
import pytest
from fastapi.testclient import TestClient

import api.deps as deps
from api import app
from api.deps import verify_token
from api.core.db import get_session


class _FakeClinic:
    def __init__(self, instance_id="INST_A"):
        self.instance_id = instance_id
        self.clinic_name = "Test Clinic"
        self.deleted_at = None


class _FakeSession:
    """Minimal stand-in: .get(Clinic, id) returns a clinic (or None)."""
    def __init__(self, clinic):
        self._clinic = clinic

    def get(self, _model, _id):
        return self._clinic


def _use_session(clinic):
    def _override():
        yield _FakeSession(clinic)
    app.dependency_overrides[get_session] = _override


@pytest.fixture
def client(monkeypatch):
    # No BigQuery: stub the Blueprint query so the endpoint returns a canned row.
    monkeypatch.setattr(
        "intelligence_report.queries.fitting_no_purchase_detail",
        lambda *a, **k: [{"client_id": "1", "surname": "Doe"}],
    )
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


URL = "/clinics/CLINIC_A/worklists/fitting-no-purchase"


def test_super_admin_allowed(client):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeClinic())
    r = client.get(URL)
    assert r.status_code == 200
    assert r.json() == [{"client_id": "1", "surname": "Doe"}]


def test_viewer_member_allowed(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "viewer", "uid": "v1"}
    monkeypatch.setattr(deps, "_is_instance_member", lambda inst, uid: True)
    _use_session(_FakeClinic())
    r = client.get(URL)
    assert r.status_code == 200


def test_viewer_non_member_denied(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "viewer", "uid": "stranger"}
    monkeypatch.setattr(deps, "_is_instance_member", lambda inst, uid: False)
    _use_session(_FakeClinic())
    r = client.get(URL)
    assert r.status_code == 403


def test_missing_clinic_404(client):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(None)
    r = client.get(URL)
    assert r.status_code == 404


# ── New reactivation-segment endpoints: gate + compliance-key contract ───────

_SEGMENT_ENDPOINTS = {
    "intelligence_report.queries.lapsed_patients_detail":
        "/clinics/CLINIC_A/worklists/lapsed-patients",
    "intelligence_report.queries.recall_due_detail":
        "/clinics/CLINIC_A/worklists/recall-due",
    "intelligence_report.queries.upgrade_candidates_detail":
        "/clinics/CLINIC_A/worklists/upgrade-candidates",
}


@pytest.mark.parametrize("fn,url", list(_SEGMENT_ENDPOINTS.items()))
def test_segment_viewer_member_allowed_with_compliance_keys(monkeypatch, fn, url):
    monkeypatch.setattr(fn, lambda *a, **k: [{
        "client_id": "1", "surname": "Doe",
        "do_not_send_commercial_messages": False, "do_not_text": False,
    }])
    app.dependency_overrides[verify_token] = lambda: {"role": "viewer", "uid": "v1"}
    monkeypatch.setattr(deps, "_is_instance_member", lambda inst, uid: True)
    _use_session(_FakeClinic())
    try:
        r = TestClient(app).get(url)
        assert r.status_code == 200
        row = r.json()[0]
        assert "do_not_send_commercial_messages" in row and "do_not_text" in row
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("fn,url", list(_SEGMENT_ENDPOINTS.items()))
def test_segment_non_member_denied(monkeypatch, fn, url):
    monkeypatch.setattr(fn, lambda *a, **k: [])
    app.dependency_overrides[verify_token] = lambda: {"role": "viewer", "uid": "stranger"}
    monkeypatch.setattr(deps, "_is_instance_member", lambda inst, uid: False)
    _use_session(_FakeClinic())
    try:
        assert TestClient(app).get(url).status_code == 403
    finally:
        app.dependency_overrides.clear()
