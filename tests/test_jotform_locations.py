"""Tests for the Jotform location map API (``/campaigns/**/locations``).

The map is what lets ONE form serve a whole group: the webhook URL names a
single clinic, and these rows re-point each submission at the site the patient
chose. Covered here:
  - instance listing: scope derived from the presence of a map, and
    ``needs_location_map`` for the misconfiguration that matters (a form that
    asks for a location but routes everything to one clinic)
  - unmapped / stale options against the form's live option list, and the
    null-vs-empty distinction when Jotform cannot be reached
  - PUT: replace semantics, and each validation that stops a map from quietly
    misrouting

Runs against a real in-memory SQLite database rather than a fake session: the
endpoints' behaviour is mostly in their SQL and in the UNIQUE constraint, and a
hand-rolled ``scalars`` stub would assert against the fake instead of the query.
The Jotform HTTP call is the only thing patched.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from api import app
import api.account.campaigns as campaigns
from api.core.db import get_session
from api.core.orm import (
    Clinic, Instance, JotformForm, JotformFormLocation,
)
from api.deps import verify_token


# SQLite only autoincrements an INTEGER PRIMARY KEY, never a BIGINT one, so the
# models' BigInteger surrogate keys come back NOT NULL on insert. Rendering
# BigInteger as INTEGER for the SQLite dialect is test-only and changes nothing
# about the MySQL schema these models actually describe.
@compiles(BigInteger, "sqlite")
def _bigint_as_integer_on_sqlite(type_, compiler, **kw):  # noqa: ARG001
    return "INTEGER"


FORM = "262174010008038"
OTHER_FORM = "261103802261039"

BURLINGTON = "Burlington: 11 - 1960 Appleby Line"
OAKVILLE = "Oakville: 240 North Service Road West Oakville"
KINGSTON = "Limestone Hearing Care Centre (Kingston): 102 - 817 Bayridge Drive"
LIVE_OPTIONS = [BURLINGTON, OAKVILLE, KINGSTON]


@pytest.fixture
def db() -> Session:
    """Only the tables these endpoints touch — creating every table would drag in
    the MySQL-specific JSON columns other models use, which SQLite cannot build."""
    # TestClient runs the app in its own thread against this same connection,
    # which SQLite refuses by default — StaticPool keeps it to one connection and
    # check_same_thread lets that thread use it.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for model in (Instance, Clinic, JotformForm, JotformFormLocation):
        model.__table__.create(engine)
    with Session(engine) as session:
        session.add(Instance(
            instance_id="I1", instance_name="Sense of Hearing",
            primary_contact_name="TBD", primary_contact_email="tbd@tbd.com",
            primary_contact_uid="U1",
        ))
        session.add_all([
            Clinic(clinic_id="C-BUR", instance_id="I1", clinic_name="Burlington",
                   pms_type="none"),
            Clinic(clinic_id="C-OAK", instance_id="I1", clinic_name="Oakville",
                   pms_type="none"),
            Clinic(clinic_id="C-KIN", instance_id="I1", clinic_name="Kingston",
                   pms_type="none"),
            # A clinic of a DIFFERENT business, to prove the map cannot reach it.
            Instance(instance_id="I2", instance_name="Other Co",
                     primary_contact_name="x", primary_contact_email="x@x.com",
                     primary_contact_uid="U2"),
            Clinic(clinic_id="C-OTHER", instance_id="I2", clinic_name="Elsewhere",
                   pms_type="none"),
        ])
        session.add(JotformForm(clinic_id="C-BUR", jotform_form_id=FORM,
                                form_title="Sense of Hearing - Appointment Request Form"))
        session.commit()
        yield session


@pytest.fixture
def client(db, monkeypatch):
    """TestClient with the DB bound and the Jotform option fetch stubbed.

    ``options`` is mutable so a test can shrink the live list (an option renamed
    in the builder) or set it to None (Jotform unreachable).
    """
    options: dict[str, list[str] | None] = {"value": list(LIVE_OPTIONS)}
    monkeypatch.setattr(campaigns, "_jotform_location_options",
                        lambda form_id: options["value"])
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "U1"}
    yield TestClient(app, raise_server_exceptions=False), options
    app.dependency_overrides.clear()


def _put(c, locations, form=FORM):
    return c.put(f"/campaigns/jotform/{form}/locations", json={"locations": locations})


# ── Scope: instance-level vs clinic-level ─────────────────────────────────────

def test_form_with_no_map_is_clinic_scoped_and_flagged(client):
    c, _ = client
    body = c.get("/campaigns/I1/jotform/locations").json()

    assert [f["jotform_form_id"] for f in body["forms"]] == [FORM]
    form = body["forms"][0]
    assert form["scope"] == "clinic"
    # It offers locations but routes everything to one clinic — the whole point
    # of the flag.
    assert form["needs_location_map"] is True
    assert form["routes_to"] == ["C-BUR"]
    assert sorted(form["unmapped_options"]) == sorted(LIVE_OPTIONS)


def test_form_with_a_map_is_instance_scoped(client):
    c, _ = client
    assert _put(c, [
        {"option_value": BURLINGTON, "clinic_id": "C-BUR"},
        {"option_value": OAKVILLE, "clinic_id": "C-OAK"},
        {"option_value": KINGSTON, "clinic_id": "C-KIN"},
    ]).status_code == 200

    form = c.get("/campaigns/I1/jotform/locations").json()["forms"][0]
    assert form["scope"] == "instance"
    assert form["needs_location_map"] is False
    assert form["routes_to"] == ["C-BUR", "C-KIN", "C-OAK"]
    assert form["unmapped_options"] == []
    assert {l["option_value"]: l["clinic_name"] for l in form["locations"]} == {
        BURLINGTON: "Burlington", OAKVILLE: "Oakville", KINGSTON: "Kingston",
    }


def test_a_form_offering_no_locations_is_clinic_scoped_without_the_flag(client):
    # A single-site contact form. Nothing to map, so nothing to warn about.
    c, options = client
    options["value"] = []
    form = c.get("/campaigns/I1/jotform/locations").json()["forms"][0]
    assert form["scope"] == "clinic"
    assert form["needs_location_map"] is False


def test_default_clinic_is_reported_separately_from_the_map(client):
    c, _ = client
    _put(c, [{"option_value": OAKVILLE, "clinic_id": "C-OAK"}])
    form = c.get("/campaigns/I1/jotform/locations").json()["forms"][0]
    # The webhook still names Burlington; that is the fallback, not the route.
    assert form["default_clinic_id"] == "C-BUR"
    assert form["default_clinic_name"] == "Burlington"
    assert form["scope"] == "instance"


# ── Drift against the live option list ───────────────────────────────────────

def test_option_renamed_in_the_builder_is_reported_stale(client):
    c, options = client
    _put(c, [{"option_value": OAKVILLE, "clinic_id": "C-OAK"}])
    # The clinic renamed the option; the stored row can never match again.
    options["value"] = [BURLINGTON, "Oakville: 240 North Service Rd W", KINGSTON]

    form = c.get(f"/campaigns/jotform/{FORM}/locations").json()
    stale = [l["option_value"] for l in form["locations"] if l["stale"]]
    assert stale == [OAKVILLE]
    assert "Oakville: 240 North Service Rd W" in form["unmapped_options"]


def test_unreachable_jotform_reports_null_not_empty(client):
    # An outage must never read as "everything is mapped".
    c, options = client
    options["value"] = None
    form = c.get(f"/campaigns/jotform/{FORM}/locations").json()
    assert form["unmapped_options"] is None
    assert all(l["stale"] is False for l in form["locations"])


# ── PUT semantics and validation ─────────────────────────────────────────────

def test_put_replaces_the_map_and_deletes_omitted_options(client):
    c, _ = client
    _put(c, [{"option_value": BURLINGTON, "clinic_id": "C-BUR"},
             {"option_value": OAKVILLE, "clinic_id": "C-OAK"}])

    resp = _put(c, [{"option_value": OAKVILLE, "clinic_id": "C-OAK"}])
    assert resp.json() == {"status": "success", "mapped": 1, "removed": 1}
    form = c.get(f"/campaigns/jotform/{FORM}/locations").json()
    assert [l["option_value"] for l in form["locations"]] == [OAKVILLE]


def test_put_repoints_an_existing_option(client):
    c, _ = client
    _put(c, [{"option_value": OAKVILLE, "clinic_id": "C-OAK"}])
    _put(c, [{"option_value": OAKVILLE, "clinic_id": "C-KIN"}])
    form = c.get(f"/campaigns/jotform/{FORM}/locations").json()
    assert form["locations"][0]["clinic_name"] == "Kingston"


def test_an_option_may_be_mapped_with_no_clinic_yet(client):
    # The rollout state: the group's form lists a site whose clinic does not
    # exist yet. Legal here, unlike the PMS map.
    c, _ = client
    assert _put(c, [{"option_value": OAKVILLE, "clinic_id": None}]).status_code == 200
    form = c.get(f"/campaigns/jotform/{FORM}/locations").json()
    assert form["locations"][0]["clinic_id"] is None
    assert form["scope"] == "instance"
    # It is mapped, so it is not "unmapped" — but it routes nowhere yet.
    assert OAKVILLE not in form["unmapped_options"]
    assert form["routes_to"] == ["C-BUR"]


def test_duplicate_option_is_rejected_as_400_not_a_500(client):
    c, _ = client
    resp = _put(c, [{"option_value": OAKVILLE, "clinic_id": "C-OAK"},
                    {"option_value": OAKVILLE, "clinic_id": "C-KIN"}])
    assert resp.status_code == 400
    assert "twice" in resp.json()["detail"]


def test_option_cannot_route_to_another_businesss_clinic(client):
    c, _ = client
    resp = _put(c, [{"option_value": OAKVILLE, "clinic_id": "C-OTHER"}])
    assert resp.status_code == 400
    assert "not a clinic of this instance" in resp.json()["detail"]


def test_retired_option_must_not_name_a_clinic(client):
    c, _ = client
    resp = _put(c, [{"option_value": OAKVILLE, "clinic_id": "C-OAK", "active": False}])
    assert resp.status_code == 400
    assert "retired" in resp.json()["detail"]


def test_whitespace_only_option_value_is_rejected(client):
    # Pydantic's min_length=1 is satisfied by spaces, so the endpoint's own blank
    # check is what catches this — as a 400 with a reason, not a 422.
    c, _ = client
    resp = _put(c, [{"option_value": "   ", "clinic_id": "C-OAK"}])
    assert resp.status_code == 400
    assert "blank" in resp.json()["detail"]


def test_empty_option_value_is_rejected_by_validation(client):
    c, _ = client
    assert _put(c, [{"option_value": "", "clinic_id": "C-OAK"}]).status_code == 422


def test_inactive_option_is_stored_but_does_not_route(client):
    c, _ = client
    assert _put(c, [{"option_value": OAKVILLE, "clinic_id": None,
                     "active": False}]).status_code == 200
    form = c.get(f"/campaigns/jotform/{FORM}/locations").json()
    assert form["locations"][0]["active"] is False
    assert form["routes_to"] == ["C-BUR"]


def test_unregistered_form_is_404_with_the_next_step(client):
    c, _ = client
    resp = c.get(f"/campaigns/jotform/{OTHER_FORM}/locations")
    assert resp.status_code == 404
    assert "not registered" in resp.json()["detail"]


def test_instance_listing_excludes_another_businesss_forms(client, db):
    c, _ = client
    db.add(JotformForm(clinic_id="C-OTHER", jotform_form_id=OTHER_FORM,
                       form_title="Someone else's form"))
    db.commit()

    ours = c.get("/campaigns/I1/jotform/locations").json()
    assert [f["jotform_form_id"] for f in ours["forms"]] == [FORM]
    assert [c["clinic_name"] for c in ours["clinics"]] == [
        "Burlington", "Kingston", "Oakville",
    ]
