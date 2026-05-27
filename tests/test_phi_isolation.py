
"""
PHI isolation tests for `POST /blueprint/{clinic_id}/patient/match`.

The voice agent v1 spec makes the `_clinic_id` filter on
Blueprint_PHI.ClientDemographics a non-negotiable PHI isolation requirement:
a patient belonging to clinic A must never be returnable via clinic B's
match endpoint. These tests exist to make that filter a regression-guarded
invariant — if someone removes or weakens the filter, these fail.

We use a fake BigQuery client that captures every SQL call + parameters.
Testing against real `Blueprint_PHI` would require production PHI as fixture
data, which is not acceptable — the structural proof (the filter is always
present and parameterized with the path clinic_id) is the correct guarantee
to enforce at the unit-test layer.
"""
import pytest
from fastapi.testclient import TestClient
from api import app
from api.voice_agent.blueprint import verify_vapi_secret


class _FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class BQCapture:
    """Captures bq_client.query calls; returns a configurable row list."""

    def __init__(self):
        self.calls: list[tuple[str, list[tuple[str, object]]]] = []
        self.rows: list[dict] = []

    def query(self, sql, job_config=None):
        params: list[tuple[str, object]] = []
        if job_config is not None and job_config.query_parameters:
            params = [(p.name, p.value) for p in job_config.query_parameters]
        self.calls.append((sql, params))
        return _FakeJob(self.rows)


@pytest.fixture
def capture(monkeypatch):
    cap = BQCapture()
    # BQ now flows through the BlueprintAdapter — patch the adapter module's
    # bq_client symbol, not the router's. (The router no longer imports
    # bq_client at all after the step-1 PMS-adapter refactor.)
    monkeypatch.setattr("api.voice_agent.pms.blueprint.bq_client", cap)
    app.dependency_overrides[verify_vapi_secret] = lambda: None
    yield cap
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(app)


# ── PHI isolation invariants ───────────────────────────────────────────────────


def test_sql_always_includes_clinic_id_filter(capture, client):
    """The query MUST filter by _clinic_id. Removing this filter = PHI leak."""
    resp = client.post(
        "/blueprint/CLINIC_A/patient/match",
        json={"first_name": "Alice", "last_name": "Smith", "last4_phone": "1234"},
    )
    assert resp.status_code == 200, resp.text
    assert len(capture.calls) == 1
    sql, params = capture.calls[0]
    assert "_clinic_id = @clinic_id" in sql
    assert ("clinic_id", "CLINIC_A") in params


def test_clinic_id_comes_from_path_not_body(capture, client):
    """The clinic_id parameter must come from the URL path, not any request-body field."""
    resp = client.post(
        "/blueprint/PATH_CLINIC/patient/match",
        json={
            "first_name": "Alice",
            "last_name": "Smith",
            "last4_phone": "1234",
            # A hostile caller cannot override the clinic_id via the body.
            "clinic_id": "OTHER_CLINIC",
            "_clinic_id": "OTHER_CLINIC",
        },
    )
    assert resp.status_code == 200, resp.text
    _, params = capture.calls[0]
    values = [v for (n, v) in params if n == "clinic_id"]
    assert values == ["PATH_CLINIC"]


def test_cross_clinic_lookup_returns_unmatched(capture, client):
    """
    If clinic B's patient set contains no matching patient (which is what the
    _clinic_id filter ensures when the match actually belongs to clinic A),
    the endpoint must return 'unmatched' — never leak anything.
    """
    capture.rows = []  # simulates the BQ filter returning empty for this clinic
    resp = client.post(
        "/blueprint/CLINIC_B/patient/match",
        json={"first_name": "Alice", "last_name": "Smith", "last4_phone": "1234"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "unmatched", "patient_id": None, "candidates_count": 0}


# ── Status mapping ─────────────────────────────────────────────────────────────


def test_one_row_returns_matched_with_patient_id(capture, client):
    capture.rows = [{"client_id": "99999"}]
    resp = client.post(
        "/blueprint/X/patient/match",
        json={"first_name": "Alice", "last_name": "Smith", "last4_phone": "1234"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "matched",
        "patient_id": "99999",
        "candidates_count": 1,
    }


def test_multiple_rows_returns_ambiguous_and_suppresses_patient_id(capture, client):
    """
    When >1 candidates match, patient_id must be null — returning any one of
    them would be a guess that could expose the wrong patient's data.
    """
    capture.rows = [{"client_id": "1"}, {"client_id": "2"}]
    resp = client.post(
        "/blueprint/X/patient/match",
        json={"first_name": "Alice", "last_name": "Smith", "last4_phone": "1234"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ambiguous", "patient_id": None, "candidates_count": 2}


# ── Input validation & DOB tie-breaker ─────────────────────────────────────────


def test_non_four_digit_last4_is_rejected(capture, client):
    resp = client.post(
        "/blueprint/X/patient/match",
        json={"first_name": "A", "last_name": "B", "last4_phone": "abc"},
    )
    assert resp.status_code == 400
    # Must not have issued any BQ query on invalid input
    assert capture.calls == []


def test_dob_adds_tiebreaker_clause(capture, client):
    resp = client.post(
        "/blueprint/X/patient/match",
        json={
            "first_name": "A",
            "last_name": "B",
            "last4_phone": "1234",
            "dob": "1990-01-01",
        },
    )
    assert resp.status_code == 200
    sql, params = capture.calls[0]
    assert "birthdate = @dob" in sql
    assert ("dob", "1990-01-01") in params


def test_dob_omitted_does_not_add_tiebreaker(capture, client):
    resp = client.post(
        "/blueprint/X/patient/match",
        json={"first_name": "A", "last_name": "B", "last4_phone": "1234"},
    )
    assert resp.status_code == 200
    sql, params = capture.calls[0]
    assert "birthdate = @dob" not in sql
    assert all(n != "dob" for (n, _) in params)


def test_name_match_is_case_insensitive_in_sql(capture, client):
    resp = client.post(
        "/blueprint/X/patient/match",
        json={"first_name": "Alice", "last_name": "Smith", "last4_phone": "1234"},
    )
    assert resp.status_code == 200
    sql, _ = capture.calls[0]
    assert "LOWER(given_name) = LOWER(@first_name)" in sql
    assert "LOWER(surname) = LOWER(@last_name)" in sql


def test_last4_matches_any_phone_field(capture, client):
    """The SQL must check last4 against all three phone fields (mobile/home/work)."""
    resp = client.post(
        "/blueprint/X/patient/match",
        json={"first_name": "A", "last_name": "B", "last4_phone": "1234"},
    )
    assert resp.status_code == 200
    sql, _ = capture.calls[0]
    assert "mobile_telephone_no" in sql
    assert "home_telephone_no" in sql
    assert "work_telephone_no" in sql