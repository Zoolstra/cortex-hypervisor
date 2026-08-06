"""
Tests for the per-clinic Customer.io workspace config (api/account/customerio_config.py).

Secret Manager is stubbed; asserts the properties the dashboard relies on:
status never leaks values, saves are partial (rotate one credential at a
time), empty saves 400, delete disables, and viewers can't write.
"""
import pytest
from fastapi.testclient import TestClient

from api import app
from api.account import customerio_config as mod
from api.core.db import get_session
from api.deps import verify_token


class _FakeClinic:
    def __init__(self):
        self.instance_id = "INST_A"
        self.clinic_name = "Alto Hearing"
        self.deleted_at = None


class _FakeSession:
    def __init__(self, clinic=None):
        self._clinic = clinic

    def get(self, _model, _id):
        return self._clinic


URL = "/clinics/CLINIC_1/customerio"


@pytest.fixture
def harness(monkeypatch):
    state = {"secrets": {}, "written": [], "deleted": []}

    def _get_secret(name, version="latest"):
        if name in state["secrets"]:
            return state["secrets"][name]
        raise RuntimeError("not found")
    _get_secret.cache_clear = lambda: None

    monkeypatch.setattr(mod, "get_secret", _get_secret)
    monkeypatch.setattr(mod, "_write_secret",
                        lambda sid, v: (state["written"].append((sid, v)),
                                        state["secrets"].__setitem__(sid, v)))
    monkeypatch.setattr(mod, "_delete_secret",
                        lambda sid: (state["deleted"].append(sid),
                                     state["secrets"].pop(sid, None)))

    def _session():
        yield _FakeSession(_FakeClinic())
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[verify_token] = lambda: {
        "role": "super_admin", "uid": "U1", "email": "admin@zoolstra.com"}

    yield TestClient(app), state
    app.dependency_overrides.clear()


def test_status_unconfigured(harness):
    client, _ = harness
    body = client.get(URL).json()
    assert body == {"configured": False, "site_id_set": False,
                    "track_api_key_set": False, "region": "us"}


def test_status_configured_never_leaks_values(harness):
    client, state = harness
    state["secrets"] = {
        "customerio-site-id-CLINIC_1": "SITE",
        "customerio-track-api-key-CLINIC_1": "KEY",
        "customerio-region-CLINIC_1": "eu",
    }
    body = client.get(URL).json()
    assert body["configured"] is True and body["region"] == "eu"
    assert "SITE" not in str(body) and "KEY" not in str(body)


def test_save_both_credentials(harness):
    client, state = harness
    resp = client.post(URL, json={"site_id": "S1", "track_api_key": "K1"})
    assert resp.status_code == 200
    assert set(resp.json()["written"]) == {"site_id", "track_api_key"}
    assert state["secrets"]["customerio-site-id-CLINIC_1"] == "S1"
    assert state["secrets"]["customerio-track-api-key-CLINIC_1"] == "K1"


def test_partial_save_rotates_one_key_only(harness):
    client, state = harness
    client.post(URL, json={"track_api_key": "K2"})
    assert [s for s, _ in state["written"]] == ["customerio-track-api-key-CLINIC_1"]


def test_blank_save_400(harness):
    client, _ = harness
    assert client.post(URL, json={"site_id": "  "}).status_code == 400
    assert client.post(URL, json={}).status_code == 400


def test_bad_region_422(harness):
    client, _ = harness
    assert client.post(URL, json={"region": "mars"}).status_code == 422


def test_delete_disables(harness):
    client, state = harness
    state["secrets"] = {"customerio-site-id-CLINIC_1": "S",
                        "customerio-track-api-key-CLINIC_1": "K"}
    resp = client.delete(URL)
    assert resp.status_code == 200 and resp.json()["configured"] is False
    assert len(state["deleted"]) == 3
    assert client.get(URL).json()["configured"] is False


def test_viewer_cannot_write(harness):
    client, _ = harness
    app.dependency_overrides[verify_token] = lambda: {
        "role": "viewer", "uid": "U2", "email": "v@x.com"}
    assert client.post(URL, json={"site_id": "S"}).status_code == 403
    assert client.delete(URL).status_code == 403
