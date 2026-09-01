"""
Tests for PMS config — api/account/pms_config.py.

Since alembic 0030 a PMS login is configured once per business and the location
map is the only per-clinic part. Every assertion here guards a way the ETL would
go quietly wrong rather than fail:

  * a location mapped twice would ingest its rows twice;
  * an active location with no clinic routes nowhere;
  * a retired location naming a clinic records a mapping a reader would act on;
  * a mapped clinic left at pms_type='none' is skipped by the ETL entirely;
  * a catch-all beside real locations would double-load every routable row.

Runs against a real in-memory SQLite session rather than a stubbed one — the
map's UNIQUE constraint, the nullable clinic_id from migration 0029, and the
replace-the-whole-map semantics are the substance of the feature, and a fake
session asserts none of them. Secret Manager is stubbed.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles

from api import app
from api.account import pms_config as mod
from api.core.db import get_session
from api.core.orm import (
    CATCH_ALL_LOCATION_KEY, Base, Clinic, Instance, InstancePmsConfig,
    PmsClinicLocation,
)
from api.deps import verify_token

# SQLite only auto-increments an INTEGER PRIMARY KEY, never a BIGINT one, so the
# map's surrogate key would come back NULL. Scoped to the sqlite dialect, so the
# BIGINT the production MySQL schema actually uses is untouched.
@compiles(BigInteger, "sqlite")
def _bigint_as_integer_on_sqlite(type_, compiler, **kw):
    return "INTEGER"


INSTANCE = "INST_CGY"
OTHER_INSTANCE = "INST_OTHER"
HERITAGE = "CLINIC_HERITAGE"
MARKET = "CLINIC_MARKET"
FOREIGN = "CLINIC_FOREIGN"

URL = f"/instances/{INSTANCE}/pms"


@pytest.fixture
def harness(monkeypatch):
    # StaticPool + check_same_thread: ":memory:" is per-connection, so the default
    # pool would hand the session a different (empty) database than create_all
    # built, and TestClient runs the request on another thread.
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = Session(engine)

    for iid, name in ((INSTANCE, "Calgary Hearing Aid"), (OTHER_INSTANCE, "Prairie")):
        session.add(Instance(instance_id=iid, instance_name=name))
    session.add(Clinic(clinic_id=HERITAGE, instance_id=INSTANCE,
                       clinic_name="Heritage", pms_type="blueprint", etl_enabled=True))
    session.add(Clinic(clinic_id=MARKET, instance_id=INSTANCE,
                       clinic_name="Market Mall", pms_type="none", etl_enabled=False))
    session.add(Clinic(clinic_id=FOREIGN, instance_id=OTHER_INSTANCE,
                       clinic_name="Prairie Hearing", pms_type="blueprint", etl_enabled=True))
    session.commit()

    written: list[tuple] = []
    monkeypatch.setattr(mod, "_write_pms_secret",
                        lambda scope, sid, pms, key, val: written.append((scope, sid, pms, key)))

    # Mirror get_session's contract (commit on success, roll back on error) so
    # the endpoints' reliance on the dependency to commit is actually exercised.
    def _session():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[verify_token] = lambda: {
        "role": "super_admin", "uid": "U1", "email": "admin@zoolstra.com"}

    yield TestClient(app), session, written

    app.dependency_overrides.clear()
    session.close()


def _map(**over):
    base = {"vendor_location_key": "1", "clinic_id": HERITAGE,
            "location_name": "Heritage Hill", "active": True,
            "prompt_for_location": False, "booking_user_id": None}
    base.update(over)
    return base


# ── GET ───────────────────────────────────────────────────────────────────────

def test_get_404_for_unknown_instance(harness):
    client, _, _ = harness
    assert client.get("/instances/NOPE/pms").status_code == 404


# ── Validation ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("locations,fragment", [
    ([_map(), _map(clinic_id=MARKET)], "appears twice"),
    ([_map(clinic_id=None)], "no clinic"),
    ([_map(active=False)], "retired"),
    ([_map(clinic_id=FOREIGN)], "not a clinic of this instance"),
    ([_map(vendor_location_key=" ")], "blank"),
])
def test_rejects_maps_that_would_misattribute(harness, locations, fragment):
    client, _, _ = harness
    resp = client.post(URL, json={"pms_type": "blueprint", "locations": locations})
    assert resp.status_code == 400, resp.json()
    assert fragment in resp.json()["detail"]


def test_rejects_unknown_config_and_secret_keys(harness):
    client, _, _ = harness
    assert client.post(URL, json={"pms_type": "blueprint",
                                  "config": {"bogus": "x"}}).status_code == 400
    assert client.post(URL, json={"pms_type": "blueprint",
                                  "secrets": {"bogus": "x"}}).status_code == 400


def test_rejects_primary_clinic_from_another_instance(harness):
    client, _, _ = harness
    resp = client.post(URL, json={"pms_type": "blueprint", "primary_clinic_id": FOREIGN})
    assert resp.status_code == 400
    assert "not a clinic of this instance" in resp.json()["detail"]


def test_counselear_config_uses_its_own_field_names(harness):
    """CounselEar's account fields are the practice's SFTP folder and login, which
    before 0030 were copied identically onto every clinic row of the practice."""
    client, session, written = harness
    url = f"/instances/{INSTANCE}/pms"
    resp = client.post(url, json={
        "pms_type": "counselear",
        "config": {"counselear_location_code": "105333",
                   "counselear_sftp_username": "virsono"},
        "locations": [{"vendor_location_key": "10797", "clinic_id": HERITAGE,
                       "location_name": "Heritage", "active": True}],
    })
    assert resp.status_code == 200, resp.json()

    body = client.get(f"{url}?pms_type=counselear").json()
    assert body["config"] == {"counselear_location_code": "105333",
                              "counselear_sftp_username": "virsono"}
    # Blueprint's fields are rejected for CounselEar and vice versa.
    assert client.post(url, json={"pms_type": "counselear",
                                  "config": {"aws_url": "s3://x"}}).status_code == 400
    assert client.post(url, json={"pms_type": "blueprint",
                                  "config": {"counselear_location_code": "1"}}
                       ).status_code == 400

    # No CounselEar secrets are accepted here: they are named after the SFTP
    # login, which is itself account config above, so nothing needs renaming.
    resp = client.post(url, json={"pms_type": "counselear",
                                  "secrets": {"sftp_password": "x"}})
    assert resp.status_code == 400
    assert "named after the SFTP login" in resp.json()["detail"]
    assert written == []


def test_the_two_vendors_are_configured_independently(harness):
    """One instance can hold both, and neither save disturbs the other."""
    client, _, _ = harness
    url = f"/instances/{INSTANCE}/pms"
    client.post(url, json={"pms_type": "blueprint",
                           "config": {"clinic_code": "AB_iai"}})
    client.post(url, json={"pms_type": "counselear",
                           "config": {"counselear_location_code": "105333"}})
    assert client.get(f"{url}?pms_type=blueprint").json()["config"]["clinic_code"] == "AB_iai"
    assert (client.get(f"{url}?pms_type=counselear").json()
            ["config"]["counselear_location_code"] == "105333")


def test_unsupported_pms_type_is_refused(harness):
    client, _, _ = harness
    for bad in ("none", "audit_data", "nonsense"):
        resp = client.post(f"/instances/{INSTANCE}/pms", json={"pms_type": bad})
        # Literal-typed field: pydantic rejects before the handler for these.
        assert resp.status_code in (400, 422), (bad, resp.status_code)


# ── The catch-all ─────────────────────────────────────────────────────────────

def test_catch_all_cannot_sit_beside_real_locations(harness):
    """Both would match the same rows, so every routable row would load twice."""
    client, _, _ = harness
    resp = client.post(f"/instances/{INSTANCE}/pms", json={
        "pms_type": "blueprint",
        "locations": [
            {"vendor_location_key": CATCH_ALL_LOCATION_KEY, "clinic_id": HERITAGE,
             "active": True},
            _map(vendor_location_key="2", clinic_id=MARKET),
        ]})
    assert resp.status_code == 400
    assert "cannot sit beside" in resp.json()["detail"]


def test_catch_all_cannot_be_retired(harness):
    """It is the account's only mapping — retiring it stops all ingest silently."""
    client, _, _ = harness
    resp = client.post(f"/instances/{INSTANCE}/pms", json={
        "pms_type": "blueprint",
        "locations": [{"vendor_location_key": CATCH_ALL_LOCATION_KEY,
                       "clinic_id": None, "active": False}]})
    assert resp.status_code == 400
    assert "cannot be retired" in resp.json()["detail"]


def test_catch_all_is_reported_as_such(harness):
    client, _, _ = harness
    client.post(f"/instances/{INSTANCE}/pms", json={
        "pms_type": "blueprint",
        "locations": [{"vendor_location_key": CATCH_ALL_LOCATION_KEY,
                       "clinic_id": HERITAGE, "active": True}]})
    row = client.get(f"/instances/{INSTANCE}/pms").json()["locations"][0]
    assert row["catch_all"] is True and row["clinic_name"] == "Heritage"


# ── Per-clinic view is read-only ──────────────────────────────────────────────

def test_clinic_view_resolves_the_account_and_offers_no_writes(harness):
    """There is no per-clinic PMS editor any more — only this explanation of
    where the clinic's data comes from."""
    client, _, _ = harness
    client.post(f"/instances/{INSTANCE}/pms", json={
        "pms_type": "blueprint",
        "config": {"clinic_code": "AB_iai"},
        "primary_clinic_id": HERITAGE,
        "locations": [_map(), _map(vendor_location_key="2", clinic_id=MARKET)]})

    body = client.get(f"/clinics/{HERITAGE}/pms").json()
    assert body["pms_type"] == "blueprint" and body["configured"] is True
    assert body["account"]["config"]["clinic_code"] == "AB_iai"
    assert body["account"]["is_primary"] is True
    # Only this clinic's mapping, not the whole account's.
    assert [l["vendor_location_key"] for l in body["locations"]] == ["1"]

    assert client.post(f"/clinics/{HERITAGE}/pms", json={}).status_code == 405
    assert client.delete(f"/clinics/{HERITAGE}/pms").status_code == 405


def test_clinic_view_for_a_clinic_with_no_pms(harness):
    client, _, _ = harness
    body = client.get(f"/clinics/{MARKET}/pms").json()
    assert body == {"clinic_id": MARKET, "pms_type": "none", "configured": False,
                    "account": None, "locations": []}


# ── Per-clinic knobs ride on the mapping ──────────────────────────────────────

def test_per_clinic_knobs_are_stored_on_the_mapping(harness):
    """prompt_for_location and booking_user_id are the only genuinely per-clinic
    PMS settings, so the mapping is where they live."""
    client, session, _ = harness
    client.post(f"/instances/{INSTANCE}/pms", json={
        "pms_type": "blueprint",
        "locations": [_map(prompt_for_location=True, booking_user_id=42)]})

    row = client.get(f"/instances/{INSTANCE}/pms").json()["locations"][0]
    assert row["prompt_for_location"] is True and row["booking_user_id"] == 42

    stored = session.scalars(select(PmsClinicLocation)).all()
    assert [(r.prompt_for_location, r.booking_user_id) for r in stored] == [(True, 42)]


# ── Import: clinics come from the PMS ─────────────────────────────────────────

def test_import_creates_a_clinic_per_location_and_drops_the_catch_all(harness):
    """The new onboarding step: an instance is provisioned with no clinics, and
    the clinics come from what the PMS reports."""
    client, session, _ = harness
    # Start from the migrated single-location state.
    client.post(f"/instances/{INSTANCE}/pms", json={
        "pms_type": "blueprint",
        "locations": [{"vendor_location_key": CATCH_ALL_LOCATION_KEY,
                       "clinic_id": HERITAGE, "active": True}]})

    resp = client.post(f"/instances/{INSTANCE}/pms/locations/import", json={
        "pms_type": "blueprint",
        "locations": [
            {"vendor_location_key": "1", "location_name": "Heritage Hill",
             "clinic_id": HERITAGE},
            {"vendor_location_key": "4", "location_name": "Marlborough Mall",
             "address": "230-433 Marlborough Way NE", "country": "CA",
             "time_zone": "Canada/Mountain"},
        ]})
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert [c["clinic_name"] for c in body["clinics_created"]] == ["Marlborough Mall"]
    assert len(body["locations_mapped"]) == 2
    assert any("catch-all" in w for w in body["warnings"])

    after = client.get(f"/instances/{INSTANCE}/pms").json()
    assert sorted(l["vendor_location_key"] for l in after["locations"]) == ["1", "4"]
    # The created clinic is a real one: right instance, adopted into the PMS.
    created = next(c for c in after["clinics"] if c["clinic_name"] == "Marlborough Mall")
    assert created["pms_type"] == "blueprint"

    # Idempotent — re-importing adopts rather than duplicating.
    again = client.post(f"/instances/{INSTANCE}/pms/locations/import", json={
        "pms_type": "blueprint",
        "locations": [{"vendor_location_key": "4",
                       "location_name": "Marlborough Mall"}]})
    assert again.status_code == 200
    assert again.json()["clinics_created"] == []


def test_import_refuses_an_unnamed_location_with_no_clinic_name(harness):
    """Blueprint leaves some locations unnamed; guessing a clinic name from
    nothing would create an unidentifiable clinic."""
    client, _, _ = harness
    resp = client.post(f"/instances/{INSTANCE}/pms/locations/import", json={
        "pms_type": "blueprint",
        "locations": [{"vendor_location_key": "6"}]})
    assert resp.status_code == 400
    assert "no clinic and no name" in resp.json()["detail"]


def test_import_refuses_the_catch_all_and_duplicates(harness):
    client, _, _ = harness
    url = f"/instances/{INSTANCE}/pms/locations/import"
    assert client.post(url, json={"pms_type": "blueprint", "locations": [
        {"vendor_location_key": CATCH_ALL_LOCATION_KEY, "clinic_id": HERITAGE}]}
        ).status_code == 400
    assert client.post(url, json={"pms_type": "blueprint", "locations": [
        {"vendor_location_key": "1", "clinic_id": HERITAGE},
        {"vendor_location_key": "1", "clinic_id": MARKET}]}).status_code == 400


# ── Write path ────────────────────────────────────────────────────────────────

def test_save_maps_locations_and_adopts_mapped_clinic_into_the_pms(harness):
    client, session, _ = harness
    resp = client.post(URL, json={
        "pms_type": "blueprint",
        "config": {"clinic_code": "AB_iai", "aws_url": "s3://feed/StandardDataFeed.zip"},
        "primary_clinic_id": HERITAGE,
        "locations": [
            _map(),
            _map(vendor_location_key="2", clinic_id=MARKET, location_name="Market Mall"),
            # Closed site: recorded, not ingested, and naming no clinic.
            {"vendor_location_key": "6", "clinic_id": None,
             "location_name": "Strathmore", "active": False},
        ],
    })
    assert resp.status_code == 200, resp.json()

    # Market Mall was pms_type='none'; the ETL scopes the account to clinics of
    # the right PMS, so the mapping would have stored fine and ingested nothing.
    assert resp.json()["clinics_updated"] == [{
        "clinic_id": MARKET, "clinic_name": "Market Mall",
        "pms_type_from": "none", "pms_type_to": "blueprint"}]
    assert session.get(Clinic, MARKET).pms_type == "blueprint"
    # etl_enabled is NOT flipped — it also gates Google Ads and Invoca ingest.
    assert session.get(Clinic, MARKET).etl_enabled is False

    body = client.get(URL).json()
    assert body["configured"] is True
    assert body["primary_clinic_id"] == HERITAGE
    assert [(l["vendor_location_key"], l["clinic_name"], l["active"])
            for l in body["locations"]] == [
        ("1", "Heritage", True), ("2", "Market Mall", True), ("6", None, False)]
    assert [c["clinic_name"] for c in body["mapped_not_etl_enabled"]] == ["Market Mall"]


def test_locations_replace_wholesale_but_omitting_them_leaves_the_map_alone(harness):
    """Saving credentials must not silently drop the map, and clearing it must
    be an explicit empty list rather than a side effect of a partial save."""
    client, _, _ = harness
    client.post(URL, json={"pms_type": "blueprint", "locations": [_map()]})

    client.post(URL, json={"pms_type": "blueprint", "config": {"clinic_code": "X"}})
    body = client.get(URL).json()
    assert len(body["locations"]) == 1 and body["config"]["clinic_code"] == "X"

    # Replace: location 1 disappears, 2 arrives.
    client.post(URL, json={"pms_type": "blueprint", "locations": [
        _map(vendor_location_key="2", clinic_id=MARKET)]})
    body = client.get(URL).json()
    assert [l["vendor_location_key"] for l in body["locations"]] == ["2"]

    client.post(URL, json={"pms_type": "blueprint", "locations": []})
    assert client.get(URL).json()["locations"] == []


def test_secrets_go_to_the_instance_scope_not_the_clinic(harness):
    client, _, written = harness
    resp = client.post(URL, json={
        "pms_type": "blueprint",
        "secrets": {"aws_access_key_id": "AKIA", "zip_password": "", "api_key": "k"},
    })
    assert resp.status_code == 200
    # Blank values are skipped so a partial save can rotate one credential.
    assert sorted(written) == [
        ("instance", INSTANCE, "blueprint", "api_key"),
        ("instance", INSTANCE, "blueprint", "aws_access_key_id"),
    ]


def test_a_blank_config_value_never_clears_a_stored_one(harness):
    """The accident that destroyed one account's config: a Save on a form whose
    fields had not loaded, treated as "clear these"."""
    client, _, _ = harness
    url = f"/instances/{INSTANCE}/pms"
    client.post(url, json={"pms_type": "blueprint", "config": {
        "clinic_code": "AB_iai", "api_url": "https://bp/rest/hello",
        "aws_url": "s3://feed/StandardDataFeed.zip"}})

    # Blank strings, and an omitted key, must both leave the stored value alone.
    client.post(url, json={"pms_type": "blueprint",
                           "config": {"clinic_code": "", "api_url": "   "}})
    cfg = client.get(url).json()["config"]
    assert cfg == {"clinic_code": "AB_iai", "api_url": "https://bp/rest/hello",
                   "aws_url": "s3://feed/StandardDataFeed.zip"}

    # A real value still overwrites.
    client.post(url, json={"pms_type": "blueprint", "config": {"clinic_code": "AB_new"}})
    assert client.get(url).json()["config"]["clinic_code"] == "AB_new"


def test_an_empty_first_save_is_refused_rather_than_stored(harness):
    """A row with every field NULL reads as configured everywhere while ingesting
    nothing — worse than reporting nothing at all."""
    client, _, _ = harness
    url = f"/instances/{INSTANCE}/pms"
    resp = client.post(url, json={"pms_type": "blueprint", "config": {}})
    assert resp.status_code == 400
    assert "Nothing to save" in resp.json()["detail"]
    assert client.get(url).json()["configured"] is False


def test_configured_means_usable_not_merely_present(harness):
    """An all-NULL row can still arrive from a migration that had no source to
    copy from, so the flag is computed rather than trusted."""
    client, session, _ = harness
    session.add(InstancePmsConfig(instance_id=INSTANCE, pms_type="blueprint"))
    session.commit()

    body = client.get(f"/instances/{INSTANCE}/pms").json()
    assert body["configured"] is False
    assert client.get(f"/clinics/{HERITAGE}/pms").json()["configured"] is False


# ── Delete ────────────────────────────────────────────────────────────────────

def test_delete_account_config_removes_the_whole_map(harness):
    client, _, _ = harness
    client.post(URL, json={"pms_type": "blueprint", "locations": [
        _map(), _map(vendor_location_key="2", clinic_id=MARKET)]})

    resp = client.delete(URL)
    assert resp.status_code == 200
    assert resp.json()["locations_removed"] == 2

    body = client.get(URL).json()
    assert body["configured"] is False and body["locations"] == []
    # Clinics keep their pms_type — deleting the account config must not quietly
    # unwire them from PMS ingest altogether.
    assert client.get(URL).json()["clinics"][0]["pms_type"] == "blueprint"
    assert client.delete(URL).status_code == 404
