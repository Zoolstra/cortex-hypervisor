"""
Setup readiness — api/account/readiness.py.

The endpoint exists because five separate onboarding failures all had the shape
"the system knew and the screen didn't say". These tests pin the checks, but they
spend most of their weight on the FALSE POSITIVES, because a readiness list that
cries wolf is worse than none: the first thing an operator learns is to ignore it.
Three states look broken and are not — a catch-all mapping, an API-only account,
and a map whose only change was migration 0030 back-filling it.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles

from api.account import readiness as mod
from api.core.orm import (
    CATCH_ALL_LOCATION_KEY, Base, Clinic, Instance, InstancePmsConfig,
    InvocaCampaign, PmsClinicLocation,
)


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(type_, compiler, **kw):
    return "INTEGER"


INSTANCE = "INST"
CALLER = {"role": "super_admin", "uid": "u"}


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = Session(engine)
    s.add(Instance(instance_id=INSTANCE, instance_name="Multi Co"))
    s.commit()
    # Credentials and BigQuery are stubbed: this module's job is composing rules,
    # not reaching Secret Manager or a warehouse.
    monkeypatch.setattr(mod, "_secret_present", lambda *a, **k: True)
    monkeypatch.setattr(mod, "_feed_state", lambda ids: {"snapshot": None, "locations": None})
    yield s
    s.close()


def _clinic(db, name, etl=True, pms="blueprint"):
    c = Clinic(clinic_id=f"C_{name}", instance_id=INSTANCE, clinic_name=name,
               pms_type=pms, etl_enabled=etl)
    db.add(c); db.commit()
    return c


def _config(db, **kw):
    cfg = InstancePmsConfig(instance_id=INSTANCE, pms_type="blueprint", **kw)
    db.add(cfg); db.commit()
    return cfg


def _map(db, key, clinic_id, when=None):
    row = PmsClinicLocation(instance_id=INSTANCE, pms_type="blueprint",
                            vendor_location_key=key, clinic_id=clinic_id, active=True)
    db.add(row); db.commit()
    if when:
        row.updated_at = when
        db.commit()
    return row


def _by_key(result):
    return {c["key"]: c for c in result["checks"]}


# ── The failures it exists to catch ───────────────────────────────────────────

def test_a_location_in_the_feed_with_no_mapping_blocks(db, monkeypatch):
    _config(db, api_url="https://bp", aws_url="s3://feed")
    a = _clinic(db, "Heritage")
    _map(db, "1", a.clinic_id)
    monkeypatch.setattr(mod, "_feed_state", lambda ids: {
        "snapshot": "2026-08-26", "locations": {"1": "Heritage Hill", "6": "Strathmore"}})

    r = mod.get_instance_readiness(INSTANCE, CALLER, db)
    chk = _by_key(r)["locations_mapped"]
    assert chk["status"] == "blocked"
    assert chk["items"] == ["6 (Strathmore)"]
    # Both resolutions offered — pushing "map it" alone invents a clinic for a
    # site that closed.
    assert "retired" in chk["consequence"]
    assert r["overall"] == "blocked"


def test_etl_off_on_a_mapped_clinic_blocks_and_says_why_it_looks_empty(db):
    _config(db, api_url="https://bp")
    a = _clinic(db, "Heritage")
    b = _clinic(db, "Market Mall", etl=False)
    _map(db, "1", a.clinic_id)
    _map(db, "2", b.clinic_id)

    chk = _by_key(mod.get_instance_readiness(INSTANCE, CALLER, db))["etl_enabled"]
    assert chk["status"] == "blocked"
    assert chk["items"] == ["Market Mall"]
    assert "looks empty" in chk["consequence"]


def test_a_stale_sync_blocks_because_attribution_is_out_of_date(db, monkeypatch):
    _config(db, api_url="https://bp", aws_url="s3://feed")
    a, b = _clinic(db, "Heritage"), _clinic(db, "Market Mall")
    _map(db, "1", a.clinic_id, when=datetime(2026, 8, 26, 12, 0))
    _map(db, "2", b.clinic_id, when=datetime(2026, 8, 26, 12, 0))
    monkeypatch.setattr(mod, "_feed_state", lambda ids: {
        "snapshot": "2026-08-25", "locations": {"1": "A", "2": "B"}})

    chk = _by_key(mod.get_instance_readiness(INSTANCE, CALLER, db))["sync_fresh"]
    assert chk["status"] == "blocked"
    assert "2026-08-25" in chk["detail"]


def test_a_clinic_with_no_campaign_warns_rather_than_blocks(db):
    """Acquisition figures read zero rather than unmeasured — wrong, but the PMS
    data still flows, so it must not sit at the same severity as a broken feed."""
    _config(db, api_url="https://bp")
    a = _clinic(db, "Heritage")
    _map(db, "1", a.clinic_id)
    chk = _by_key(mod.get_instance_readiness(INSTANCE, CALLER, db))["campaigns"]
    assert chk["status"] == "warn" and chk["items"] == ["Heritage"]

    db.add(InvocaCampaign(clinic_id=a.clinic_id, invoca_campaign_id="1", active=True))
    db.commit()
    assert _by_key(mod.get_instance_readiness(INSTANCE, CALLER, db))["campaigns"]["status"] == "ok"


# ── The false positives, which matter more ────────────────────────────────────

def test_a_catch_all_covers_every_feed_location(db, monkeypatch):
    """"*" means every row belongs to this clinic, so nothing the feed reports can
    be unmapped. Comparing ids against it flagged all 13 of Alto's locations."""
    _config(db, api_url="https://bp", aws_url="s3://feed")
    a = _clinic(db, "Alto")
    _map(db, CATCH_ALL_LOCATION_KEY, a.clinic_id)
    monkeypatch.setattr(mod, "_feed_state", lambda ids: {
        "snapshot": "2026-08-26",
        "locations": {str(i): f"Site {i}" for i in range(1, 14)}})

    r = mod.get_instance_readiness(INSTANCE, CALLER, db)
    assert _by_key(r)["locations_mapped"]["status"] == "ok"
    # No campaigns in this fixture, so `warn` is legitimate — what matters is that
    # a correctly-mapped account is never *blocked*.
    assert r["blocked_count"] == 0


def test_a_catch_all_map_never_reports_a_stale_sync(db, monkeypatch):
    """One destination means no edit can re-attribute a row. Without this, every
    account migration 0030 back-filled looked stale on the day it ran."""
    _config(db, api_url="https://bp", aws_url="s3://feed")
    a = _clinic(db, "Alto")
    _map(db, CATCH_ALL_LOCATION_KEY, a.clinic_id, when=datetime(2026, 8, 26, 12, 0))
    monkeypatch.setattr(mod, "_feed_state", lambda ids: {
        "snapshot": "2026-08-25", "locations": {"1": "Site"}})

    assert _by_key(mod.get_instance_readiness(INSTANCE, CALLER, db))["sync_fresh"]["status"] == "ok"


def test_an_api_only_account_is_not_broken(db):
    """Blueprint exposes a live API and an S3 feed; a client can be on the API
    alone. Demanding aws_url sent someone hunting for a URL that never existed."""
    _config(db, api_url="https://bp")  # no aws_url
    a = _clinic(db, "Prairie")
    _map(db, CATCH_ALL_LOCATION_KEY, a.clinic_id)

    r = mod.get_instance_readiness(INSTANCE, CALLER, db)
    by = _by_key(r)
    assert by["pms_account"]["status"] == "ok"
    assert by["pms_secrets"]["status"] == "skipped"
    assert "sync_fresh" not in by      # nothing to be stale
    assert r["blocked_count"] == 0


def test_a_business_with_no_pms_is_skipped_not_failed(db):
    _clinic(db, "Solo", pms="none")
    by = _by_key(mod.get_instance_readiness(INSTANCE, CALLER, db))
    assert by["pms_account"]["status"] == "skipped"
    assert "locations_mapped" not in by


# ── Shape ─────────────────────────────────────────────────────────────────────

def test_unreachable_bigquery_is_unknown_never_a_silent_pass(db, monkeypatch):
    _config(db, api_url="https://bp", aws_url="s3://feed")
    a = _clinic(db, "Heritage")
    _map(db, "1", a.clinic_id)
    monkeypatch.setattr(mod, "_feed_state", lambda ids: {"snapshot": None, "locations": None})
    by = _by_key(mod.get_instance_readiness(INSTANCE, CALLER, db))
    assert by["locations_mapped"]["status"] == "unknown"
    assert by["sync_fresh"]["status"] == "blocked"


def test_every_non_ok_check_says_what_breaks(db, monkeypatch):
    """A status with no consequence is just a red dot — the operator still has to
    go and find out whether it matters."""
    _config(db, api_url="https://bp", aws_url="s3://feed")
    a = _clinic(db, "Heritage", etl=False)
    _map(db, "1", a.clinic_id, when=datetime(2026, 8, 26, 12, 0))
    _map(db, "2", None)
    monkeypatch.setattr(mod, "_feed_state", lambda ids: {
        "snapshot": "2026-08-25", "locations": {"1": "A", "9": "Unmapped"}})

    for c in mod.get_instance_readiness(INSTANCE, CALLER, db)["checks"]:
        if c["status"] in ("blocked", "warn", "skipped"):
            assert c["consequence"], f"{c['key']} has no consequence"


def test_per_clinic_checks_carry_ids_not_just_names(db):
    """A dashboard deciding whether to explain a zero has to match on something
    that survives a rename, so `items` (names, for people) is not enough."""
    _config(db, api_url="https://bp")
    a = _clinic(db, "Heritage")
    b = _clinic(db, "Market Mall", etl=False)
    _map(db, "1", a.clinic_id)
    _map(db, "2", b.clinic_id)

    by = _by_key(mod.get_instance_readiness(INSTANCE, CALLER, db))
    assert by["etl_enabled"]["clinic_ids"] == [b.clinic_id]
    # A business-wide check claims no specific clinic; every clinic inherits it.
    assert by["pms_account"]["clinic_ids"] is None


def test_unknown_instance_404s(db):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        mod.get_instance_readiness("NOPE", CALLER, db)
    assert e.value.status_code == 404
