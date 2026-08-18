"""
Renaming a clinic (`PATCH /clinics/{id}`) and an instance
(`PATCH /instance/{id}`).

Both names were immutable after provisioning until now — neither appeared in
its update model, so a typo made during an onboarding call could only be fixed
in Cloud SQL by hand.

Renaming is safe because every consumer treats these as LABELS: reports, the
clinic picker, the voice-agent prompt copy, the marts' clinic dimension, and the
`ClinicData.webforms` stamp. Nothing joins on them — scope, FKs and the
per-clinic PMS secrets (`clinic_{clinic_id}_{pms}_{key}`) all key on ids. The
test that matters most is therefore the dispatch one: `clinic_name` lives on
`clinics`, and routing it to `clinic_location_details` instead would 400 or
write to the wrong table.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import app
from api.core.db import get_session
from api.deps import verify_token


class FakeClinic:
    """clinics row + its 1:1 location child, as update_clinic walks them."""

    def __init__(self):
        self.clinic_id = "c1"
        self.instance_id = "i1"
        self.clinic_name = "Old Clinic Name"
        self.address = "1 Old St"
        self.deleted_at = None
        self.location = SimpleNamespace(
            clinic_id="c1", phone="+1 555 0000", hours_monday="9:00 AM - 5:00 PM")


class FakeInstance:
    def __init__(self):
        self.instance_id = "i1"
        self.instance_name = "Old Business Name"
        self.primary_contact_uid = "owner"


class FakeSession:
    def __init__(self, clinic=None, instance=None):
        self._objs = {"c1": clinic, "i1": instance}
        self.added, self.committed = [], False

    def get(self, _entity, pk):
        return self._objs.get(pk)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True


def client_as(role, session) -> TestClient:
    app.dependency_overrides[verify_token] = lambda: {"role": role, "uid": "me"}
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


# ── clinic ───────────────────────────────────────────────────────────────────

def test_clinic_can_be_renamed():
    clinic = FakeClinic()
    c = client_as("super_admin", FakeSession(clinic=clinic))
    r = c.patch("/clinics/c1", json={"clinic_name": "New Clinic Name"})
    assert r.status_code == 200
    assert clinic.clinic_name == "New Clinic Name"


def test_clinic_name_is_dispatched_to_the_clinics_table():
    """It is a `clinics` column, not a `clinic_location_details` one. Landing in
    the wrong dispatch bucket would write the name onto the location row."""
    clinic = FakeClinic()
    c = client_as("super_admin", FakeSession(clinic=clinic))
    c.patch("/clinics/c1", json={"clinic_name": "Renamed"})
    assert clinic.clinic_name == "Renamed"
    assert not hasattr(clinic.location, "clinic_name")


def test_rename_alongside_other_fields_still_splits_across_both_tables():
    clinic = FakeClinic()
    c = client_as("super_admin", FakeSession(clinic=clinic))
    r = c.patch("/clinics/c1", json={
        "clinic_name": "Renamed", "address": "2 New Rd", "phone": "+1 555 1111"})
    assert r.status_code == 200
    assert clinic.clinic_name == "Renamed" and clinic.address == "2 New Rd"
    assert clinic.location.phone == "+1 555 1111"


def test_blank_clinic_name_is_refused():
    """`_reject_empty_string` — a clinic with no name would render as a blank
    row in every picker and report."""
    clinic = FakeClinic()
    c = client_as("super_admin", FakeSession(clinic=clinic))
    assert c.patch("/clinics/c1", json={"clinic_name": "   "}).status_code == 422
    assert clinic.clinic_name == "Old Clinic Name"


def test_clinic_rename_is_trimmed():
    clinic = FakeClinic()
    c = client_as("super_admin", FakeSession(clinic=clinic))
    c.patch("/clinics/c1", json={"clinic_name": "  Padded Name  "})
    assert clinic.clinic_name == "Padded Name"


def test_clinic_rename_requires_write_access():
    clinic = FakeClinic()
    c = client_as("viewer", FakeSession(clinic=clinic))
    assert c.patch("/clinics/c1", json={"clinic_name": "Nope"}).status_code == 403
    assert clinic.clinic_name == "Old Clinic Name"


# ── instance ─────────────────────────────────────────────────────────────────

def test_instance_can_be_renamed():
    instance = FakeInstance()
    c = client_as("super_admin", FakeSession(instance=instance))
    with patch("api.account.instances.require_write_access"):
        r = c.patch("/instance/i1", json={"instance_name": "New Business Name"})
    assert r.status_code == 200
    assert instance.instance_name == "New Business Name"


def test_blank_instance_name_is_refused():
    instance = FakeInstance()
    c = client_as("super_admin", FakeSession(instance=instance))
    with patch("api.account.instances.require_write_access"):
        r = c.patch("/instance/i1", json={"instance_name": ""})
    assert r.status_code == 422
    assert instance.instance_name == "Old Business Name"


def test_renaming_does_not_disturb_the_upstream_ids():
    """update_instance drops None, so an untouched field is never written —
    a rename must not blank the ids the ETL reads."""
    instance = FakeInstance()
    instance.google_ads_customer_id = "123-456-7890"
    instance.invoca_profile_id = "99887"
    c = client_as("super_admin", FakeSession(instance=instance))
    with patch("api.account.instances.require_write_access"):
        c.patch("/instance/i1", json={"instance_name": "Renamed"})
    assert instance.google_ads_customer_id == "123-456-7890"
    assert instance.invoca_profile_id == "99887"
