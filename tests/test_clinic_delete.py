"""
Deleting a clinic (`DELETE /clinics/{id}`) from the dashboard.

It is a SOFT delete: `clinics.deleted_at` is the convention every reader in
both repos already filters on, and BigQuery rows stamped with the clinic_id
(webforms, marts, PHI snapshots) need a row to resolve against. The endpoint
used to `db.delete()` the row, which cascaded every config table away.

A clinic with a live VAPI assistant is refused (409): the assistant would keep
answering the phone number with nothing left in Cloud SQL that knows about it.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import app
from api.core.db import get_session
from api.deps import verify_token


class FakeClinic:
    def __init__(self, vapi_assistant_id=None):
        self.clinic_id = "c1"
        self.instance_id = "i1"
        self.clinic_name = "Clinic"
        self.deleted_at = None
        self.etl_enabled = True
        self.voice_agent = SimpleNamespace(
            voice_agent_status="active" if vapi_assistant_id else "inactive",
            vapi_assistant_id=vapi_assistant_id,
        )


class FakeSession:
    def __init__(self, clinic):
        self._clinic = clinic
        self.deleted = []

    def get(self, _entity, pk):
        return self._clinic if pk == "c1" else None

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        pass


def client_as(role, session) -> TestClient:
    app.dependency_overrides[verify_token] = lambda: {"role": role, "uid": "me"}
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


def test_delete_sets_deleted_at_and_keeps_the_row():
    clinic = FakeClinic()
    session = FakeSession(clinic)
    r = client_as("super_admin", session).delete("/clinics/c1")
    assert r.status_code == 200
    assert clinic.deleted_at is not None
    assert clinic.etl_enabled is False
    assert session.deleted == [], "must not hard-delete — readers filter on deleted_at"
    assert r.json()["clinic_id"] == "c1"


def test_delete_is_refused_while_a_voice_agent_is_live():
    clinic = FakeClinic(vapi_assistant_id="asst_123")
    r = client_as("super_admin", FakeSession(clinic)).delete("/clinics/c1")
    assert r.status_code == 409
    assert clinic.deleted_at is None


def test_delete_requires_write_access():
    clinic = FakeClinic()
    r = client_as("viewer", FakeSession(clinic)).delete("/clinics/c1")
    assert r.status_code == 403
    assert clinic.deleted_at is None


def test_already_deleted_clinic_is_404():
    clinic = FakeClinic()
    clinic.deleted_at = "2026-01-01"
    r = client_as("super_admin", FakeSession(clinic)).delete("/clinics/c1")
    assert r.status_code == 404
