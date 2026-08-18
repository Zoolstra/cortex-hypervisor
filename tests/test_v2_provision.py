"""
Tests for `/v2/admin/provision` (api/v2/provision.py).

The endpoint creates a Firebase user AND writes Cloud SQL rows, which cannot be
one transaction. What is worth pinning down is therefore the ORDERING: every
refusal must land before Firebase is touched, so the common failure (this
contact already has an instance) leaves nothing behind. Firebase writes are
patched — no test may create a real account or claim.
"""
from types import SimpleNamespace
from unittest.mock import patch

import firebase_admin.auth as real_auth
import pytest
from fastapi.testclient import TestClient

from api import app
from api.core.db import get_session
from api.deps import verify_token


class FakeQuery:
    """`db.query(X).filter(...).count()` — always an empty clinic, which is the
    only state a freshly provisioned one can be in."""

    def filter(self, *_a, **_kw):
        return self

    def count(self):
        return 0


class FakeSession:
    """Enough Session surface for provisioning: the ownership probe (`scalar`),
    the row writes (`add`/`flush`), and the seeding reads that
    `seed_voice_agent_defaults` performs (`get`/`query`)."""

    def __init__(self, owned_instance_name=None):
        self._owned = owned_instance_name
        self.added, self.committed = [], False

    def scalar(self, _stmt):
        return self._owned

    def get(self, _entity, _pk):
        return None

    def query(self, _entity):
        return FakeQuery()

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.committed = True


def client_as(role, uid="me", session=None) -> TestClient:
    app.dependency_overrides[verify_token] = lambda: {"role": role, "uid": uid}
    app.dependency_overrides[get_session] = lambda: session or FakeSession()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


CLINIC = {
    "ref_id": "clinic_1",
    "clinic_name": "Downtown Hearing",
    "address": "123 Main St",
    "place_id": "ChIJ123",
    "about_us": "We help.",
    "hours_monday": "9:00 AM - 5:00 PM",
    "hours_tuesday": "9:00 AM - 5:00 PM",
    "hours_wednesday": "9:00 AM - 5:00 PM",
    "hours_thursday": "9:00 AM - 5:00 PM",
    "hours_friday": "9:00 AM - 5:00 PM",
    "hours_saturday": "Closed",
    "hours_sunday": "Closed",
    "phone": "+1 604 555 0100",
    "time_zone": "America/Vancouver",
    "country": "CA",
}

BODY = {
    "primary_contact_email": "jane@acme.com",
    "primary_contact_name": "Jane Smith",
    "instance": {"instance_name": "Acme Hearing"},
    "clinics": [CLINIC],
}


def _fb_user(uid="u1", email="jane@acme.com", role=None):
    return SimpleNamespace(uid=uid, email=email,
                           custom_claims={"role": role} if role else None)


# ── the gate ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["admin", "viewer", None])
def test_non_super_admins_are_refused(role):
    """Provisioning mints accounts and instances — super_admin only."""
    c = client_as(role)
    with patch("api.v2.provision.fb_auth.create_user") as mk:
        assert c.post("/v2/admin/provision", json=BODY).status_code == 403
    mk.assert_not_called()


# ── happy path ───────────────────────────────────────────────────────────────

def test_new_contact_gets_account_claim_instance_and_clinics():
    sess = FakeSession()
    c = client_as("super_admin", session=sess)
    with patch("api.v2.provision.fb_auth.get_user_by_email",
               side_effect=real_auth.UserNotFoundError("nope")), \
         patch("api.v2.provision.fb_auth.create_user",
               return_value=_fb_user()) as mk, \
         patch("api.v2.provision.fb_auth.set_custom_user_claims") as setter, \
         patch("api.v2.provision.fb_auth.generate_password_reset_link",
               return_value="https://reset"):
        r = c.post("/v2/admin/provision", json=BODY)

    assert r.status_code == 201
    body = r.json()
    assert body["uid"] == "u1" and body["created_user"] is True
    assert body["instance_id"]
    # ref_id is echoed back so the caller can match its draft rows to real ids.
    assert set(body["clinic_ids"]) == {"clinic_1"}
    assert body["password_reset_link"] == "https://reset"
    mk.assert_called_once()
    assert mk.call_args.kwargs["email_verified"] is False
    setter.assert_called_once_with("u1", {"role": "admin"})
    # Instance + clinic + location details + voice-agent config, at minimum.
    assert len(sess.added) >= 4


def test_existing_contact_without_an_instance_is_reused_not_recreated():
    c = client_as("super_admin", session=FakeSession())
    with patch("api.v2.provision.fb_auth.get_user_by_email",
               return_value=_fb_user(uid="existing", role="viewer")), \
         patch("api.v2.provision.fb_auth.create_user") as mk, \
         patch("api.v2.provision.fb_auth.set_custom_user_claims") as setter, \
         patch("api.v2.provision.fb_auth.generate_password_reset_link") as link:
        r = c.post("/v2/admin/provision", json=BODY)

    assert r.status_code == 201
    assert r.json()["uid"] == "existing" and r.json()["created_user"] is False
    mk.assert_not_called()
    # An existing claim is never overwritten — provisioning must not re-level a
    # user who was deliberately set to viewer (or super_admin).
    setter.assert_not_called()
    # No account was created, so there is nothing to send a set-password link for.
    link.assert_not_called()
    assert r.json()["password_reset_link"] is None


def test_existing_contact_with_no_claim_gets_the_default_role():
    c = client_as("super_admin", session=FakeSession())
    with patch("api.v2.provision.fb_auth.get_user_by_email",
               return_value=_fb_user(uid="claimless")), \
         patch("api.v2.provision.fb_auth.set_custom_user_claims") as setter:
        assert c.post("/v2/admin/provision", json=BODY).status_code == 201
    setter.assert_called_once_with("claimless", {"role": "admin"})


def test_upstream_ids_are_stored_on_the_instance():
    """These drive what the ETL pulls, so setting them at provisioning is what
    makes a new client's data start flowing without a follow-up config pass."""
    sess = FakeSession()
    c = client_as("super_admin", session=sess)
    with patch("api.v2.provision.fb_auth.get_user_by_email",
               return_value=_fb_user(role="admin")):
        r = c.post("/v2/admin/provision", json={**BODY, "instance": {
            "instance_name": "Acme Hearing",
            "google_ads_customer_id": " 123-456-7890 ",
            "invoca_profile_id": "  99887  ",
        }})
    assert r.status_code == 201
    inst = next(o for o in sess.added if type(o).__name__ == "Instance")
    # Trimmed — a copy-pasted id with trailing whitespace must not become a
    # value the ETL then fails to match on.
    assert inst.google_ads_customer_id == "123-456-7890"
    assert inst.invoca_profile_id == "99887"


@pytest.mark.parametrize("supplied", [{}, {"google_ads_customer_id": "",
                                          "invoca_profile_id": "   "}])
def test_blank_upstream_ids_become_null_not_empty_string(supplied):
    """The ETL tests these for presence; "" is truthy in a SQL join and would
    quietly widen a query instead of excluding the instance."""
    sess = FakeSession()
    c = client_as("super_admin", session=sess)
    with patch("api.v2.provision.fb_auth.get_user_by_email",
               return_value=_fb_user(role="admin")):
        r = c.post("/v2/admin/provision", json={**BODY, "instance": {
            "instance_name": "Acme Hearing", **supplied}})
    assert r.status_code == 201
    inst = next(o for o in sess.added if type(o).__name__ == "Instance")
    assert inst.google_ads_customer_id is None
    assert inst.invoca_profile_id is None


def test_instance_without_clinics_is_allowed():
    """Clinics can be added later from the instance directory."""
    sess = FakeSession()
    c = client_as("super_admin", session=sess)
    with patch("api.v2.provision.fb_auth.get_user_by_email",
               return_value=_fb_user(role="admin")):
        r = c.post("/v2/admin/provision", json={**BODY, "clinics": []})
    assert r.status_code == 201 and r.json()["clinic_ids"] == {}


# ── refusals land before any write ───────────────────────────────────────────

def test_contact_who_already_owns_an_instance_is_409_before_firebase():
    sess = FakeSession(owned_instance_name="Acme Hearing")
    c = client_as("super_admin", session=sess)
    with patch("api.v2.provision.fb_auth.get_user_by_email",
               return_value=_fb_user()), \
         patch("api.v2.provision.fb_auth.create_user") as mk, \
         patch("api.v2.provision.fb_auth.set_custom_user_claims") as setter:
        r = c.post("/v2/admin/provision", json=BODY)

    assert r.status_code == 409
    assert "Acme Hearing" in r.json()["detail"]
    mk.assert_not_called()
    setter.assert_not_called()
    assert not sess.added


def test_invalid_clinic_is_rejected_before_firebase():
    """ClinicCreate requires a non-empty name; validation must precede the
    account write or a rejected form leaves an orphan Firebase user."""
    sess = FakeSession()
    c = client_as("super_admin", session=sess)
    bad = {**BODY, "clinics": [{**CLINIC, "clinic_name": "  "}]}
    with patch("api.v2.provision.fb_auth.get_user_by_email") as lookup, \
         patch("api.v2.provision.fb_auth.create_user") as mk:
        assert c.post("/v2/admin/provision", json=bad).status_code == 422
    lookup.assert_not_called()
    mk.assert_not_called()
    assert not sess.added


@pytest.mark.parametrize("field,value", [
    ("primary_contact_email", "   "),
    ("primary_contact_name", ""),
])
def test_blank_contact_fields_are_rejected(field, value):
    c = client_as("super_admin", session=FakeSession())
    with patch("api.v2.provision.fb_auth.get_user_by_email") as lookup:
        r = c.post("/v2/admin/provision", json={**BODY, field: value})
    assert r.status_code == 422
    lookup.assert_not_called()


def test_blank_instance_name_is_rejected():
    c = client_as("super_admin", session=FakeSession())
    with patch("api.v2.provision.fb_auth.get_user_by_email") as lookup:
        r = c.post("/v2/admin/provision",
                   json={**BODY, "instance": {"instance_name": " "}})
    assert r.status_code == 422
    lookup.assert_not_called()


def test_create_race_surfaces_as_409():
    c = client_as("super_admin", session=FakeSession())
    with patch("api.v2.provision.fb_auth.get_user_by_email",
               side_effect=real_auth.UserNotFoundError("nope")), \
         patch("api.v2.provision.fb_auth.create_user",
               side_effect=real_auth.EmailAlreadyExistsError("dup", None, None)):
        assert c.post("/v2/admin/provision", json=BODY).status_code == 409


def test_provision_still_succeeds_when_reset_link_fails():
    """The account and instance exist by then; failing would misreport reality."""
    c = client_as("super_admin", session=FakeSession())
    with patch("api.v2.provision.fb_auth.get_user_by_email",
               side_effect=real_auth.UserNotFoundError("nope")), \
         patch("api.v2.provision.fb_auth.create_user", return_value=_fb_user()), \
         patch("api.v2.provision.fb_auth.set_custom_user_claims"), \
         patch("api.v2.provision.fb_auth.generate_password_reset_link",
               side_effect=RuntimeError("identity toolkit down")):
        r = c.post("/v2/admin/provision", json=BODY)
    assert r.status_code == 201 and r.json()["password_reset_link"] is None
