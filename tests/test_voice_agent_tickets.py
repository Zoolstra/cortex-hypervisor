"""Tests for the submit_ticket endpoint's message hand-off behavior:

  - callback number resolution: agent-transcribed caller_phone wins, else the
    VAPI caller-ID header (X-Vapi-Caller-Number) is used as a fallback, and the
    resolved number is what gets stored + alerted on;
  - a best-effort staff alert (notify_new_ticket) fires with the resolved number;
  - a failing alert never breaks the ticket write (still 200).

BigQuery, the DB session, VAPI auth, and the notifier are all stubbed so no
external calls happen.
"""
from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

from api import app
from api.core.db import get_session
from api.core.orm import Clinic, ClinicVoiceAgentConfiguration
from api.voice_agent.blueprint import verify_vapi_secret


class _FakeQueryJob:
    def result(self):
        return None


class _FakeDB:
    """Minimal db.get(model, key) stand-in for the ticket endpoint."""

    def __init__(self, alert_sms_to="+17805550100"):
        self._alert_sms_to = alert_sms_to

    def get(self, model, key):
        # Plain stand-ins (not SQLAlchemy instances) — the endpoint only reads
        # a few attributes; instantiating real ORM models via __new__ breaks on
        # instrumented attribute assignment.
        if model is Clinic:
            return types.SimpleNamespace(clinic_name="Test Clinic", deleted_at=None)
        if model is ClinicVoiceAgentConfiguration:
            return types.SimpleNamespace(alert_sms_to=self._alert_sms_to, alert_email_to=None)
        return None


@pytest.fixture
def captured(monkeypatch):
    """Stub BQ + notifier + auth/session; capture the notify call kwargs."""
    calls = {}

    monkeypatch.setattr(
        "api.voice_agent.voice_agent.bq_client",
        types.SimpleNamespace(query=lambda *a, **k: _FakeQueryJob()),
    )

    def fake_notify(**kwargs):
        calls["notify"] = kwargs

    monkeypatch.setattr("api.voice_agent.voice_agent.notify.notify_new_ticket", fake_notify)

    app.dependency_overrides[verify_vapi_secret] = lambda: None
    def _fake_session():
        yield _FakeDB()

    app.dependency_overrides[get_session] = _fake_session
    yield calls
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_caller_id_header_used_when_agent_omits_phone(captured, client):
    r = client.post(
        "/clinics/abc/voice_agent/tickets",
        json={"patient_match_status": "new", "caller_name": "Jane"},
        headers={"X-Vapi-Caller-Number": "+17805551234"},
    )
    assert r.status_code == 200
    # Alert fired with the caller-ID as the callback number.
    assert captured["notify"]["callback_number"] == "+17805551234"
    assert captured["notify"]["alert_sms_to"] == "+17805550100"


def test_transcribed_phone_wins_over_caller_id(captured, client):
    r = client.post(
        "/clinics/abc/voice_agent/tickets",
        json={"patient_match_status": "new", "caller_phone": "+16045559999"},
        headers={"X-Vapi-Caller-Number": "+17805551234"},
    )
    assert r.status_code == 200
    assert captured["notify"]["callback_number"] == "+16045559999"


def test_alert_failure_does_not_break_ticket(monkeypatch, client):
    monkeypatch.setattr(
        "api.voice_agent.voice_agent.bq_client",
        types.SimpleNamespace(query=lambda *a, **k: _FakeQueryJob()),
    )

    def boom(**kwargs):
        raise RuntimeError("sms provider down")

    monkeypatch.setattr("api.voice_agent.voice_agent.notify.notify_new_ticket", boom)

    def _fake_session():
        yield _FakeDB()

    app.dependency_overrides[verify_vapi_secret] = lambda: None
    app.dependency_overrides[get_session] = _fake_session
    try:
        r = client.post(
            "/clinics/abc/voice_agent/tickets",
            json={"patient_match_status": "new"},
        )
        assert r.status_code == 200
        assert "ticket_id" in r.json()
    finally:
        app.dependency_overrides.clear()
