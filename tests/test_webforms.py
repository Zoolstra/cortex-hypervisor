"""Tests for web-form ingestion (``POST /webforms``) and ``GET /webforms/coverage``.

The Jotform webhook relay and its location-routing tests moved to the ETL with
the parser (``cortex-data-ingestion/app/test_jotform_parse.py`` /
``test_jotform_sync.py``) when ingestion switched to API polling (2026-09).

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


def test_json_relay_rows_are_tagged_and_have_no_submission_id(harness):
    make_client, captured = harness
    client = make_client(_FakeDb({"C1": _clinic()}))
    client.post("/webforms", headers={"X-Webform-Secret": SECRET},
                json={"clinic_id": "C1", "first_name": "Jane"})
    row = captured[0]
    assert row["ingest_source"] == "json_relay"
    assert row["submission_id"] is None
    assert row["ingested_at"]


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


# ── Cross-repo parity fixtures ────────────────────────────────────────────────
#
# The ETL (cortex-data-ingestion/app/jotform/) now carries a copy of _utm /
# _attribution for the API-polling ingest, and is the schema of record for
# ClinicData.webforms. Both repos assert the same fixture files, which must be
# byte-identical — see resources/jotform-api-polling-plan.md §6.

import pathlib as _pathlib

_FIXTURES = _pathlib.Path(__file__).parent / "fixtures"
_SIBLING = _pathlib.Path(__file__).parents[2] / "cortex-data-ingestion" / "app" / "test_fixtures"


def _parity_cases():
    return json.loads((_FIXTURES / "webform_attribution_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", _parity_cases(), ids=lambda c: c["name"])
def test_attribution_and_utm_match_the_shared_fixture(case):
    fields = case["fields"]
    for key, want in (case.get("expect_attribution") or {}).items():
        assert webforms._attribution(fields)[key] == want, key
    for key, want in (case.get("expect_utm") or {}).items():
        assert webforms._utm(fields)[key] == want, key


def test_webforms_schema_mirror_matches_the_pinned_fixture():
    pinned = json.loads((_FIXTURES / "webforms_schema.json").read_text())
    mine = [{"name": f.name, "type": f.field_type, "mode": f.mode}
            for f in webforms.WEBFORMS_SCHEMA]
    assert mine == pinned, (
        "api/webforms.py::WEBFORMS_SCHEMA drifted from the schema of record "
        "(cortex-data-ingestion/app/jotform/schema.py) — regenerate the fixture "
        "there and copy it here")


@pytest.mark.parametrize("name", ["webform_attribution_cases.json", "webforms_schema.json"])
def test_fixture_copies_are_identical_across_repos(name):
    sibling = _SIBLING / name
    if not sibling.exists():
        pytest.skip("cortex-data-ingestion not checked out beside this repo")
    assert (_FIXTURES / name).read_bytes() == sibling.read_bytes(), (
        f"{name} differs between the two repos — copy the updated one over the other")
