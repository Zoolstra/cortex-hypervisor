"""Tests for the client data feed (``/datafeed/v1/{instance_id}/*``).

Covers the endpoint contract with the heavy edges mocked:
  - auth: missing/wrong key, unprovisioned secret (SM NotFound), unknown
    instance — all 403 with an identical body
  - tenant scoping: every BQ query carries the instance's own profile ID as a
    parameter (never interpolated), and an instance without a linked
    Google Ads / Invoca ID short-circuits to an empty result set (no query)
  - window validation: start > end and > 366-day spans are 422
  - serialization: datetimes come back ISO-formatted inside the envelope
  - transcript endpoints stay removed (they existed 2026-07-22..24 and were
    dropped before client handoff — the feed is metadata-only)

BigQuery is never touched: ``bq_client.query`` is captured. The DB is an
in-memory stand-in resolving the Instance by id.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as gcp_exc

from api import app
import api.datafeed as datafeed
from api.core.db import get_session


KEY = "test-datafeed-key"
INSTANCE_ID = "9e59f7fa-dcf5-4308-bdff-7c1b56ae7a1a"


def _instance(instance_id=INSTANCE_ID, ads="9751756815", invoca="202527", clinics=None):
    if clinics is None:
        clinics = [SimpleNamespace(clinic_id="CL1", deleted_at=None),
                   SimpleNamespace(clinic_id="CL2", deleted_at=None)]
    return SimpleNamespace(
        instance_id=instance_id,
        instance_name="Virsono Hearing Centres",
        google_ads_customer_id=ads,
        invoca_profile_id=invoca,
        clinics=clinics,
    )


class _FakeDb:
    def __init__(self, instances=None):
        self._instances = instances or {}

    def get(self, model, key):
        return self._instances.get(key)


class _FakeBqJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


@pytest.fixture
def harness(monkeypatch):
    """TestClient with SM, BQ, and the DB faked.

    Returns ``(make_client, queries)``; ``queries`` accumulates
    ``(sql, {param: value})`` for every BQ query issued so scoping can be
    asserted. Seed BQ responses by appending row-lists to ``queries.results``.
    """
    queries = SimpleNamespace(calls=[], results=[])

    monkeypatch.setattr(datafeed, "get_secret", lambda name, *a, **k: KEY)

    def _fake_query(sql, job_config=None):
        params = {p.name: p.value for p in (job_config.query_parameters if job_config else [])}
        queries.calls.append((sql, params))
        rows = queries.results.pop(0) if queries.results else []
        return _FakeBqJob(rows)

    monkeypatch.setattr(datafeed.bq_client, "query", _fake_query)

    def _make(fake_db=None) -> TestClient:
        app.dependency_overrides[get_session] = lambda: fake_db or _FakeDb({INSTANCE_ID: _instance()})
        return TestClient(app, raise_server_exceptions=False)

    yield _make, queries
    app.dependency_overrides.clear()


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_missing_key_is_403(harness):
    make_client, _ = harness
    resp = make_client().get(f"/datafeed/v1/{INSTANCE_ID}/google-ads/campaigns")
    assert resp.status_code == 403


def test_wrong_key_is_403(harness):
    make_client, _ = harness
    resp = make_client().get(
        f"/datafeed/v1/{INSTANCE_ID}/google-ads/campaigns",
        headers={"X-API-Key": "nope"},
    )
    assert resp.status_code == 403


def test_unprovisioned_secret_is_403(harness, monkeypatch):
    make_client, _ = harness
    monkeypatch.setattr(
        datafeed, "get_secret",
        lambda name, *a, **k: (_ for _ in ()).throw(gcp_exc.NotFound("no secret")),
    )
    resp = make_client().get(
        f"/datafeed/v1/{INSTANCE_ID}/google-ads/campaigns",
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 403


def test_unknown_instance_is_403(harness):
    make_client, _ = harness
    resp = make_client(_FakeDb({})).get(
        f"/datafeed/v1/{INSTANCE_ID}/google-ads/campaigns",
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 403


# ── Window validation ─────────────────────────────────────────────────────────

def test_inverted_window_is_422(harness):
    make_client, _ = harness
    resp = make_client().get(
        f"/datafeed/v1/{INSTANCE_ID}/google-ads/campaigns",
        params={"start_date": "2026-07-01", "end_date": "2026-06-01"},
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 422


def test_oversized_window_is_422(harness):
    make_client, _ = harness
    resp = make_client().get(
        f"/datafeed/v1/{INSTANCE_ID}/google-ads/campaigns",
        params={"start_date": "2024-01-01", "end_date": "2026-07-01"},
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 422


# ── Scoping + envelope ────────────────────────────────────────────────────────

def test_campaigns_scopes_by_ads_profile(harness):
    make_client, queries = harness
    queries.results.append([
        {"date": "2026-07-01", "campaign_id": "123", "campaign_name": "Virsono Calgary",
         "clicks": 10, "interactions": 12, "cost": 45.6, "conversions": 1.0,
         "conversions_value": 100.0},
    ])
    resp = make_client().get(
        f"/datafeed/v1/{INSTANCE_ID}/google-ads/campaigns",
        params={"start_date": "2026-07-01", "end_date": "2026-07-07"},
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["instance_id"] == INSTANCE_ID
    assert body["count"] == 1
    assert body["rows"][0]["campaign_name"] == "Virsono Calgary"

    sql, params = queries.calls[0]
    assert params["profile"] == "9751756815"
    assert params["start"] == "2026-07-01" and params["end"] == "2026-07-07"
    assert "@profile" in sql and "9751756815" not in sql  # parameterized, not interpolated


def test_transactions_scopes_by_invoca_profile_and_serializes(harness):
    make_client, queries = harness
    queries.results.append([
        {"transaction_id": "T1", "complete_call_id": "C1",
         "timestamp": datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc),
         "calling_phone_number": "15551234567"},
    ])
    resp = make_client().get(
        f"/datafeed/v1/{INSTANCE_ID}/invoca/transactions",
        params={"start_date": "2026-07-01", "end_date": "2026-07-07", "limit": 50},
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 50 and body["offset"] == 0
    assert body["rows"][0]["timestamp"] == "2026-07-01T12:30:00+00:00"

    sql, params = queries.calls[0]
    assert params["profile"] == 202527  # INT64, from the instance row
    assert params["limit"] == 50
    assert "QUALIFY ROW_NUMBER()" in sql  # event → call dedupe


def test_transactions_withholds_tracking_and_qa_columns(harness):
    """Columns excluded from the client feed (2026-07-24) must stay in the
    SELECT * EXCEPT list; gclid stays exposed as the join key to clicks."""
    make_client, queries = harness
    resp = make_client().get(
        f"/datafeed/v1/{INSTANCE_ID}/invoca/transactions",
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 200
    sql, _ = queries.calls[0]
    no_comments = "\n".join(
        l for l in sql.splitlines() if not l.strip().startswith("--")
    )
    except_clause = no_comments.split("EXCEPT", 1)[1].split(")", 1)[0]
    for col in (
        # tracking / caller-history / QA (first pass)
        "repeat_calling_phone_number", "_fbc", "_fbp", "fbclid",
        "g_cid", "ga_session_id", "google_analytics_id", "ga_measurement_id",
        "wbraid", "gbraid", "msclkid", "customer_id", "reviewed_by", "evaluated_by",
        # Invoca AI judgments + signal events + dead column (second pass)
        "Appointment_Discussed__Industry_", "Conversion_Likely__Industry_",
        "Buying_Intent__Industry_", "Existing_Appointment__Industry_",
        "Existing_Customer__Industry_", "Customer_Experience_Issue__Industry_",
        "Appointment_Booked__Conversion_", "Credit_Card_Payment__Conversion_",
        "Service_Appointment_Booked__Conversion_",
        "Proper_Greeting__Scorecard_", "Positive_Wrap_Up__Scorecard_",
        "Assume_Appointment__Scorecard_",
        "Answered_by_Agent", "Answered_by_Voicemail", "Voicemail_Left", "Short_Call",
        "Opportunity", "Non_Converting_Opportunity", "Call_to_Review", "Excellent_Call",
        "AI_Appointment_Booked", "AI_New_Customer", "AI_Opportunity",
        "call_sentiment_overall", "call_sentiment_overall_label",
        "signal_name", "signal_occurred_at", "signal_source", "signal_partner_unique_id",
        "revenue", "has_transcript",
        # BAA gate: withheld until the per-instance flag secret is set
        "calling_phone_number",
    ):
        assert col in except_clause, col
    assert "gclid" not in except_clause.replace("fbclid", "")  # join key stays


def test_transactions_ships_caller_number_when_flag_enabled(harness, monkeypatch):
    """The BAA flag secret (truthy value) removes calling_phone_number from the
    EXCEPT list; everything else stays withheld."""
    make_client, queries = harness

    def _secrets(name, *a, **k):
        if name == f"datafeed-caller-number-enabled-{INSTANCE_ID}":
            return "1"
        return KEY

    monkeypatch.setattr(datafeed, "get_secret", _secrets)
    resp = make_client().get(
        f"/datafeed/v1/{INSTANCE_ID}/invoca/transactions",
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 200
    sql, _ = queries.calls[0]
    except_clause = sql.split("EXCEPT", 1)[1].split(")", 1)[0]
    assert "calling_phone_number" not in except_clause
    assert "repeat_calling_phone_number" in except_clause  # rest still withheld


def test_caller_number_flag_requires_truthy_value(harness, monkeypatch):
    """A flag secret that exists but isn't truthy (e.g. '0') keeps the gate shut."""
    make_client, queries = harness

    def _secrets(name, *a, **k):
        if name == f"datafeed-caller-number-enabled-{INSTANCE_ID}":
            return "0"
        return KEY

    monkeypatch.setattr(datafeed, "get_secret", _secrets)
    resp = make_client().get(
        f"/datafeed/v1/{INSTANCE_ID}/invoca/transactions",
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 200
    sql, _ = queries.calls[0]
    assert "calling_phone_number" in sql.split("EXCEPT", 1)[1].split(")", 1)[0]


# ── Callscoring (settled layer) ───────────────────────────────────────────────

def test_callscoring_scopes_and_serializes(harness):
    make_client, queries = harness
    queries.results.append([
        {"complete_call_id": "C1",
         "call_started_at": datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc),
         "market": "Greenville", "classification": "appointment_booked",
         "verified_outcome": "appointment_booked", "booked_verified": True,
         "reasoning": "Caller booked a hearing test.",
         "scored_as_of": datetime(2026, 7, 2, 7, 0, tzinfo=timezone.utc)},
    ])
    resp = make_client().get(
        f"/datafeed/v1/{INSTANCE_ID}/invoca/callscoring",
        params={"start_date": "2026-07-01", "end_date": "2026-07-07"},
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"][0]["booked_verified"] is True
    assert body["rows"][0]["scored_as_of"] == "2026-07-02T07:00:00+00:00"

    sql, params = queries.calls[0]
    assert params["profile"] == 202527
    assert params["clinic_ids"] == ["CL1", "CL2"]  # instance's clinics only
    assert params["match_days"] == 3
    assert "call_outcome_overrides" in sql   # relabels applied
    assert "patient_contacts" in sql         # PMS reconciliation present


def test_callscoring_empty_without_clinics(harness):
    make_client, queries = harness
    db = _FakeDb({INSTANCE_ID: _instance(clinics=[])})
    resp = make_client(db).get(
        f"/datafeed/v1/{INSTANCE_ID}/invoca/callscoring",
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["rows"] == []
    assert queries.calls == []


# ── Dictionary ────────────────────────────────────────────────────────────────

def test_dictionary_serves_versioned_definitions(harness):
    make_client, _ = harness
    resp = make_client().get(
        f"/datafeed/v1/{INSTANCE_ID}/dictionary",
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == datafeed.SCHEMA_VERSION
    assert set(body["layers"]) == {"settled", "operational_mirror"}
    assert "/invoca/callscoring" in body["layers"]["settled"]["endpoints"]
    assert "booked_verified" in body["field_definitions"]["/invoca/callscoring"]
    assert body["caller_number_enabled"] is False  # default OFF
    assert body["booked_match_days"] == 3


def test_dictionary_requires_key(harness):
    make_client, _ = harness
    assert make_client().get(f"/datafeed/v1/{INSTANCE_ID}/dictionary").status_code == 403


def test_clicks_envelope_carries_settled_through(harness):
    """settled_through = today − CLICKS_SETTLE_DAYS; the additive-pull anchor.
    Rows dated on/before it are final (outside the ETL's restatement window)."""
    from datetime import date, timedelta

    make_client, queries = harness
    queries.results.append([])
    resp = make_client().get(
        f"/datafeed/v1/{INSTANCE_ID}/google-ads/clicks",
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 200
    expected = (date.today() - timedelta(days=datafeed._CLICKS_SETTLE_DAYS)).isoformat()
    assert resp.json()["settled_through"] == expected


def test_missing_ads_link_returns_empty_without_querying(harness):
    make_client, queries = harness
    db = _FakeDb({INSTANCE_ID: _instance(ads=None)})
    resp = make_client(db).get(
        f"/datafeed/v1/{INSTANCE_ID}/google-ads/clicks",
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["rows"] == []
    assert queries.calls == []


def test_missing_invoca_link_returns_empty_without_querying(harness):
    make_client, queries = harness
    db = _FakeDb({INSTANCE_ID: _instance(invoca=None)})
    resp = make_client(db).get(
        f"/datafeed/v1/{INSTANCE_ID}/invoca/transactions",
        headers={"X-API-Key": KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["rows"] == []
    assert queries.calls == []


# ── Transcript endpoints removed (2026-07-24) ─────────────────────────────────

def test_transcript_routes_do_not_exist(harness):
    """The transcript endpoints were removed before client handoff; make sure
    they don't quietly come back. Both paths must fall through to FastAPI's
    404 without issuing any BQ query."""
    make_client, queries = harness
    client = make_client()
    for path in (
        f"/datafeed/v1/{INSTANCE_ID}/invoca/transcripts",
        f"/datafeed/v1/{INSTANCE_ID}/invoca/transactions/C1/transcript",
    ):
        resp = client.get(path, headers={"X-API-Key": KEY})
        assert resp.status_code == 404, path
    assert queries.calls == []
