"""
Tests for the Customer.io reactivation sync (api/worklists.py customerio-sync).

Covers the properties the pilot depends on, without BigQuery, Cloud SQL, or
Customer.io: auth (scheduler secret vs Firebase admin vs outsiders), dry-run
harmlessness (no sends, no log rows), send-once idempotency (already-enrolled
skipped), the consent gate (fully-opted-out patients never reach the client),
and no-contact handling.
"""
import pytest
from fastapi.testclient import TestClient

from api import app
from api.core.db import get_session
from api.services import customerio as cio


class _FakeClinic:
    def __init__(self, instance_id="INST_A"):
        self.instance_id = instance_id
        self.clinic_name = "Alto Hearing"
        self.deleted_at = None


class _FakeScalars(list):
    pass


class _FakeSession:
    def __init__(self, clinic, enrolled=()):
        self._clinic = clinic
        self._enrolled = list(enrolled)
        self.added = []
        self.committed = False

    def get(self, _model, _id):
        return self._clinic

    def scalars(self, _stmt):
        return _FakeScalars(self._enrolled)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True


def _row(client_id, *, email="p@example.com", dnc=False, dnt=False, dne=False,
         mobile="555-000-1111"):
    return {
        "client_id": client_id, "given_name": "Pat", "surname": "Tested",
        "email": email, "primary_phone": "", "mobile_phone": mobile,
        "home_phone": "", "work_phone": "",
        "appt_start_time": "2026-05-01 10:00", "appt_event_type": "Test - New",
        "appt_status": "Completed", "patient_status": "Active",
        "do_not_send_commercial_messages": dnc, "do_not_text": dnt,
        "do_not_email": dne,
    }


URL = "/clinics/CLINIC_1/worklists/cohort/tested-not-sold/customerio-sync"


@pytest.fixture
def harness(monkeypatch):
    """TestClient + fake session + stubbed cohort rows / secret / CIO client."""
    state = {"rows": [], "session": _FakeSession(_FakeClinic()),
             "identified": [], "tracked": []}

    def _override():
        yield state["session"]
    app.dependency_overrides[get_session] = _override

    monkeypatch.setattr(
        "api.worklists._run_cohort", lambda *a, **k: state["rows"])
    monkeypatch.setattr(
        "api.worklists.get_secret",
        lambda name: "sync-secret-123" if name == "customerio-sync-secret" else "")
    monkeypatch.setattr("api.worklists.audit.log_phi_access",
                        lambda **k: None)

    class _FakeCIO:
        def __init__(self, clinic_id, **k):
            state["client_clinic"] = clinic_id

        def identify(self, person_id, attrs):
            state["identified"].append((person_id, attrs))

        def track(self, person_id, event, data=None):
            state["tracked"].append((person_id, event, data))

    monkeypatch.setattr(cio, "CustomerIOClient", _FakeCIO)

    yield TestClient(app), state
    app.dependency_overrides.clear()


def _post(client, url=URL, secret="sync-secret-123", **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return client.post(f"{url}?{q}" if q else url,
                       headers={"X-CIO-Sync-Secret": secret} if secret else {})


def test_no_auth_rejected(harness):
    client, _ = harness
    assert _post(client, secret=None).status_code == 401


def test_bad_secret_rejected(harness):
    client, _ = harness
    assert _post(client, secret="wrong").status_code == 401


def test_dry_run_default_sends_and_writes_nothing(harness):
    client, state = harness
    state["rows"] = [_row("1"), _row("2")]
    resp = _post(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["sent"] == 2
    assert state["identified"] == [] and state["tracked"] == []
    assert state["session"].added == []
    assert state["session"].committed is False


def test_live_run_enrolls_and_logs(harness):
    client, state = harness
    state["rows"] = [_row("1")]
    body = _post(client, dry_run="false").json()
    assert body["sent"] == 1
    assert state["identified"][0][0] == "CLINIC_1:1"
    person, event, data = state["tracked"][0]
    assert person == "CLINIC_1:1"
    assert event == "tested_not_sold_lead"  # derived from cohort key
    assert data["cohort"] == "tested-not-sold"
    assert len(state["session"].added) == 1
    assert state["session"].added[0].status == "sent"
    assert state["session"].committed is True


def test_already_enrolled_skipped(harness):
    client, state = harness
    state["rows"] = [_row("1"), _row("2")]
    state["session"] = _FakeSession(_FakeClinic(), enrolled=["1"])
    body = _post(client, dry_run="false").json()
    assert body["already_enrolled"] == 1
    assert body["sent"] == 1
    assert [p for p, _ in state["identified"]] == ["CLINIC_1:2"]


def test_fully_opted_out_never_reaches_customerio(harness):
    client, state = harness
    state["rows"] = [_row("1", dnc=True)]
    body = _post(client, dry_run="false").json()
    assert body["blocked_consent"] == 1 and body["sent"] == 0
    assert state["identified"] == [] and state["tracked"] == []
    assert state["session"].added[0].status == "blocked_consent"


def test_email_only_optout_still_enrolls_with_flags(harness):
    client, state = harness
    state["rows"] = [_row("1", dne=True)]
    body = _post(client, dry_run="false").json()
    assert body["sent"] == 1
    _, attrs = state["identified"][0]
    assert attrs["do_not_email"] is True and attrs["do_not_text"] is False


def test_no_contact_logged_not_sent(harness):
    client, state = harness
    state["rows"] = [_row("1", email="", mobile="")]
    body = _post(client, dry_run="false").json()
    assert body["no_contact"] == 1 and body["sent"] == 0
    assert state["identified"] == []
    assert state["session"].added[0].status == "no_contact"


def test_custom_event_name_wins(harness):
    client, state = harness
    state["rows"] = [_row("1")]
    _post(client, dry_run="false", event_name="alto_winback")
    assert state["tracked"][0][1] == "alto_winback"


def test_client_built_for_the_clinics_workspace(harness):
    client, state = harness
    state["rows"] = [_row("1")]
    _post(client, dry_run="false")
    assert state["client_clinic"] == "CLINIC_1"  # per-clinic CIO workspace


def test_unconfigured_clinic_workspace_409(harness, monkeypatch):
    client, state = harness
    state["rows"] = [_row("1")]

    def _boom(clinic_id, **k):
        raise cio.CustomerIONotConfigured(f"no secrets for {clinic_id}")
    monkeypatch.setattr(cio, "CustomerIOClient", _boom)
    resp = _post(client, dry_run="false")
    assert resp.status_code == 409
    assert state["session"].committed is False
