"""Tests for the ``google_analytics`` campaign type (``/campaigns/**``, alembic 0033).

A GA4 property is registered against ONE default clinic (UNIQUE on
``ga4_property_id``, Jotform semantics) so a group's shared property is never
counted once per clinic. Covered here:
  - create → both list endpoints surface it as ``campaign_type = google_analytics``
  - the same property on a second clinic → 409 with the GA4-specific message
  - delete by type + id
  - the create endpoint's explicit unsupported-type guard (the bare ``else`` it
    replaced used to write a jotform_forms row for any unknown type)
  - the ETL-status payload lists active ``ga4_property_ids``

Runs against a real in-memory SQLite database (see test_jotform_locations.py for
why); only the tables these endpoints touch are created.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
import api.account.campaigns as campaigns
from api.core.db import get_session
from api.core.orm import (
    Clinic, GoogleAdsCampaign, GoogleAnalyticsProperty, Instance, InvocaCampaign,
    JotformForm, JotformFormLocation,
)
from api.deps import verify_token


@compiles(BigInteger, "sqlite")
def _bigint_as_integer_on_sqlite(type_, compiler, **kw):  # noqa: ARG001
    return "INTEGER"


PROP = "268146803"        # Calgary Hearing Aid — the registered one
OTHER_PROP = "331247915"  # Calgary Ear Centre


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for model in (Instance, Clinic, GoogleAdsCampaign, InvocaCampaign, JotformForm,
                  JotformFormLocation, GoogleAnalyticsProperty):
        model.__table__.create(engine)
    with Session(engine) as session:
        session.add(Instance(
            instance_id="I1", instance_name="Calgary Hearing Aid and Audiology",
            primary_contact_name="TBD", primary_contact_email="tbd@tbd.com",
            primary_contact_uid="U1", ga4_account_id="41550710",
        ))
        session.add_all([
            Clinic(clinic_id="C-HER", instance_id="I1", clinic_name="Heritage",
                   pms_type="none"),
            Clinic(clinic_id="C-SUN", instance_id="I1", clinic_name="Sunterra",
                   pms_type="none"),
            Instance(instance_id="I2", instance_name="Calgary Ear Centre",
                     primary_contact_name="x", primary_contact_email="x@x.com",
                     primary_contact_uid="U2"),
            Clinic(clinic_id="C-CEC", instance_id="I2", clinic_name="Calgary Ear Center",
                   pms_type="none"),
        ])
        session.commit()
        yield session


@pytest.fixture
def client(db):
    # Mirror get_session's commit/rollback contract on the shared test session:
    # a 409 leaves the session in a failed transaction, and the next request in
    # the same test must start clean exactly as it would in production.
    def _session():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "U1"}
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _add(c, clinic_id, prop_id, **extra):
    return c.post(f"/campaigns/{clinic_id}",
                  json={"campaign_type": "google_analytics",
                        "external_campaign_id": prop_id, **extra})


# ── create + list ─────────────────────────────────────────────────────────────

def test_create_then_both_list_endpoints_show_it(client):
    r = _add(client, "C-HER", PROP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["campaign_type"] == "google_analytics"
    new_id = body["id"]

    per_clinic = client.get("/campaigns/I1/C-HER").json()
    ga4 = [x for x in per_clinic if x["campaign_type"] == "google_analytics"]
    assert len(ga4) == 1
    assert ga4[0]["id"] == new_id
    assert ga4[0]["external_campaign_id"] == PROP
    assert ga4[0]["active"] is True
    # ``name`` / ``property_name`` ride along even when unset, so the SPA can
    # label the numeric id without a second lookup.
    assert "property_name" in ga4[0] and "name" in ga4[0]

    per_instance = client.get("/campaigns/I1").json()
    assert [x["external_campaign_id"] for x in per_instance
            if x["campaign_type"] == "google_analytics"] == [PROP]

    # Sibling clinic of the same instance does NOT see it — the property lives
    # on its default clinic only.
    assert client.get("/campaigns/I1/C-SUN").json() == []


def test_list_excludes_soft_deleted_clinics(client, db):
    _add(client, "C-HER", PROP)
    from datetime import datetime
    db.get(Clinic, "C-HER").deleted_at = datetime(2026, 9, 1)
    db.commit()
    assert [x for x in client.get("/campaigns/I1").json()
            if x["campaign_type"] == "google_analytics"] == []


# ── uniqueness ────────────────────────────────────────────────────────────────

def test_same_property_on_second_clinic_is_409_with_ga4_message(client):
    assert _add(client, "C-HER", PROP).status_code == 200
    r = _add(client, "C-SUN", PROP)
    assert r.status_code == 409
    assert "GA4 property already mapped" in r.json()["detail"]
    # …and across instances too: the uniqueness is global.
    r = _add(client, "C-CEC", PROP)
    assert r.status_code == 409


def test_same_property_on_same_clinic_is_409(client):
    assert _add(client, "C-HER", PROP).status_code == 200
    assert _add(client, "C-HER", PROP).status_code == 409


def test_different_properties_coexist(client):
    assert _add(client, "C-HER", PROP).status_code == 200
    assert _add(client, "C-CEC", OTHER_PROP).status_code == 200
    assert len([x for x in client.get("/campaigns/I2").json()
                if x["campaign_type"] == "google_analytics"]) == 1


# ── delete ────────────────────────────────────────────────────────────────────

def test_delete_by_type_and_id(client):
    new_id = _add(client, "C-HER", PROP).json()["id"]
    r = client.delete(f"/campaigns/google_analytics/{new_id}")
    assert r.status_code == 200
    assert client.get("/campaigns/I1/C-HER").json() == []
    # Gone is gone.
    assert client.delete(f"/campaigns/google_analytics/{new_id}").status_code == 404


def test_delete_unknown_type_is_400(client):
    r = client.delete("/campaigns/search_console/1")
    assert r.status_code == 400


# ── the unsupported-type guard ────────────────────────────────────────────────

def test_unknown_campaign_type_never_writes_a_row(client, db):
    """The Pydantic ``Literal`` rejects an unknown type as 422 before the handler
    runs; the handler's own ``else`` guard is the second line of defence. Either
    way the invariant that matters is: nothing lands in ANY registry table — the
    bare ``else`` this replaced wrote a jotform_forms row."""
    r = client.post("/campaigns/C-HER",
                    json={"campaign_type": "search_console", "external_campaign_id": "x"})
    assert r.status_code in (400, 422), r.text
    assert db.query(JotformForm).count() == 0
    assert db.query(GoogleAnalyticsProperty).count() == 0


def test_handler_guard_directly_rejects_unknown_type(db):
    """Bypass Pydantic to exercise the handler's own guard."""
    from types import SimpleNamespace
    from fastapi import HTTPException
    body = SimpleNamespace(campaign_type="search_console", external_campaign_id="x",
                           active=True)
    with pytest.raises(HTTPException) as exc:
        campaigns.add_campaign("C-HER", body, caller={"role": "super_admin", "uid": "U1"},
                               db=db)
    assert exc.value.status_code == 400
    assert db.query(JotformForm).count() == 0


# ── catalog ───────────────────────────────────────────────────────────────────

def test_catalog_without_account_id_is_empty_without_touching_bq(client, db, monkeypatch):
    # I2 has no ga4_account_id → [] and the SPA falls back to manual entry.
    def _boom(*a, **k):
        raise AssertionError("BigQuery must not be queried without an account id")
    monkeypatch.setattr(campaigns.bq_client, "query", _boom)
    assert client.get("/campaigns_catalog/google_analytics/I2").json() == []


def test_catalog_marks_linked_properties(client, db, monkeypatch):
    _add(client, "C-HER", PROP)

    class _Row(dict):
        def __getitem__(self, k):
            return dict.__getitem__(self, k)

    class _Job:
        def result(self):
            return [
                _Row(external_campaign_id=PROP, name="GA4 - Calgary Hearing - Zoolstra",
                     account_name="Calgary Hearing Aid - Main",
                     time_zone="America/Edmonton", currency_code="CAD",
                     primary_hostname="www.calgaryhearingaid.ca"),
                _Row(external_campaign_id="401829846", name="Calgary Hearing Aid - Main - GA4",
                     account_name="Calgary Hearing Aid - Main",
                     time_zone="America/Edmonton", currency_code="CAD",
                     primary_hostname="www.calgaryhearingaid.ca"),
            ]

    monkeypatch.setattr(campaigns.bq_client, "query", lambda *a, **k: _Job())
    rows = client.get("/campaigns_catalog/google_analytics/I1").json()
    by_id = {r["external_campaign_id"]: r for r in rows}
    assert by_id[PROP]["already_linked"] is True
    assert by_id[PROP]["linked_clinic_names"] == ["Heritage"]
    assert by_id["401829846"]["already_linked"] is False
    assert by_id[PROP]["time_zone"] == "America/Edmonton"
    assert by_id[PROP]["status"] == "active"
    assert by_id[PROP]["account_name"] == "Calgary Hearing Aid - Main"
    assert set(by_id[PROP]) >= {"external_campaign_id", "name", "status",
                                "already_linked", "linked_clinic_names"}


def test_instance_update_blank_ga4_account_id_is_ignored_not_422():
    from api.models import InstanceUpdate
    m = InstanceUpdate(ga4_account_id="   ", instance_name="X")
    assert m.ga4_account_id is None
    assert InstanceUpdate(ga4_account_id=" 41550710 ").ga4_account_id == "41550710"


# ── etl_status payload ────────────────────────────────────────────────────────

def test_etl_status_lists_active_ga4_property_ids(client, db, monkeypatch):
    """``GET /clinics/{clinic_id}/etl_status`` reports the clinic's linked ids per
    type; GA4 joins google_ads / invoca there. Inactive rows are not linked."""
    import api.account.clinics as clinics_mod
    _add(client, "C-HER", PROP)
    db.add(GoogleAnalyticsProperty(clinic_id="C-HER", ga4_property_id="999", active=False))
    db.commit()

    # The endpoint also counts BigQuery rows; stub the client so the test stays
    # offline and asserts only the Cloud SQL-derived ids.
    class _Job:
        def result(self):
            return iter(())

    class _BQ:
        project = "project-demo-2-482101"
        def query(self, *a, **k):
            return _Job()

    monkeypatch.setattr(clinics_mod, "bq_client", _BQ())
    r = client.get("/clinics/C-HER/etl_status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ga4_property_ids"] == [PROP]
    assert body["google_ads_campaign_ids"] == []
