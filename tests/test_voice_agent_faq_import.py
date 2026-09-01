"""Tests for POST /clinics/{clinic_id}/voice_agent/faqs/import.

The endpoint had NO coverage and shipped broken: it built its table reference
with ``bq_table('faq')``, which hardcodes the **Users** dataset, while the
transcript-extracted suggestions live in **ClinicData**. Every call 404'd
inside BigQuery and surfaced to the dashboard as a bare HTTP 500.

``bq_table`` is still correct for this router's Users-resident table
(``voice_agent_tickets``), so the guard here is specifically that THIS query
targets ClinicData — a plain "does it 200" test would not have caught the
original bug either, since any stubbed client returns rows regardless of the
dataset in the SQL.
"""
import pytest
from fastapi.testclient import TestClient

from api import app
from api.core.db import get_session
from api.deps import verify_token
from api.voice_agent import voice_agent as mod


CLINIC = "CLINIC_1"
URL = f"/clinics/{CLINIC}/voice_agent/faqs/import"


class _FakeClinic:
    instance_id = "INST_A"
    clinic_name = "Test Clinic"
    deleted_at = None


class _Row(dict):
    """BigQuery Row stand-in — the handler subscripts by column name."""


class _FakeBQ:
    """Captures the rendered SQL and replays fixed rows."""

    def __init__(self, rows):
        self.rows = rows
        self.sql = None

    def query(self, sql, job_config=None):
        self.sql = sql
        rows = self.rows
        return type("Job", (), {"result": staticmethod(lambda: rows)})()


class _FakeSession:
    """Enough Session surface for the handler: get(), execute(), add(), flush()."""

    def __init__(self, clinic, existing_questions=()):
        self._clinic = clinic
        self.added = []
        self._existing = [(q,) for q in existing_questions]

    def get(self, _model, _id):
        return self._clinic

    def execute(self, _stmt):
        # Both reads in this path (existing questions, then the full refetch)
        # are satisfied from the same list; the handler only iterates them.
        return list(self._existing)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


@pytest.fixture
def harness(monkeypatch):
    bq = _FakeBQ([
        _Row(question="Do you take Blue Cross?", answer="We do.",
             complete_call_id="CALL_1"),
        _Row(question="Where do I park?", answer="Out front.",
             complete_call_id="CALL_2"),
    ])
    monkeypatch.setattr(mod, "bq_client", bq)
    # The response re-reads the clinic's FAQs; keep that out of the assertions.
    monkeypatch.setattr(mod, "_all_faqs_for_clinic", lambda db, cid: [])

    session = _FakeSession(_FakeClinic())

    def _session():
        yield session
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[verify_token] = lambda: {
        "role": "super_admin", "uid": "U1", "email": "admin@zoolstra.com"}

    yield TestClient(app), bq, session
    app.dependency_overrides.clear()


def test_import_queries_clinicdata_not_users(harness):
    """The regression guard. `Users.faq` does not exist — targeting it 500s."""
    client, bq, _ = harness
    resp = client.post(URL)
    assert resp.status_code == 200, resp.text

    assert "ClinicData.faq" in bq.sql
    assert "Users.faq" not in bq.sql
    # Pin the constant too, so a future edit can't quietly re-point it.
    assert mod.FAQ_SUGGESTIONS_TABLE.endswith(".ClinicData.faq")


def test_import_scopes_the_query_to_the_clinic(harness):
    client, bq, _ = harness
    assert client.post(URL).status_code == 200
    assert "clinic_id = @clinic_id" in bq.sql


def test_imported_rows_are_unapproved_etl_suggestions(harness):
    """Never auto-approve. These are unreviewed LLM extractions from calls —
    an approved row is retrievable mid-call and speaks in the clinic's voice."""
    client, _, session = harness
    body = client.post(URL).json()

    assert body["imported"] == 2
    assert body["skipped"] == 0
    assert len(session.added) == 2
    for row in session.added:
        assert row.source == "etl"
        assert row.approved is False
        assert row.clinic_id == CLINIC
    assert {r.source_call_id for r in session.added} == {"CALL_1", "CALL_2"}


def test_import_is_idempotent_on_question(monkeypatch, harness):
    """Re-running as the extractor produces more must not duplicate."""
    client, bq, _ = harness
    session = _FakeSession(_FakeClinic(),
                           existing_questions=["Do you take Blue Cross?"])

    def _session():
        yield session
    app.dependency_overrides[get_session] = _session

    body = client.post(URL).json()
    assert body["imported"] == 1
    assert body["skipped"] == 1
    assert [r.question for r in session.added] == ["Where do I park?"]


def test_overlong_question_is_skipped_not_fatal(monkeypatch, harness):
    """question is String(512); one oversized extraction must not abort the
    whole import."""
    client, bq, session = harness
    bq.rows = [
        _Row(question="x" * 600, answer="too long", complete_call_id="C1"),
        _Row(question="Where do I park?", answer="Out front.",
             complete_call_id="C2"),
    ]
    body = client.post(URL).json()
    assert body["imported"] == 1
    assert [r.question for r in session.added] == ["Where do I park?"]
