"""
Tests for `/v2/auth/set-claims` (api/v2/auth.py) and the mart full-history guard
it ships alongside.

set-claims was re-homed from the Next app's only writer of Firebase role claims;
its logic decides who becomes super_admin, so the branch table is worth pinning
exactly. `firebase_admin.auth.set_custom_user_claims` is patched — no test may
write a real claim.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import app
from api.deps import verify_token


def client_as(**claims) -> TestClient:
    app.dependency_overrides[verify_token] = lambda: claims
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


# ── role assignment ──────────────────────────────────────────────────────────

def test_zoolstra_email_is_promoted_to_super_admin():
    c = client_as(uid="u1", email="will@zoolstra.com")
    with patch("api.v2.auth.fb_auth.set_custom_user_claims") as setter:
        r = c.post("/v2/auth/set-claims")
    assert r.status_code == 200
    assert r.json() == {"role": "super_admin", "updated": True}
    setter.assert_called_once_with("u1", {"role": "super_admin"})


def test_existing_super_admin_is_not_rewritten():
    """Idempotence matters: this runs on every sign-in."""
    c = client_as(uid="u1", email="will@zoolstra.com", role="super_admin")
    with patch("api.v2.auth.fb_auth.set_custom_user_claims") as setter:
        r = c.post("/v2/auth/set-claims")
    assert r.json() == {"role": "super_admin", "updated": False}
    setter.assert_not_called()


def test_new_external_user_gets_viewer():
    c = client_as(uid="u2", email="staff@someclinic.com")
    with patch("api.v2.auth.fb_auth.set_custom_user_claims") as setter:
        r = c.post("/v2/auth/set-claims")
    assert r.json() == {"role": "viewer", "updated": True}
    setter.assert_called_once_with("u2", {"role": "viewer"})


@pytest.mark.parametrize("role", ["super_admin", "admin", "viewer"])
def test_manually_assigned_roles_are_preserved(role):
    """Granted by hand via PATCH /v2/admin/users/{uid}/role; a sign-in must never
    downgrade any of them to viewer.

    Regression: the preserve-list was ("admin", "viewer"), so an external
    super_admin was demoted to viewer on their next sign-in. The claim was
    written correctly and then clobbered before the dashboard rendered, which
    made the admin UI look like it had not saved.
    """
    c = client_as(uid="u3", email="staff@someclinic.com", role=role)
    with patch("api.v2.auth.fb_auth.set_custom_user_claims") as setter:
        r = c.post("/v2/auth/set-claims")
    assert r.json() == {"role": role, "updated": False}
    setter.assert_not_called()


def test_preserve_list_covers_every_assignable_role():
    """Anything a super_admin can grant must survive sign-in.

    ASSIGNABLE_ROLES is duplicated rather than imported (see api/v2/auth.py), so
    pin the two together here — adding a role to the admin endpoint without
    adding it to set-claims would silently reintroduce the demotion bug.
    """
    from api.v2.admin_users import ROLES
    from api.v2.auth import ASSIGNABLE_ROLES
    assert set(ASSIGNABLE_ROLES) == set(ROLES)


def test_missing_email_falls_through_to_viewer():
    """A token with no email must not crash, and must not be promoted."""
    c = client_as(uid="u4")
    with patch("api.v2.auth.fb_auth.set_custom_user_claims") as setter:
        r = c.post("/v2/auth/set-claims")
    assert r.json() == {"role": "viewer", "updated": True}
    setter.assert_called_once_with("u4", {"role": "viewer"})


def test_lookalike_domain_is_not_promoted():
    """`endswith("@zoolstra.com")` must not match a domain that merely ends in it."""
    c = client_as(uid="u5", email="attacker@notzoolstra.com")
    with patch("api.v2.auth.fb_auth.set_custom_user_claims") as setter:
        r = c.post("/v2/auth/set-claims")
    assert r.json()["role"] == "viewer"
    setter.assert_called_once_with("u5", {"role": "viewer"})


def test_caller_cannot_name_another_uid_or_role():
    """The endpoint takes no body: uid and role come only from the verified token.

    A body must be ignored rather than honoured, or this becomes an escalation
    endpoint.
    """
    c = client_as(uid="victim", email="staff@someclinic.com")
    with patch("api.v2.auth.fb_auth.set_custom_user_claims") as setter:
        r = c.post("/v2/auth/set-claims",
                   json={"uid": "attacker", "role": "super_admin"})
    assert r.json() == {"role": "viewer", "updated": True}
    setter.assert_called_once_with("victim", {"role": "viewer"})


# ── mart full-history guard ──────────────────────────────────────────────────
#
# Regression tests for a real defect: the guard originally inferred coverage from
# MIN(call_ts), which cannot distinguish a delta-built mart from a clinic that
# simply has no older data — so 9 of 13 clinics were pinned to v1 permanently.

import datetime as dt  # noqa: E402
import json  # noqa: E402

from api.v2 import marts  # noqa: E402


class _FakeDB:
    """Minimal stand-in for a Session: returns one scalar."""
    def __init__(self, value, raises=False):
        self._value, self._raises = value, raises

    def execute(self, *_a, **_k):
        if self._raises:
            raise RuntimeError("connection lost")
        return self

    def scalar(self):
        return self._value


def _wm(floor):
    return json.dumps({"blueprint_snapshot_date": "2026-08-05",
                       "history_floor": {"call_facts": floor}})


def test_guard_true_when_build_scanned_to_the_floor():
    db = _FakeDB(_wm("2025-12-01T00:00:00Z"))
    assert marts.mart_covers_full_history(db, "c1") is True


def test_guard_true_for_new_clinic_with_no_old_data():
    """The bug this replaces: a June-onboarded clinic has no December calls, but
    a full build DID scan to the floor, so its mart is complete."""
    db = _FakeDB(_wm(marts.MIN_WINDOW_DATE.isoformat() + "T00:00:00Z"))
    assert marts.mart_covers_full_history(db, "c1") is True


def test_guard_false_after_a_delta_build():
    recent = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    db = _FakeDB(_wm(recent + "T00:00:00Z"))
    assert marts.mart_covers_full_history(db, "c1") is False


def test_guard_fails_closed_on_missing_metadata():
    assert marts.mart_covers_full_history(_FakeDB(None), "c1") is False


def test_guard_fails_closed_on_pre_upgrade_build():
    """A mart built before history_floor existed must defer to v1."""
    db = _FakeDB(json.dumps({"blueprint_snapshot_date": "2026-08-05"}))
    assert marts.mart_covers_full_history(db, "c1") is False


def test_guard_fails_closed_on_db_error():
    assert marts.mart_covers_full_history(_FakeDB(None, raises=True), "c1") is False


def test_guard_accepts_dict_watermarks():
    """The Cloud SQL column is JSON; some drivers hand back a dict already."""
    db = _FakeDB({"history_floor": {"call_facts": "2025-12-01T00:00:00Z"}})
    assert marts.mart_covers_full_history(db, "c1") is True
