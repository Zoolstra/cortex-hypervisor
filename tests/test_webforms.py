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
    return None. Pass ``deleted=True`` clinics to exercise the soft-delete path.

    ``execute`` serves the one select the ingest path makes — the form's
    ``jotform_form_locations`` rows. It applies the same ``active`` filter the
    real query does; scoping to a form id stays a SQL concern, so pass only the
    rows belonging to the form under test."""

    def __init__(self, clinics: dict[str, SimpleNamespace] | None = None,
                 locations: list[SimpleNamespace] | None = None):
        self._clinics = clinics or {}
        self._locations = locations or []

    def get(self, model, key):
        return self._clinics.get(key)

    def execute(self, stmt):
        rows = [r for r in self._locations if r.active]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def _clinic(clinic_id="C1", name="Test Clinic", deleted_at=None):
    return SimpleNamespace(clinic_id=clinic_id, clinic_name=name, deleted_at=deleted_at)


def _location(option_value, clinic_id="C2", form_id="F1", active=True):
    """One ``jotform_form_locations`` row: a dropdown answer -> a clinic."""
    return SimpleNamespace(jotform_form_id=form_id, option_value=option_value,
                           clinic_id=clinic_id, active=active)


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


# ── Location routing (jotform_form_locations) ─────────────────────────────────
#
# A group's single lead form names ONE clinic in its webhook path but asks the
# patient which site they want. These cover the answer overriding the path, and
# every way that override can fail without losing the lead.

# The Sense of Hearing appointment form: four location dropdowns revealed by
# condition (adult / 6-17 / APD / 10-months-up), of which exactly one is
# answered. Option strings are the real ones, addresses and all.
_BURLINGTON = "Burlington: 11 - 1960 Appleby Line"
_OAKVILLE = "Oakville: 240 North Service Road West Oakville"
_KINGSTON = "Limestone Hearing Care Centre (Kingston): 102 - 817 Bayridge Drive"


def _location_raw_request(answer, field="q19_chooseYour"):
    return json.dumps({
        "q24_name": {"first": "Ada", "last": "Byron"},
        "q5_email3": "ada@example.com",
        "q26_phoneNumber": {"full": "(905) 555-0100"},
        field: answer,
    })


def _post_location(client, answer, field="q19_chooseYour", form_id="F1"):
    return client.post(
        "/webforms/jotform/C1",
        params={"token": SECRET},
        data={"rawRequest": _location_raw_request(answer, field), "formID": form_id},
    )


def test_jotform_location_answer_overrides_the_path_clinic(harness):
    make_client, captured = harness
    client = make_client(_FakeDb(
        {"C1": _clinic("C1", "Burlington"), "C2": _clinic("C2", "Oakville")},
        [_location(_BURLINGTON, "C1"), _location(_OAKVILLE, "C2")],
    ))

    assert _post_location(client, _OAKVILLE).status_code == 200
    row = captured[0]
    # Path said C1; the patient chose Oakville, so the row belongs to C2.
    assert row["clinic_id"] == "C2"
    assert row["clinic_name"] == "Oakville"


def test_jotform_location_found_in_whichever_dropdown_was_answered(harness):
    # The condition engine reveals one of four location fields. The resolver
    # matches on the VALUE, so it must not care which field carried it.
    make_client, captured = harness
    client = make_client(_FakeDb(
        {"C1": _clinic("C1", "Burlington"), "C3": _clinic("C3", "Kingston")},
        [_location(_KINGSTON, "C3")],
    ))

    # q21_chooseYour21 is the APD-specific dropdown, not the default one.
    assert _post_location(client, _KINGSTON, field="q21_chooseYour21").status_code == 200
    assert captured[0]["clinic_id"] == "C3"


def test_jotform_unmapped_location_falls_back_to_the_path_clinic(harness):
    # An option renamed in the builder, or a site added and never mapped. The
    # lead must still land — the verbatim answer survives in raw_fields.
    make_client, captured = harness
    client = make_client(_FakeDb(
        {"C1": _clinic("C1", "Burlington")},
        [_location(_BURLINGTON, "C1")],
    ))

    assert _post_location(client, "Somewhere Else: 1 Nowhere Rd").status_code == 200
    row = captured[0]
    assert row["clinic_id"] == "C1"
    assert json.loads(row["raw_fields"])["chooseYour"] == "Somewhere Else: 1 Nowhere Rd"


def test_jotform_location_mapped_to_no_clinic_yet_falls_back(harness):
    # Known option, clinic not created yet (clinic_id NULL) — the expected state
    # part-way through a group's rollout, not a misconfiguration.
    make_client, captured = harness
    client = make_client(_FakeDb(
        {"C1": _clinic("C1", "Burlington")},
        [_location(_OAKVILLE, clinic_id=None)],
    ))

    assert _post_location(client, _OAKVILLE).status_code == 200
    assert captured[0]["clinic_id"] == "C1"


def test_jotform_inactive_location_mapping_is_ignored(harness):
    make_client, captured = harness
    client = make_client(_FakeDb(
        {"C1": _clinic("C1", "Burlington"), "C2": _clinic("C2", "Oakville")},
        [_location(_OAKVILLE, "C2", active=False)],
    ))

    assert _post_location(client, _OAKVILLE).status_code == 200
    assert captured[0]["clinic_id"] == "C1"


def test_jotform_location_mapped_to_deleted_clinic_falls_back(harness):
    make_client, captured = harness
    client = make_client(_FakeDb(
        {"C1": _clinic("C1", "Burlington"),
         "C2": _clinic("C2", "Oakville", deleted_at="2026-01-01")},
        [_location(_OAKVILLE, "C2")],
    ))

    assert _post_location(client, _OAKVILLE).status_code == 200
    assert captured[0]["clinic_id"] == "C1"


def test_jotform_form_with_no_location_map_keeps_the_path_clinic(harness):
    # Every single-site form: no rows, no override, unchanged behaviour.
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic("C1", "Burlington")}))

    assert _post_location(client, _OAKVILLE).status_code == 200
    assert captured[0]["clinic_id"] == "C1"


def test_jotform_submission_without_form_id_keeps_the_path_clinic(harness):
    # No formID part means nothing to look the map up by.
    make_client, captured = harness
    client = make_client(_FakeDb(
        {"C1": _clinic("C1", "Burlington"), "C2": _clinic("C2", "Oakville")},
        [_location(_OAKVILLE, "C2")],
    ))

    resp = client.post(
        "/webforms/jotform/C1",
        params={"token": SECRET},
        data={"rawRequest": _location_raw_request(_OAKVILLE)},
    )

    assert resp.status_code == 200
    assert captured[0]["clinic_id"] == "C1"


def test_jotform_location_match_tolerates_surrounding_whitespace(harness):
    make_client, captured = harness
    client = make_client(_FakeDb(
        {"C1": _clinic("C1", "Burlington"), "C2": _clinic("C2", "Oakville")},
        [_location(_OAKVILLE, "C2")],
    ))

    assert _post_location(client, f"  {_OAKVILLE} ").status_code == 200
    assert captured[0]["clinic_id"] == "C2"


# ── Coverage endpoint ──────────────────────────────────────────────────────────

from datetime import datetime, timezone

from api.deps import verify_token


class _FakeCoverageDb(_FakeDb):
    """Extends the clinic-map fake with ``execute`` for the registry select."""

    def __init__(self, clinics=None, registry_rows=None):
        super().__init__(clinics)
        self._registry_rows = registry_rows or []

    def execute(self, stmt):
        rows = self._registry_rows
        return SimpleNamespace(all=lambda: rows)


def _form(clinic_id="C1", form_id="F1", title="Contact", active=True):
    return SimpleNamespace(
        clinic_id=clinic_id, jotform_form_id=form_id, form_title=title, active=active
    )


class _FakeBqJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


@pytest.fixture
def coverage_harness(harness, monkeypatch):
    """Layer BQ-query stubbing and a role-bearing caller onto the base harness."""
    make_client, _ = harness
    bq_rows: list[dict] = []
    caller = {"role": "super_admin", "uid": "U1"}

    monkeypatch.setattr(webforms.bq_client, "query", lambda sql: _FakeBqJob(bq_rows))

    def _make(fake_db):
        client = make_client(fake_db)
        client.app.dependency_overrides[verify_token] = lambda: caller
        return client

    yield _make, bq_rows, caller


def test_coverage_requires_super_admin(coverage_harness):
    make_client, _, caller = coverage_harness
    caller["role"] = "admin"
    client = make_client(_FakeCoverageDb())

    assert client.get("/webforms/coverage").status_code == 403


def test_coverage_joins_registry_and_bq(coverage_harness):
    make_client, bq_rows, _ = coverage_harness
    last = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    bq_rows.extend([
        # Registered form with traffic.
        {"clinic_id": "C1", "form_id": "F1", "total": 10, "last_7d": 2,
         "last_30d": 5, "last_submission_at": last},
        # NULL form_id (JSON endpoint / pre-provenance rows) — clinic totals only.
        {"clinic_id": "C1", "form_id": None, "total": 3, "last_7d": 1,
         "last_30d": 2, "last_submission_at": last},
        # Unregistered form on an unregistered clinic → drift markers.
        {"clinic_id": "C2", "form_id": "F9", "total": 4, "last_7d": 0,
         "last_30d": 4, "last_submission_at": last},
    ])
    db = _FakeCoverageDb(
        clinics={"C1": _clinic("C1", "Alpha"), "C2": _clinic("C2", "Beta")},
        registry_rows=[(_form("C1", "F1"), "Alpha"),
                       (_form("C1", "F2", title="Quiet form"), "Alpha")],
    )
    client = make_client(db)

    resp = client.get("/webforms/coverage")
    assert resp.status_code == 200
    by_id = {c["clinic_id"]: c for c in resp.json()}

    alpha = by_id["C1"]
    assert alpha["total"] == 13 and alpha["last_7d"] == 3          # includes NULL-form rows
    forms = {f["jotform_form_id"]: f for f in alpha["forms"]}
    assert forms["F1"]["total"] == 10
    assert forms["F2"]["total"] == 0                                # registered, never fired
    assert forms["F2"]["last_submission_at"] is None
    assert alpha["unregistered_form_ids"] == []

    beta = by_id["C2"]
    assert beta["clinic_name"] == "Beta"                            # resolved via db.get
    assert beta["forms"] == []                                      # data but no registry
    assert beta["unregistered_form_ids"] == ["F9"]
    assert beta["total"] == 4


def test_coverage_empty_when_no_table_and_no_registry(coverage_harness, monkeypatch):
    make_client, _, _ = coverage_harness

    def _raise_not_found(sql):
        from google.cloud.exceptions import NotFound
        raise NotFound("no table")

    monkeypatch.setattr(webforms.bq_client, "query", _raise_not_found)
    client = make_client(_FakeCoverageDb())

    resp = client.get("/webforms/coverage")
    assert resp.status_code == 200
    assert resp.json() == []


# ── Ad-click identifier extraction ────────────────────────────────────────────

def test_attribution_reads_click_ids_from_landing_page():
    """gbraid / gad_campaignid arrive ONLY in the landing URL today — no form
    sends them as hidden fields — so the fallback is the whole feature."""
    lp = ("/brand-official-cec?matchtype=p&keyword=hearing%20places"
          "&gad_source=1&gad_campaignid=22788881937"
          "&gbraid=0AAAAADmeCYZRgOvoUrY7oEIU5gZjT70N5&gclid=Cj0KCQjw7eXT")
    got = webforms._attribution({"landing_page": lp})
    assert got["gad_campaignid"] == "22788881937"
    assert got["gbraid"] == "0AAAAADmeCYZRgOvoUrY7oEIU5gZjT70N5"
    assert got["gclid"] == "Cj0KCQjw7eXT"
    assert got["wbraid"] is None


def test_attribution_prefers_explicit_field_over_url():
    got = webforms._attribution({
        "gclid": "from-field",
        "landing_page": "/x?gclid=from-url&gbraid=g1",
    })
    assert got["gclid"] == "from-field"
    assert got["gbraid"] == "g1"          # no field → URL still used


def test_attribution_rejects_nan_and_blank_sentinels():
    """'nan' is the ETL's absent-marker (queries.py:574) and a bare '?gclid='
    must not become an empty-string id that reads as present."""
    assert webforms._attribution({"gclid": "nan", "landing_page": "/x"})["gclid"] is None
    assert webforms._attribution({"landing_page": "/x?gclid=nan"})["gclid"] is None
    assert webforms._attribution({"landing_page": "/x?gclid="})["gclid"] is None


def test_attribution_handles_missing_and_querystringless_urls():
    for lp in (None, "", "/no-query-string"):
        got = webforms._attribution({"landing_page": lp})
        assert all(v is None for v in got.values()), lp


# ── utm_source / referrer split ───────────────────────────────────────────────

def test_utm_moves_referrer_host_out_of_utm_source():
    """The sites write document.referrer's host into the utm_source field when
    the visit carried no UTM tags. That is not a campaign source."""
    got = webforms._utm({"utm_source": "google.com", "landing_page": "/contact"})
    assert got["utm_source"] is None
    assert got["referrer_host"] == "google.com"


def test_utm_keeps_genuine_utm_whose_value_looks_like_a_host():
    """`?utm_source=chatgpt.com` is a REAL tag whose value happens to be a
    hostname — this row exists in production. Provenance decides, not shape."""
    got = webforms._utm({
        "utm_source": "chatgpt.com",
        "landing_page": "/contact?utm_source=chatgpt.com",
    })
    assert got["utm_source"] == "chatgpt.com"
    assert got["referrer_host"] is None


def test_utm_url_params_beat_the_form_field():
    got = webforms._utm({
        "utm_source": "google.com",           # referrer fallback from the site
        "landing_page": "/lp?utm_source=newsletter&utm_medium=email",
    })
    assert got["utm_source"] == "newsletter"
    assert got["utm_medium"] == "email"
    assert got["referrer_host"] is None       # a real tag was present


def test_utm_treats_direct_and_android_package_as_referrers():
    for value in ("direct", "com.google.android.googlequicksearchbox",
                  "ca.search.yahoo.com"):
        got = webforms._utm({"utm_source": value, "landing_page": "/"})
        assert got["utm_source"] is None, value
        assert got["referrer_host"] == value, value


def test_utm_passes_through_a_plain_campaign_source():
    got = webforms._utm({"utm_source": "spring-promo", "landing_page": "/"})
    assert got["utm_source"] == "spring-promo"
    assert got["referrer_host"] is None


def test_utm_honours_referrer_host_sent_explicitly_by_a_fixed_site():
    """After the site fix the referrer arrives in its OWN field and utm_source is
    genuinely empty. The salvage path must not blank it."""
    got = webforms._utm({
        "utm_source": None,
        "referrer_host": "google.com",
        "landing_page": "/contact",
    })
    assert got["referrer_host"] == "google.com"
    assert got["utm_source"] is None


def test_utm_explicit_referrer_host_wins_over_the_salvaged_one():
    """Mixed fleet: one site deployed, another still on the old build. An
    explicit value must never be overwritten by the utm_source salvage."""
    got = webforms._utm({
        "utm_source": "bing.com",        # old-build fallback
        "referrer_host": "google.com",   # new-build explicit
        "landing_page": "/",
    })
    assert got["referrer_host"] == "google.com"
    assert got["utm_source"] is None
