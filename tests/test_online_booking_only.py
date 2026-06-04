"""
Tests for the ``online_booking_only`` per-clinic config on
SearchAppointmentAvailabilityProtocol — ACNA's request and the first
instance of the design doc's §5 protocol config story.

Behavioural invariants under test:

1. Default config: existing behaviour preserved. No extra Blueprint call.
2. ``online_booking_only=True``: the adapter calls
   ``POST /availability/search`` with ``availableForOnlineBookingOnly=true``,
   then restricts the slot search to the (provider, location) pairs that
   came back.
3. Empty online-bookable set: returns no slots. The agent will fall back
   to ticket capture per the protocol prompt.
4. Pydantic config validates against the schema: extra fields rejected,
   bool typed correctly.
"""
from __future__ import annotations

import pytest
import httpx
from fastapi.testclient import TestClient

from api import app
from api.voice_agent.blueprint import verify_vapi_secret
from api.core.db import get_session
from api.voice_agent.protocols import (
    SearchAppointmentAvailabilityProtocol,
    load_protocol_config,
)


# ── Pydantic config model ─────────────────────────────────────────────────────


def test_config_model_default_is_off():
    cfg = SearchAppointmentAvailabilityProtocol.config_model()
    assert cfg.online_booking_only is False


def test_config_model_accepts_explicit_true():
    cfg = SearchAppointmentAvailabilityProtocol.config_model(online_booking_only=True)
    assert cfg.online_booking_only is True


def test_config_model_rejects_extra_fields():
    """``model_config = {"extra": "forbid"}`` — an operator typo (e.g.
    ``onlinebookingonly`` instead of ``online_booking_only``) should
    surface as a validation error, not be silently ignored.
    """
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SearchAppointmentAvailabilityProtocol.config_model(
            online_booking_only=True,
            stowaway_field="surprise",
        )


def test_config_model_emits_json_schema_for_dashboard():
    schema = SearchAppointmentAvailabilityProtocol.config_model.model_json_schema()
    assert schema["type"] == "object"
    props = schema["properties"]
    assert "online_booking_only" in props
    assert props["online_booking_only"]["type"] == "boolean"
    assert props["online_booking_only"]["default"] is False
    # Title + description make it into the schema so the auto-form can
    # render meaningful labels.
    assert props["online_booking_only"]["title"]
    assert props["online_booking_only"]["description"]


# ── Adapter-level integration via the route ───────────────────────────────────


_FAKE_CONFIG = {
    "clinic_name": "Test Clinic",
    "api_url": "https://test.bp-solutions.net/test_clinic/rest/hello",
    "clinic_code": "TEST",
    "api_key": "FAKE_API_KEY",
    "timezone": "America/Vancouver",
    "instance_id": "test-instance",
    "user_id": "42",
    "location_id": "7",
}


class StubResp:
    def __init__(self, status_code: int = 200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://stub")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("stub", request=request, response=response)

    def json(self):
        return self._json


class HttpxStub:
    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.routes: list[tuple[str, str, object]] = []

    def set(self, method: str, url_suffix: str, response) -> None:
        self.routes.append((method, url_suffix, response))

    def _dispatch(self, method: str, url: str, payload):
        self.calls.append((method, url, payload))
        for m, frag, resp in reversed(self.routes):
            if m == method and url.endswith(frag):
                return resp(payload) if callable(resp) else resp
        return StubResp(404, {})

    def get(self, url, params=None, timeout=None, **kw):
        return self._dispatch("GET", url, params)

    def post(self, url, json=None, timeout=None, **kw):
        return self._dispatch("POST", url, json)

    def put(self, url, json=None, timeout=None, **kw):
        return self._dispatch("PUT", url, json)


class _ProtoRow:
    """Stand-in for the ClinicProtocol ORM row used by load_protocol_config."""
    def __init__(self, config):
        self.config = config


@pytest.fixture
def stub(monkeypatch):
    s = HttpxStub()
    monkeypatch.setattr("api.voice_agent.pms.blueprint.httpx", s)
    monkeypatch.setattr(
        "api.voice_agent.pms.blueprint._get_blueprint_config",
        lambda db, clinic_id: dict(_FAKE_CONFIG),
    )
    app.dependency_overrides[verify_vapi_secret] = lambda: None
    app.dependency_overrides[get_session] = lambda: iter([None])
    yield s
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _stub_protocol_config(monkeypatch, online_booking_only: bool):
    """Bypass the ClinicProtocol ORM lookup with a fixed config."""
    cfg_dict = {"online_booking_only": online_booking_only}
    monkeypatch.setattr(
        "api.voice_agent.blueprint.load_protocol_config",
        lambda db, clinic_id, protocol_id:
            SearchAppointmentAvailabilityProtocol.config_model(**cfg_dict),
    )


def test_default_config_does_not_call_search_endpoint(stub, client, monkeypatch):
    """When online_booking_only is False (the default), the slot search
    hits ONLY ``GET /availability/?eventTypeId=...`` — no
    ``POST /availability/search`` round trip. Existing behavior preserved.
    """
    _stub_protocol_config(monkeypatch, online_booking_only=False)
    stub.set("GET", "/availability/", StubResp(200, []))

    resp = client.post(
        "/blueprint/CLINIC_X/availability/find",
        json={"event_type_id": 1, "start_date": "2026-06-01", "end_date": "2026-06-08"},
    )
    assert resp.status_code == 200, resp.text
    methods = [c[0] for c in stub.calls]
    assert "POST" not in methods, (
        f"Should not call POST /availability/search when online_booking_only=False; got calls: {stub.calls}"
    )
    assert methods.count("GET") == 1


def test_online_booking_only_calls_search_first_then_restricts(stub, client, monkeypatch):
    """When online_booking_only is True:
       1. Adapter calls POST /availability/search with availableForOnlineBookingOnly=true
       2. Extracts (provider_id, location_id) pairs from the response
       3. Calls GET /availability/?eventTypeId=... with providers + locations params
          set to the union of pairs from step 2
    """
    _stub_protocol_config(monkeypatch, online_booking_only=True)

    # POST /availability/search returns three blocks: two providers at one location.
    stub.set("POST", "/availability/search", StubResp(200, [
        {"provider_id": 10, "location_id": 7},
        {"provider_id": 11, "location_id": 7},
        {"provider_id": 10, "location_id": 7},  # duplicate, dedup expected
    ]))
    captured: list[dict] = []
    def capture(params):
        captured.append(params)
        return StubResp(200, [])
    stub.set("GET", "/availability/", capture)

    resp = client.post(
        "/blueprint/CLINIC_X/availability/find",
        json={"event_type_id": 1, "start_date": "2026-06-01", "end_date": "2026-06-08"},
    )
    assert resp.status_code == 200, resp.text

    # Verify the search payload carried the filter.
    search_payloads = [c[2] for c in stub.calls if c[0] == "POST" and c[1].endswith("/availability/search")]
    assert len(search_payloads) == 1
    assert search_payloads[0]["availableForOnlineBookingOnly"] is True

    # Verify the GET narrowed providers + locations to the online-bookable set.
    assert len(captured) == 1
    params = captured[0]
    # Providers serialized as comma-joined; dedup → "10,11".
    assert params["providers"] == "10,11"
    assert params["locations"] == "7"


def test_online_booking_only_empty_set_returns_no_slots(stub, client, monkeypatch):
    """No providers flagged online-bookable → no slot request issued,
    response is an empty days list. Caller's protocol prompt covers
    "no availability — capture in ticket".
    """
    _stub_protocol_config(monkeypatch, online_booking_only=True)
    stub.set("POST", "/availability/search", StubResp(200, []))  # no blocks

    captured: list[dict] = []
    def capture(params):
        captured.append(params)
        return StubResp(200, [])
    stub.set("GET", "/availability/", capture)

    resp = client.post(
        "/blueprint/CLINIC_X/availability/find",
        json={"event_type_id": 1, "start_date": "2026-06-01", "end_date": "2026-06-08"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"days": []}
    assert captured == [], "No GET /availability/ should be issued when the online-bookable set is empty"


def test_online_booking_only_ignores_agent_supplied_providers(stub, client, monkeypatch):
    """When the operator has set online_booking_only=true, the agent
    cannot override the provider restriction by passing its own providers
    list — policy beats request. The GET still goes out narrowed to the
    online-bookable set, not the agent's set.
    """
    _stub_protocol_config(monkeypatch, online_booking_only=True)
    stub.set("POST", "/availability/search", StubResp(200, [
        {"provider_id": 10, "location_id": 7},
    ]))
    captured: list[dict] = []
    def capture(params):
        captured.append(params)
        return StubResp(200, [])
    stub.set("GET", "/availability/", capture)

    resp = client.post(
        "/blueprint/CLINIC_X/availability/find",
        json={
            "event_type_id": 1,
            "start_date": "2026-06-01",
            "end_date": "2026-06-08",
            "providers": [99, 100],  # agent tries to ask for unflagged providers
        },
    )
    assert resp.status_code == 200
    assert captured[0]["providers"] == "10"


# ── load_protocol_config helper ───────────────────────────────────────────────


def test_load_protocol_config_falls_back_to_defaults_when_row_missing(monkeypatch):
    """Common case: a clinic that has never been configured for this
    protocol — no clinic_protocols row. Should produce a default config
    instance, not raise.
    """
    class FakeDb:
        def get(self, model, key):
            return None

    cfg = load_protocol_config(FakeDb(), "X", "search_appointment_availability")
    assert cfg.online_booking_only is False


def test_load_protocol_config_reads_persisted_value():
    """Persisted row with ``{"online_booking_only": true}`` should
    surface as a parsed model with the flag set.
    """
    class FakeDb:
        def get(self, model, key):
            return _ProtoRow({"online_booking_only": True})

    cfg = load_protocol_config(FakeDb(), "X", "search_appointment_availability")
    assert cfg.online_booking_only is True


def test_load_protocol_config_unknown_protocol_raises():
    class FakeDb:
        def get(self, model, key):
            return None

    with pytest.raises(KeyError):
        load_protocol_config(FakeDb(), "X", "not_a_real_protocol")
