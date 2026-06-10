"""Tests for admin notes on Blueprint appointment types / providers.

Covers the upsert endpoint's create / update / clear branches via a minimal
fake session (no real DB). _get_blueprint_config is patched so no Cloud SQL /
Blueprint call happens; auth is bypassed as super_admin.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import app
from api.core.db import get_session
from api.core.orm import ClinicBlueprintEntityNote
from api.deps import verify_token


class _FakeDb:
    """Session stand-in for the notes upsert route.

    ``scalar`` returns the configured existing row (or None); ``add`` and
    ``delete`` just record what the route did so tests can assert the branch.
    """

    def __init__(self, existing: ClinicBlueprintEntityNote | None = None):
        self.existing = existing
        self.added: list = []
        self.deleted: list = []

    def scalar(self, _stmt):
        return self.existing

    def add(self, row):
        self.added.append(row)

    def delete(self, row):
        self.deleted.append(row)


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setattr(
        "api.voice_agent.blueprint._get_blueprint_config",
        lambda db, clinic_id: {"instance_id": "inst"},
    )

    def _make(db: _FakeDb) -> TestClient:
        app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "t"}
        app.dependency_overrides[get_session] = lambda: db
        return TestClient(app, raise_server_exceptions=False)

    yield _make
    app.dependency_overrides.clear()


_URL = "/blueprint/C1/notes"


def test_create_note_when_none_exists(make_client):
    db = _FakeDb(existing=None)
    client = make_client(db)
    resp = client.put(_URL, json={
        "entity_kind": "appointment_type", "entity_id": 3, "note": "45-min comprehensive eval",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "entity_kind": "appointment_type", "entity_id": 3, "note": "45-min comprehensive eval",
    }
    assert len(db.added) == 1 and db.added[0].note == "45-min comprehensive eval"
    assert db.deleted == []


def test_update_existing_note(make_client):
    row = ClinicBlueprintEntityNote(
        clinic_id="C1", entity_kind="provider", entity_id=526, note="old",
    )
    db = _FakeDb(existing=row)
    client = make_client(db)
    resp = client.put(_URL, json={
        "entity_kind": "provider", "entity_id": 526, "note": "Pediatric specialty",
    })
    assert resp.status_code == 200, resp.text
    assert row.note == "Pediatric specialty"   # mutated in place
    assert db.added == [] and db.deleted == []


def test_blank_note_deletes_existing(make_client):
    row = ClinicBlueprintEntityNote(
        clinic_id="C1", entity_kind="provider", entity_id=526, note="old",
    )
    db = _FakeDb(existing=row)
    client = make_client(db)
    resp = client.put(_URL, json={"entity_kind": "provider", "entity_id": 526, "note": "   "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["note"] == ""
    assert db.deleted == [row] and db.added == []


def test_blank_note_when_none_is_noop(make_client):
    db = _FakeDb(existing=None)
    client = make_client(db)
    resp = client.put(_URL, json={"entity_kind": "appointment_type", "entity_id": 9, "note": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["note"] == ""
    assert db.added == [] and db.deleted == []


def test_invalid_entity_kind_rejected(make_client):
    db = _FakeDb()
    client = make_client(db)
    resp = client.put(_URL, json={"entity_kind": "widget", "entity_id": 1, "note": "x"})
    assert resp.status_code == 422  # Literal validation
