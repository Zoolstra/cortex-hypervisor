"""
Tests for the per-clinic worklist cohort taxonomy feature.

Three layers, none hitting BigQuery or Cloud SQL:
  1. WorklistTaxonomyConfig / WorklistCohort Pydantic validation.
  2. cohort_detail() query wiring — a fake BQ client captures the SQL + bound
     params so we assert the event/status/ha filters are built correctly, and
     that the back-compat wrapper delegates with the default fitting cohort.
  3. Endpoint gates — cohort GET allows viewer-members; export.csv is
     super_admin/admin only, filters opt-outs, and audit-logs.
"""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api.deps as deps
from api import app
from api.deps import verify_token
from api.core.db import get_session
from api.account.worklist_taxonomy import (
    WorklistCohort, WorklistTaxonomyConfig, _DEFAULT_TAXONOMY, resolve_taxonomy,
)
from intelligence_report import queries


# ── 1. Config validation ─────────────────────────────────────────────────────

def _cohort(**over):
    base = dict(key="tested_not_sold", label="Tested — not sold",
                event_types=["Test - New"], statuses=["Completed"], require_no_sale=True)
    base.update(over)
    return base


def test_valid_config():
    cfg = WorklistTaxonomyConfig(ha_item_types=["ha"], cohorts=[_cohort()])
    assert cfg.cohort("tested_not_sold").event_types == ["Test - New"]
    assert cfg.cohort("missing") is None


@pytest.mark.parametrize("cohorts,ha,why", [
    ([_cohort(), _cohort()], ["ha"], "duplicate keys"),
    ([_cohort(event_types=[], event_like=None)], ["ha"], "no event source"),
    ([_cohort(event_types=["x"], event_like="%fit%")], ["ha"], "both event sources"),
    ([_cohort(statuses=[])], ["ha"], "no statuses"),
    ([_cohort(require_no_sale=True)], [], "require_no_sale without ha_item_types"),
    ([_cohort(key="Bad Key")], ["ha"], "non-slug key"),
])
def test_invalid_config_rejected(cohorts, ha, why):
    with pytest.raises(ValidationError):
        WorklistTaxonomyConfig(ha_item_types=ha, cohorts=cohorts)


def test_extra_field_forbidden():
    with pytest.raises(ValidationError):
        WorklistCohort(**_cohort(), bogus=1)


def test_default_reproduces_legacy_behavior():
    d = _DEFAULT_TAXONOMY
    assert d.ha_item_types == ["ha", "hao"]
    c = d.cohort("fitted_not_sold")
    assert c.event_like == "%fit%" and c.event_types == []
    assert c.statuses == ["Completed", "Arrived"] and c.require_no_sale is True


# ── 2. cohort_detail query wiring (fake BQ client) ────────────────────────────

class _FakeJob:
    def result(self):
        return []


class _RecClient:
    def __init__(self):
        self.sql = None
        self.params = None

    def query(self, sql, job_config=None):
        self.sql = sql
        self.params = {p.name: p for p in (job_config.query_parameters if job_config else [])}
        return _FakeJob()


@pytest.fixture
def rec(monkeypatch):
    client = _RecClient()
    monkeypatch.setattr(queries, "_client", lambda: client)
    return client


def test_cohort_detail_event_types_and_sale_exclusion(rec):
    queries.cohort_detail(
        "C1", event_types=["Test - New", "Test - Annual"],
        statuses=["Completed", "Arrived"], require_no_sale=True, ha_item_types=["ha"],
    )
    assert "IN UNNEST(@event_types)" in rec.sql
    assert "IN UNNEST(@statuses)" in rec.sql
    assert "ha_clients" in rec.sql and "IN UNNEST(@ha_item_types)" in rec.sql
    assert rec.params["event_types"].values == ["test - new", "test - annual"]  # lowercased
    assert rec.params["ha_item_types"].values == ["ha"]
    assert "email_address" not in rec.sql  # no contact columns by default


def test_cohort_detail_no_show_skips_sale_exclusion(rec):
    queries.cohort_detail(
        "C1", event_types=["Test - New"], statuses=["No show"], require_no_sale=False,
    )
    assert "ha_clients" not in rec.sql
    assert "ha_item_types" not in rec.params


def test_cohort_detail_event_like_and_contact(rec):
    queries.cohort_detail(
        "C1", event_like="%fit%", statuses=["Completed"], require_no_sale=True,
        ha_item_types=["ha", "hao"], include_contact=True,
    )
    assert "LIKE @event_like" in rec.sql
    assert rec.params["event_like"].value == "%fit%"
    assert "email_address" in rec.sql  # contact columns present for export


@pytest.mark.parametrize("kwargs", [
    dict(),  # neither event source
    dict(event_types=["x"], event_like="%y%"),  # both
    dict(event_types=["x"], statuses=[]),  # no statuses
    dict(event_types=["x"], require_no_sale=True, ha_item_types=[]),  # missing ha
])
def test_cohort_detail_bad_args(rec, kwargs):
    with pytest.raises(ValueError):
        queries.cohort_detail("C1", **kwargs)


def test_fitting_wrapper_delegates_to_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(queries, "cohort_detail",
                        lambda cid, **k: seen.update(k) or [])
    queries.fitting_no_purchase_detail("C1")
    assert seen["event_like"] == "%fit%"
    assert tuple(seen["statuses"]) == ("Completed", "Arrived")
    assert seen["require_no_sale"] is True
    assert tuple(seen["ha_item_types"]) == ("ha", "hao")


# ── resolve_taxonomy ──────────────────────────────────────────────────────────

class _Row:
    def __init__(self, config):
        self.config = config


class _ClinicStub:
    def __init__(self, taxonomy_config=None):
        self.clinic_id = "C1"
        self.instance_id = "INST_A"
        self.clinic_name = "Test Clinic"
        self.deleted_at = None
        self.worklist_taxonomy = _Row(taxonomy_config) if taxonomy_config else None


def test_resolve_taxonomy_falls_back_to_default():
    assert resolve_taxonomy(_ClinicStub()) is _DEFAULT_TAXONOMY


def test_resolve_taxonomy_uses_clinic_config():
    cfg = {"ha_item_types": ["ha"], "cohorts": [_cohort()]}
    resolved = resolve_taxonomy(_ClinicStub(cfg))
    assert [c.key for c in resolved.cohorts] == ["tested_not_sold"]


# ── 3. Endpoint gates ─────────────────────────────────────────────────────────

class _FakeSession:
    def __init__(self, clinic):
        self._clinic = clinic

    def get(self, _model, _id):
        return self._clinic


def _use_session(clinic):
    def _override():
        yield _FakeSession(clinic)
    app.dependency_overrides[get_session] = _override


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_cohort_get_viewer_member_allowed(monkeypatch):
    monkeypatch.setattr(queries, "cohort_detail",
                        lambda *a, **k: [{"client_id": "1", "surname": "Doe"}])
    app.dependency_overrides[verify_token] = lambda: {"role": "viewer", "uid": "v1"}
    monkeypatch.setattr(deps, "_is_instance_member", lambda i, u: True)
    _use_session(_ClinicStub())  # default taxonomy → fitted_not_sold cohort
    r = TestClient(app).get("/clinics/C1/worklists/cohort/fitted_not_sold")
    assert r.status_code == 200 and r.json()[0]["surname"] == "Doe"


def test_cohort_get_unknown_key_404(monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_ClinicStub())
    r = TestClient(app).get("/clinics/C1/worklists/cohort/does_not_exist")
    assert r.status_code == 404


def test_export_viewer_denied(monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "viewer", "uid": "v1"}
    monkeypatch.setattr(deps, "_is_instance_member", lambda i, u: True)
    _use_session(_ClinicStub())
    r = TestClient(app).get("/clinics/C1/worklists/cohort/fitted_not_sold/export.csv")
    assert r.status_code == 403


def test_export_super_admin_csv_filters_optouts(monkeypatch):
    rows = [
        {"client_id": "1", "given_name": "A", "surname": "Keep",
         "appt_start_time": "2026-01-01", "appt_event_type": "Fit", "appt_status": "Completed",
         "patient_status": "Active", "do_not_send_commercial_messages": False,
         "do_not_text": False, "email": "a@x.com", "primary_phone": "5550001",
         "mobile_phone": "5550001", "home_phone": "", "work_phone": "", "do_not_email": False},
        {"client_id": "2", "given_name": "B", "surname": "Drop",
         "appt_start_time": "2026-01-02", "appt_event_type": "Fit", "appt_status": "Completed",
         "patient_status": "Active", "do_not_send_commercial_messages": True,  # opt-out
         "do_not_text": False, "email": "b@x.com", "primary_phone": "5550002",
         "mobile_phone": "5550002", "home_phone": "", "work_phone": "", "do_not_email": False},
    ]
    monkeypatch.setattr(queries, "cohort_detail", lambda *a, **k: rows)
    logged = {}
    monkeypatch.setattr("api.audit.log_phi_access",
                        lambda **kw: logged.update(kw))
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa",
                                                       "email": "sa@zoolstra.com"}
    _use_session(_ClinicStub())
    r = TestClient(app).get("/clinics/C1/worklists/cohort/fitted_not_sold/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    body = r.text
    assert "Keep" in body and "Drop" not in body   # opt-out filtered
    assert "a@x.com" in body                        # contact included
    assert logged["action"] == "worklist_export" and "rows=1" in logged["detail"]
