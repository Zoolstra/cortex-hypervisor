"""
Tests for `/v2/admin/users` (api/v2/admin_users.py).

This endpoint set can grant super_admin over every instance and all PHI, so the
gate and the lockout guard are tested as carefully as the happy path. Firebase
writes are patched — no test may touch a real claim.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import app
from api.core.db import get_session
from api.core.orm import ClinicAdmin, Instance
from api.deps import verify_token


class FakeScalars(list):
    def all(self):
        return list(self)


class FakeSession:
    """Dispatches `scalars(select(X))` by the entity in the statement."""

    def __init__(self, instances=(), clinic_admins=()):
        self._data = {Instance: list(instances), ClinicAdmin: list(clinic_admins)}
        self.added, self.deleted, self.committed = [], [], False

    def scalars(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        return FakeScalars(self._data.get(entity, []))

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.committed = True


def _fb_user(uid, email, role=None, **kw):
    return SimpleNamespace(
        uid=uid, email=email, display_name=kw.get("display_name"),
        disabled=kw.get("disabled", False),
        email_verified=kw.get("email_verified", True),
        provider_data=[SimpleNamespace(provider_id=p)
                       for p in kw.get("providers", ["password"])],
        custom_claims={"role": role} if role else None,
    )


def client_as(role, uid="me", session=None) -> TestClient:
    app.dependency_overrides[verify_token] = lambda: {"role": role, "uid": uid}
    app.dependency_overrides[get_session] = lambda: session or FakeSession()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


# ── the gate ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["admin", "viewer", None])
def test_non_super_admins_are_refused_everywhere(role):
    c = client_as(role)
    assert c.get("/v2/admin/users").status_code == 403
    assert c.patch("/v2/admin/users/u1/role",
                   json={"role": "viewer"}).status_code == 403
    assert c.put("/v2/admin/users/u1/instances",
                 json={"instance_ids": []}).status_code == 403


# ── listing ──────────────────────────────────────────────────────────────────

def test_list_reports_role_and_scope_including_ownership():
    """Ownership and explicit grants both confer access; the union must show."""
    sess = FakeSession(
        instances=[SimpleNamespace(instance_id="i1", instance_name="Alpha",
                                   primary_contact_uid="owner"),
                   SimpleNamespace(instance_id="i2", instance_name="Beta",
                                   primary_contact_uid=None)],
        clinic_admins=[SimpleNamespace(uid="granted", instance_id="i2")],
    )
    users = [_fb_user("owner", "o@x.com", "admin"),
             _fb_user("granted", "g@x.com", "viewer")]
    c = client_as("super_admin", session=sess)
    with patch("api.v2.admin_users.fb_auth.list_users") as lu:
        lu.return_value.iterate_all.return_value = users
        body = c.get("/v2/admin/users").json()

    by_uid = {u["uid"]: u for u in body["users"]}
    assert by_uid["owner"]["instances"] == [
        {"instance_id": "i1", "instance_name": "Alpha", "via": "owner"}]
    assert by_uid["granted"]["instances"] == [
        {"instance_id": "i2", "instance_name": "Beta", "via": "grant"}]
    assert body["count"] == 2 and body["truncated"] is False


def test_super_admin_is_flagged_as_ignoring_scope():
    """An empty instance list on a super_admin means 'everything', not 'nothing'."""
    sess = FakeSession(instances=[])
    c = client_as("super_admin", session=sess)
    with patch("api.v2.admin_users.fb_auth.list_users") as lu:
        lu.return_value.iterate_all.return_value = [
            _fb_user("sa", "sa@zoolstra.com", "super_admin")]
        u = c.get("/v2/admin/users").json()["users"][0]
    assert u["instances"] == [] and u["scope_applies"] is False


def test_user_with_no_claim_is_listed_not_hidden():
    """The 4 claim-less users are exactly who an admin needs to find."""
    c = client_as("super_admin", session=FakeSession())
    with patch("api.v2.admin_users.fb_auth.list_users") as lu:
        lu.return_value.iterate_all.return_value = [_fb_user("n", "n@x.com")]
        u = c.get("/v2/admin/users").json()["users"][0]
    assert u["role"] is None and u["scope_applies"] is True


# ── role changes ─────────────────────────────────────────────────────────────

def test_set_role_writes_the_claim():
    c = client_as("super_admin")
    with patch("api.v2.admin_users.fb_auth.get_user",
               return_value=_fb_user("u1", "u@x.com", "viewer")), \
         patch("api.v2.admin_users.fb_auth.set_custom_user_claims") as setter:
        r = c.patch("/v2/admin/users/u1/role", json={"role": "admin"})
    assert r.status_code == 200
    assert r.json()["previous"] == "viewer" and r.json()["role"] == "admin"
    setter.assert_called_once_with("u1", {"role": "admin"})


def test_self_demotion_is_refused():
    """Demoting yourself locks you out of the endpoint that would undo it."""
    c = client_as("super_admin", uid="me")
    with patch("api.v2.admin_users.fb_auth.set_custom_user_claims") as setter:
        r = c.patch("/v2/admin/users/me/role", json={"role": "viewer"})
    assert r.status_code == 400
    setter.assert_not_called()


def test_self_update_to_super_admin_is_allowed_as_a_noop():
    c = client_as("super_admin", uid="me")
    with patch("api.v2.admin_users.fb_auth.get_user",
               return_value=_fb_user("me", "m@x.com", "super_admin")), \
         patch("api.v2.admin_users.fb_auth.set_custom_user_claims"):
        assert c.patch("/v2/admin/users/me/role",
                       json={"role": "super_admin"}).status_code == 200


def test_invalid_role_is_rejected():
    c = client_as("super_admin")
    with patch("api.v2.admin_users.fb_auth.set_custom_user_claims") as setter:
        r = c.patch("/v2/admin/users/u1/role", json={"role": "root"})
    assert r.status_code == 422
    setter.assert_not_called()


def test_unknown_user_is_404():
    import firebase_admin.auth as real_auth
    c = client_as("super_admin")
    with patch("api.v2.admin_users.fb_auth.get_user",
               side_effect=real_auth.UserNotFoundError("nope")):
        assert c.patch("/v2/admin/users/ghost/role",
                       json={"role": "viewer"}).status_code == 404


# ── instance scope ───────────────────────────────────────────────────────────

def test_set_instances_computes_add_and_remove():
    sess = FakeSession(
        instances=[SimpleNamespace(instance_id="i1", instance_name="A",
                                   primary_contact_uid=None),
                   SimpleNamespace(instance_id="i2", instance_name="B",
                                   primary_contact_uid=None)],
        clinic_admins=[SimpleNamespace(uid="u1", instance_id="i1")],
    )
    c = client_as("super_admin", session=sess)
    r = c.put("/v2/admin/users/u1/instances", json={"instance_ids": ["i2"]})
    assert r.status_code == 200
    assert r.json()["added"] == ["i2"] and r.json()["removed"] == ["i1"]
    assert sess.committed and len(sess.added) == 1 and len(sess.deleted) == 1


def test_unknown_instance_is_rejected_before_any_write():
    sess = FakeSession(instances=[SimpleNamespace(
        instance_id="i1", instance_name="A", primary_contact_uid=None)])
    c = client_as("super_admin", session=sess)
    r = c.put("/v2/admin/users/u1/instances", json={"instance_ids": ["nope"]})
    assert r.status_code == 422
    assert not sess.committed and not sess.added and not sess.deleted


def _instances(*ids):
    return [SimpleNamespace(instance_id=i, instance_name=i.upper(),
                            primary_contact_uid=None) for i in ids]


# ── create ───────────────────────────────────────────────────────────────────

def test_create_user_sets_role_and_grants():
    sess = FakeSession(instances=_instances("i1", "i2"))
    c = client_as("super_admin", session=sess)
    with patch("api.v2.admin_users.fb_auth.create_user",
               return_value=SimpleNamespace(uid="new1")) as mk, \
         patch("api.v2.admin_users.fb_auth.set_custom_user_claims") as setter, \
         patch("api.v2.admin_users.fb_auth.generate_password_reset_link",
               return_value="https://reset"):
        r = c.post("/v2/admin/users", json={"email": "a@b.com", "role": "admin",
                                            "instance_ids": ["i1", "i2"]})
    assert r.status_code == 201
    body = r.json()
    assert body["instances"] == ["i1", "i2"]
    assert body["password_reset_link"] == "https://reset"
    setter.assert_called_once_with("new1", {"role": "admin"})
    assert mk.call_args.kwargs["email_verified"] is False
    assert "password" not in mk.call_args.kwargs
    assert len(sess.added) == 2 and sess.committed


def test_create_super_admin_ignores_instance_grants():
    """Scope does not apply to super_admin; writing grants would be latent state
    that a later demotion silently activates."""
    sess = FakeSession(instances=_instances("i1"))
    c = client_as("super_admin", session=sess)
    with patch("api.v2.admin_users.fb_auth.create_user",
               return_value=SimpleNamespace(uid="sa2")), \
         patch("api.v2.admin_users.fb_auth.set_custom_user_claims"), \
         patch("api.v2.admin_users.fb_auth.generate_password_reset_link",
               return_value="x"):
        r = c.post("/v2/admin/users", json={"email": "s@zoolstra.com",
                                            "role": "super_admin",
                                            "instance_ids": ["i1"]})
    assert r.json()["instances"] == [] and not sess.added


def test_create_with_password_returns_no_reset_link():
    c = client_as("super_admin", session=FakeSession())
    with patch("api.v2.admin_users.fb_auth.create_user",
               return_value=SimpleNamespace(uid="p1")) as mk, \
         patch("api.v2.admin_users.fb_auth.set_custom_user_claims"), \
         patch("api.v2.admin_users.fb_auth.generate_password_reset_link") as link:
        r = c.post("/v2/admin/users",
                   json={"email": "a@b.com", "password": "hunter22"})
    assert r.json()["password_reset_link"] is None
    assert mk.call_args.kwargs["password"] == "hunter22"
    link.assert_not_called()


def test_create_validates_before_touching_firebase():
    """A late failure would leave an account with no role and no scope."""
    sess = FakeSession(instances=_instances("i1"))
    c = client_as("super_admin", session=sess)
    with patch("api.v2.admin_users.fb_auth.create_user") as mk:
        bad_role = c.post("/v2/admin/users",
                          json={"email": "a@b.com", "role": "root"})
        bad_inst = c.post("/v2/admin/users",
                          json={"email": "a@b.com", "instance_ids": ["nope"]})
    assert bad_role.status_code == 422 and bad_inst.status_code == 422
    mk.assert_not_called()
    assert not sess.added and not sess.committed


def test_duplicate_email_is_409():
    import firebase_admin.auth as real_auth
    c = client_as("super_admin", session=FakeSession())
    with patch("api.v2.admin_users.fb_auth.create_user",
               side_effect=real_auth.EmailAlreadyExistsError("dup", None, None)):
        r = c.post("/v2/admin/users", json={"email": "a@b.com"})
    assert r.status_code == 409


def test_create_still_succeeds_when_reset_link_fails():
    """The account exists by then; failing the request would misreport reality."""
    c = client_as("super_admin", session=FakeSession())
    with patch("api.v2.admin_users.fb_auth.create_user",
               return_value=SimpleNamespace(uid="u9")), \
         patch("api.v2.admin_users.fb_auth.set_custom_user_claims"), \
         patch("api.v2.admin_users.fb_auth.generate_password_reset_link",
               side_effect=RuntimeError("smtp down")):
        r = c.post("/v2/admin/users", json={"email": "a@b.com"})
    assert r.status_code == 201 and r.json()["password_reset_link"] is None


# ── delete ───────────────────────────────────────────────────────────────────

def test_delete_removes_grants_then_the_user():
    sess = FakeSession(instances=[],
                       clinic_admins=[SimpleNamespace(uid="u1", instance_id="i1")])
    c = client_as("super_admin", session=sess)
    with patch("api.v2.admin_users.fb_auth.get_user",
               return_value=_fb_user("u1", "u@x.com")), \
         patch("api.v2.admin_users.fb_auth.delete_user") as rm:
        r = c.delete("/v2/admin/users/u1")
    assert r.status_code == 200 and r.json()["grants_removed"] == 1
    assert len(sess.deleted) == 1 and sess.committed
    rm.assert_called_once_with("u1")


def test_cannot_delete_self():
    c = client_as("super_admin", uid="me", session=FakeSession())
    with patch("api.v2.admin_users.fb_auth.delete_user") as rm:
        assert c.delete("/v2/admin/users/me").status_code == 400
    rm.assert_not_called()


def test_cannot_delete_an_instance_primary_contact():
    """Ownership is how _is_instance_member resolves access; deleting the owner
    silently strips access for that instance."""
    sess = FakeSession(instances=[SimpleNamespace(
        instance_id="i1", instance_name="Alpha", primary_contact_uid="owner")])
    c = client_as("super_admin", session=sess)
    with patch("api.v2.admin_users.fb_auth.delete_user") as rm:
        r = c.delete("/v2/admin/users/owner")
    assert r.status_code == 409 and "Alpha" in r.json()["detail"]
    rm.assert_not_called()
    assert not sess.deleted


def test_delete_unknown_user_is_404():
    import firebase_admin.auth as real_auth
    c = client_as("super_admin", session=FakeSession())
    with patch("api.v2.admin_users.fb_auth.get_user",
               side_effect=real_auth.UserNotFoundError("nope")), \
         patch("api.v2.admin_users.fb_auth.delete_user") as rm:
        assert c.delete("/v2/admin/users/ghost").status_code == 404
    rm.assert_not_called()


def test_delete_is_gated_to_super_admin():
    c = client_as("admin", session=FakeSession())
    with patch("api.v2.admin_users.fb_auth.delete_user") as rm:
        assert c.delete("/v2/admin/users/u1").status_code == 403
    rm.assert_not_called()


def test_create_is_gated_to_super_admin():
    c = client_as("admin", session=FakeSession())
    with patch("api.v2.admin_users.fb_auth.create_user") as mk:
        assert c.post("/v2/admin/users",
                      json={"email": "a@b.com"}).status_code == 403
    mk.assert_not_called()


def test_empty_set_revokes_all_grants():
    sess = FakeSession(
        instances=[SimpleNamespace(instance_id="i1", instance_name="A",
                                   primary_contact_uid=None)],
        clinic_admins=[SimpleNamespace(uid="u1", instance_id="i1")],
    )
    c = client_as("super_admin", session=sess)
    r = c.put("/v2/admin/users/u1/instances", json={"instance_ids": []})
    assert r.json()["removed"] == ["i1"] and len(sess.deleted) == 1
