"""Tests for web-form ingestion (``POST /webforms``).

Covers the endpoint contract end-to-end with the heavy edges mocked:
  - auth: missing/wrong X-Webform-Secret → 403 (verify_webform_secret NOT
    overridden here; get_secret is monkeypatched to a known value)
  - happy path: valid secret + known clinic → 200, one row streamed to BQ with
    clinic_name enrichment, server-side submitted_at, and blank→None normalisation
  - unknown / soft-deleted clinic → 404, nothing written
  - validation: missing clinic_id → 422

BQ is never touched: insert_rows_json is captured and _ensure_table is no-op'd.
The DB is an in-memory stand-in resolving Clinic by id.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import app
import api.webforms as webforms
from api.core.db import get_session


SECRET = "test-webform-secret"


class _FakeDb:
    """``get(Clinic, id)`` resolves clinics from an in-memory map; unknown ids
    return None. Pass ``deleted=True`` clinics to exercise the soft-delete path."""

    def __init__(self, clinics: dict[str, SimpleNamespace] | None = None):
        self._clinics = clinics or {}

    def get(self, model, key):
        return self._clinics.get(key)


def _clinic(clinic_id="C1", name="Test Clinic", deleted_at=None):
    return SimpleNamespace(clinic_id=clinic_id, clinic_name=name, deleted_at=deleted_at)


@pytest.fixture
def harness(monkeypatch):
    """Build a TestClient with the DB faked and BQ stubbed.

    Returns (make_client, captured) where ``captured`` accumulates the rows
    handed to insert_rows_json so assertions can inspect what would be written.
    """
    captured: list[dict] = []

    monkeypatch.setattr(webforms, "get_secret", lambda name, *a, **k: SECRET)
    monkeypatch.setattr(webforms, "_ensure_table", lambda: None)

    def _fake_insert(table, rows):
        captured.extend(rows)
        return []  # no errors

    monkeypatch.setattr(webforms.bq_client, "insert_rows_json", _fake_insert)

    def _make(fake_db: _FakeDb) -> TestClient:
        app.dependency_overrides[get_session] = lambda: fake_db
        return TestClient(app, raise_server_exceptions=False)

    yield _make, captured
    app.dependency_overrides.clear()


def test_happy_path_writes_one_enriched_row(harness):
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic("C1", "Northside Audiology")}))

    resp = client.post(
        "/webforms",
        headers={"X-Webform-Secret": SECRET},
        json={
            "clinic_id": "C1",
            "first_name": "Jane",
            "last_name": "Doe",
            "phone_number": "555-1234",
            "email": "jane@example.com",
            "utm_source": "google",
            "utm_content": "hero_cta",
            "landing_page": "https://clinic.example/contact",
            "customer_type": "New Customer",
            "message": "I'd like to book a hearing test.",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}
    assert len(captured) == 1
    row = captured[0]
    assert row["clinic_id"] == "C1"
    assert row["clinic_name"] == "Northside Audiology"  # enriched server-side
    assert row["first_name"] == "Jane"
    assert row["utm_content"] == "hero_cta"
    assert row["customer_type"] == "New Customer"
    assert row["message"] == "I'd like to book a hearing test."
    assert row["submitted_at"]  # server-stamped, present


def test_blank_optional_fields_normalised_to_none(harness):
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic()}))

    resp = client.post(
        "/webforms",
        headers={"X-Webform-Secret": SECRET},
        json={"clinic_id": "C1", "first_name": "  ", "email": ""},
    )

    assert resp.status_code == 200
    row = captured[0]
    assert row["first_name"] is None
    assert row["email"] is None


def test_missing_secret_is_403(harness):
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic()}))

    resp = client.post("/webforms", json={"clinic_id": "C1"})

    assert resp.status_code == 403
    assert captured == []


def test_wrong_secret_is_403(harness):
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic()}))

    resp = client.post(
        "/webforms",
        headers={"X-Webform-Secret": "nope"},
        json={"clinic_id": "C1"},
    )

    assert resp.status_code == 403
    assert captured == []


def test_unknown_clinic_is_404(harness):
    make_client, captured = harness
    client = make_client(_FakeDb())  # no clinics

    resp = client.post(
        "/webforms",
        headers={"X-Webform-Secret": SECRET},
        json={"clinic_id": "ghost"},
    )

    assert resp.status_code == 404
    assert captured == []


def test_soft_deleted_clinic_is_404(harness):
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic(deleted_at="2026-01-01T00:00:00Z")}))

    resp = client.post(
        "/webforms",
        headers={"X-Webform-Secret": SECRET},
        json={"clinic_id": "C1"},
    )

    assert resp.status_code == 404
    assert captured == []


def test_missing_clinic_id_is_422(harness):
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic()}))

    resp = client.post(
        "/webforms",
        headers={"X-Webform-Secret": SECRET},
        json={"first_name": "Jane"},
    )

    assert resp.status_code == 422
    assert captured == []


# ── Jotform webhook relay (POST /webforms/jotform/{clinic_id}) ──────────────────

def _raw_request() -> str:
    """A representative Jotform ``rawRequest`` payload (qN_ prefixed fields)."""
    return json.dumps({
        "q3_fullName": {"first": "Jane", "last": "Doe"},
        "q5_email": "jane@example.com",
        "q6_phone": {"full": "(403) 555-1212"},
        "q7_utm_source": "google",
        "q8_utm_content": "hero_cta",
        "q12_utm_medium": "cpc",
        "q13_utm_campaign": "spring_sale",
        "q14_utm_term": "hearing+aids",
        "q15_gclid": "abc123",
        "q16_fbclid": "fb456",
        "q9_customerType": "New Customer",
        "q10_message": "I need a hearing test",
        "q11_landing_page": "/contact-us/?utm_source=google",
    })


def test_jotform_happy_path_parses_and_enriches(harness):
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic("C1", "Northside Audiology")}))

    resp = client.post(
        "/webforms/jotform/C1",
        params={"token": SECRET},
        data={"rawRequest": _raw_request(), "formID": "261067364350050",
              "submissionID": "999"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}
    assert len(captured) == 1
    row = captured[0]
    assert row["clinic_id"] == "C1"
    assert row["clinic_name"] == "Northside Audiology"  # enriched server-side
    assert row["first_name"] == "Jane"
    assert row["last_name"] == "Doe"
    assert row["email"] == "jane@example.com"
    assert row["phone_number"] == "(403) 555-1212"
    assert row["utm_source"] == "google"
    assert row["utm_content"] == "hero_cta"
    assert row["utm_medium"] == "cpc"
    assert row["utm_campaign"] == "spring_sale"
    assert row["utm_term"] == "hearing+aids"
    assert row["gclid"] == "abc123"
    assert row["fbclid"] == "fb456"
    assert row["customer_type"] == "New Customer"
    assert row["message"] == "I need a hearing test"
    assert row["landing_page"] == "/contact-us/?utm_source=google"
    assert row["submitted_at"]


def test_jotform_parses_autogenerated_field_names(harness):
    # Real-world payload: Jotform names fields fullname0/email1/phone2/textarea4
    # when no custom unique name is set. The parser must still extract them.
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic()}))
    raw = json.dumps({
        "q2_fullname0": {"first": "Sam", "last": "Rivera"},
        "q3_email1": "sam@example.com",
        "q4_phone2": {"full": "(403) 222-3333"},
        "q6_textarea4": "Need a hearing test",
        "preferredLocation": "Saskatoon",
        "utm_source": "facebook",
    })

    resp = client.post(
        "/webforms/jotform/C1",
        params={"token": SECRET},
        data={"rawRequest": raw},
    )

    assert resp.status_code == 200
    row = captured[0]
    assert row["first_name"] == "Sam"
    assert row["last_name"] == "Rivera"
    assert row["email"] == "sam@example.com"
    assert row["phone_number"] == "(403) 222-3333"
    assert row["message"] == "Need a hearing test"
    assert row["utm_source"] == "facebook"


def test_jotform_token_tolerates_secret_trailing_newline(harness, monkeypatch):
    # Regression: the SM secret was stored with a trailing "\n"; the webhook URL
    # token has the clean value. Auth must still pass.
    monkeypatch.setattr(webforms, "get_secret", lambda name, *a, **k: SECRET + "\n")
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic()}))

    resp = client.post(
        "/webforms/jotform/C1",
        params={"token": SECRET},
        data={"rawRequest": _raw_request()},
    )

    assert resp.status_code == 200
    assert len(captured) == 1


def test_jotform_bad_token_is_403(harness):
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic()}))

    resp = client.post(
        "/webforms/jotform/C1",
        params={"token": "nope"},
        data={"rawRequest": _raw_request()},
    )

    assert resp.status_code == 403
    assert captured == []


def test_jotform_unknown_clinic_is_404(harness):
    make_client, captured = harness
    client = make_client(_FakeDb())  # no clinics

    resp = client.post(
        "/webforms/jotform/ghost",
        params={"token": SECRET},
        data={"rawRequest": _raw_request()},
    )

    assert resp.status_code == 404
    assert captured == []


def test_jotform_garbage_rawrequest_stores_nulls(harness):
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic()}))

    resp = client.post(
        "/webforms/jotform/C1",
        params={"token": SECRET},
        data={"rawRequest": "not-json"},
    )

    assert resp.status_code == 200
    assert len(captured) == 1
    row = captured[0]
    assert row["clinic_id"] == "C1"
    assert row["first_name"] is None and row["email"] is None
    assert row["submitted_at"]  # still stamped
