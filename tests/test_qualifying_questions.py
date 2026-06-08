"""Tests for the new-patient qualifying-questions feature.

Three layers, mirroring the suite's existing conventions (no real DB):
  1. ORM model invariants — table name, columns, PK/FK, Clinic relationship.
  2. Factory rendering — _render_qualifying_questions + _stage_3a_new_patient
     (ordinal sort, inactive filtering, empty→omitted, serialization hints).
  3. CRUD endpoints — TestClient with get_session/verify_token overridden and
     an in-memory FakeDb; covers ordinal-append, validation, cross-clinic
     ownership, and the canonical full-list response contract.

Behavioural invariants under test:
  - named, active questions render in ordinal order; inactive ones are hidden
  - a clinic with no questions gets NO screening block (no hardcoded defaults)
  - Stage 3a tells the agent to carry answers into book notes AND ticket details
  - create appends at max(ordinal)+1 when ordinal is omitted
  - empty/whitespace question_text is rejected with 400 (not 404)
  - one clinic cannot mutate another clinic's question rows (404)
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import app
from api.core.db import get_session
from api.deps import verify_token
from api.core.orm import (
    Clinic,
    ClinicVoiceAgentConfiguration,
    ClinicVoiceAgentQualifyingQuestion,
)
from api.voice_agent.factory import (
    _render_qualifying_questions,
    _stage_3a_new_patient,
)
from api.voice_agent.voice_agent import (
    _QualifyingQuestionCreate,
    _QualifyingQuestionUpdate,
    _question_to_item,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _q(qid, text, *, ordinal=0, active=True, clinic_id="C1", expected=None):
    """Construct a qualifying-question ORM row in memory (no session)."""
    return ClinicVoiceAgentQualifyingQuestion(
        id=qid, clinic_id=clinic_id, ordinal=ordinal,
        question_text=text, expected_responses=expected, active=active,
    )


# ── 1. ORM model ──────────────────────────────────────────────────────────────

def test_orm_table_and_columns():
    t = ClinicVoiceAgentQualifyingQuestion.__table__
    assert t.name == "clinic_voice_agent_qualifying_question"
    assert set(t.columns.keys()) == {
        "id", "clinic_id", "ordinal", "question_text",
        "expected_responses", "active", "created_at", "updated_at",
    }
    assert list(t.primary_key.columns.keys()) == ["id"]
    # question_text is the required field; expected_responses is optional
    assert t.columns["question_text"].nullable is False
    assert t.columns["expected_responses"].nullable is True


def test_orm_clinic_fk_cascades():
    fk = next(iter(ClinicVoiceAgentQualifyingQuestion.__table__.columns["clinic_id"].foreign_keys))
    assert fk.column.table.name == "clinics"
    assert fk.ondelete == "CASCADE"


def test_orm_relationship_on_clinic():
    # Clinic gained a list-relationship back to the new table.
    rel = Clinic.__mapper__.relationships["voice_agent_qualifying_questions"]
    assert rel.mapper.class_ is ClinicVoiceAgentQualifyingQuestion
    assert rel.uselist is True


# ── 2. Factory rendering ──────────────────────────────────────────────────────

def test_render_sorts_by_ordinal_and_drops_inactive():
    out = _render_qualifying_questions([
        _q(2, "Second", ordinal=1),
        _q(1, "First", ordinal=0),
        _q(3, "Hidden", ordinal=2, active=False),
    ])
    assert "Hidden" not in out
    # ordered: First (#1) before Second (#2)
    assert out.index("1) First") < out.index("2) Second")


def test_render_empty_returns_blank():
    assert _render_qualifying_questions([]) == ""
    # all-inactive also collapses to nothing
    assert _render_qualifying_questions([_q(1, "x", active=False)]) == ""


def test_render_includes_expected_responses():
    out = _render_qualifying_questions([_q(1, "Wear aids?", expected="Yes / No")])
    assert "Wear aids?" in out
    assert "Yes / No" in out


def test_render_collapses_multiline_expected_responses():
    # The dashboard field is a textarea, so guidance may be multi-line; it must
    # collapse onto one indented line so it can't run into the next question.
    out = _render_qualifying_questions([
        _q(1, "Q1", expected="self\nfamily member\nother"),
        _q(2, "Q2", ordinal=1),
    ])
    assert "self; family member; other" in out
    # the next question still renders as its own numbered entry
    assert "2) Q2" in out


def test_render_skips_blank_question_text():
    out = _render_qualifying_questions([_q(1, "   "), _q(2, "Real one", ordinal=1)])
    assert "Real one" in out
    # the blank row produced no numbered entry
    assert out.startswith("1) Real one")


def test_stage_3a_includes_block_and_serialization_hints():
    out = _stage_3a_new_patient([], [_q(1, "Wear aids?")])
    assert "New-patient screening questions" in out
    # tells the agent to carry answers into BOTH sinks
    assert "book_appointment" in out
    assert "screening_answers" in out
    assert "Wear aids?" in out


def test_stage_3a_omits_block_when_no_questions():
    out = _stage_3a_new_patient([], [])
    assert "New-patient screening questions" not in out


# ── 3. Pydantic models + serializer ───────────────────────────────────────────

def test_create_model_defaults():
    m = _QualifyingQuestionCreate(question_text="Q?")
    assert m.ordinal is None       # None → endpoint appends at the end
    assert m.active is True
    assert m.expected_responses is None


def test_update_model_all_optional():
    m = _QualifyingQuestionUpdate()
    assert m.model_dump(exclude_unset=True) == {}


def test_question_to_item_serializes_timestamp():
    row = _q(7, "Q?", ordinal=3, expected="A / B")
    row.updated_at = datetime.datetime(2026, 6, 4, 12, 30, 0)
    item = _question_to_item(row)
    assert item.id == 7
    assert item.question_text == "Q?"
    assert item.expected_responses == "A / B"
    assert item.updated_at == "2026-06-04T12:30:00Z"


def test_question_to_item_handles_null_timestamp():
    # Freshly-constructed rows have updated_at=None (server_default only on insert).
    item = _question_to_item(_q(1, "Q?"))
    assert item.updated_at is None


# ── 4. CRUD endpoints (TestClient + in-memory FakeDb) ─────────────────────────

class _FakeDb:
    """Minimal SQLAlchemy-session stand-in for the qualifying-question routes.

    - ``get(Clinic, id)`` resolves any clinic (active, instance 'inst').
    - ``get(ClinicVoiceAgentQualifyingQuestion, id)`` looks up the in-memory row.
    - ``get(ClinicVoiceAgentConfiguration, id)`` → None, so
      _sync_assistant_if_provisioned no-ops (no VAPI/build_agent_config).
    - ``scalars`` returns rows ordinal-sorted (the only query the routes run).
    """

    def __init__(self, questions=None):
        self._q = {q.id: q for q in (questions or [])}
        self._next = max(self._q, default=0) + 1

    def get(self, model, key):
        if model is Clinic:
            return SimpleNamespace(clinic_id=key, instance_id="inst", deleted_at=None)
        if model is ClinicVoiceAgentQualifyingQuestion:
            return self._q.get(key)
        if model is ClinicVoiceAgentConfiguration:
            return None
        return None

    def scalars(self, _stmt):
        return sorted(self._q.values(), key=lambda r: (r.ordinal, r.id))

    def add(self, row):
        row.id = self._next
        self._next += 1
        self._q[row.id] = row

    def flush(self):
        pass

    def delete(self, row):
        self._q.pop(row.id, None)


@pytest.fixture
def make_client():
    """Build a TestClient bound to a given FakeDb, with auth bypassed as
    super_admin (skips the instance-membership DB lookup)."""
    def _make(fake_db: _FakeDb) -> TestClient:
        app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "t"}
        # Return the fake session directly. (A `lambda: iter([db])` override
        # would hand the *iterator* to the route as `db`; that only works in
        # tests where `db` is never used. These routes call `db.get`, so the
        # override must resolve to the FakeDb itself.)
        app.dependency_overrides[get_session] = lambda: fake_db
        return TestClient(app, raise_server_exceptions=False)
    yield _make
    app.dependency_overrides.clear()


_BASE = "/clinics/C1/voice_agent/qualifying_questions"


def test_list_returns_ordinal_ordered(make_client):
    db = _FakeDb([_q(2, "B", ordinal=1), _q(1, "A", ordinal=0)])
    client = make_client(db)
    resp = client.get(_BASE)
    assert resp.status_code == 200
    body = resp.json()
    assert [q["question_text"] for q in body["questions"]] == ["A", "B"]


def test_create_first_question_gets_ordinal_zero(make_client):
    db = _FakeDb()
    client = make_client(db)
    resp = client.post(_BASE, json={"question_text": "First?"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["questions"]) == 1
    assert body["questions"][0]["ordinal"] == 0
    # un-provisioned clinic → sync no-ops, surfaced (not raised)
    assert body["vapi_sync"]["synced"] is False


def test_create_appends_after_max_ordinal(make_client):
    db = _FakeDb([_q(1, "A", ordinal=0), _q(2, "B", ordinal=5)])
    client = make_client(db)
    resp = client.post(_BASE, json={"question_text": "C?"})
    assert resp.status_code == 200
    new = next(q for q in resp.json()["questions"] if q["question_text"] == "C?")
    assert new["ordinal"] == 6


def test_create_strips_and_persists_expected_responses(make_client):
    db = _FakeDb()
    client = make_client(db)
    resp = client.post(_BASE, json={"question_text": "  Wear aids?  ", "expected_responses": "Yes / No"})
    assert resp.status_code == 200
    q = resp.json()["questions"][0]
    assert q["question_text"] == "Wear aids?"
    assert q["expected_responses"] == "Yes / No"


@pytest.mark.parametrize("bad", ["", "   "])
def test_create_rejects_empty_question_text(make_client, bad):
    client = make_client(_FakeDb())
    resp = client.post(_BASE, json={"question_text": bad})
    assert resp.status_code == 400


def test_update_partial_only_touches_present_fields(make_client):
    db = _FakeDb([_q(1, "A", ordinal=0, active=True)])
    client = make_client(db)
    resp = client.put(f"{_BASE}/1", json={"active": False})
    assert resp.status_code == 200
    q = resp.json()["questions"][0]
    assert q["active"] is False
    assert q["question_text"] == "A"  # untouched


def test_update_ignores_explicit_null_for_not_null_columns(make_client):
    # ordinal/active are NOT NULL; an explicit null in the body must be a
    # no-op (not written), so it can't 500 on the real DB. expected_responses
    # is nullable, so a null there legitimately clears it.
    db = _FakeDb([_q(1, "A", ordinal=3, active=True, expected="keep")])
    client = make_client(db)
    resp = client.put(
        f"{_BASE}/1",
        json={"ordinal": None, "active": None, "expected_responses": None},
    )
    assert resp.status_code == 200
    q = resp.json()["questions"][0]
    assert q["ordinal"] == 3          # unchanged
    assert q["active"] is True        # unchanged
    assert q["expected_responses"] is None  # cleared (nullable)


def test_update_rejects_empty_question_text(make_client):
    db = _FakeDb([_q(1, "A")])
    client = make_client(db)
    resp = client.put(f"{_BASE}/1", json={"question_text": "   "})
    assert resp.status_code == 400


def test_update_cross_clinic_is_404(make_client):
    # Row belongs to a DIFFERENT clinic; mutating it via /clinics/C1 must 404.
    db = _FakeDb([_q(9, "A", clinic_id="OTHER")])
    client = make_client(db)
    resp = client.put(f"{_BASE}/9", json={"active": False})
    assert resp.status_code == 404


def test_delete_removes_and_returns_remaining(make_client):
    db = _FakeDb([_q(1, "A", ordinal=0), _q(2, "B", ordinal=1)])
    client = make_client(db)
    resp = client.delete(f"{_BASE}/1")
    assert resp.status_code == 200
    remaining = [q["id"] for q in resp.json()["questions"]]
    assert remaining == [2]


def test_delete_cross_clinic_is_404(make_client):
    db = _FakeDb([_q(9, "A", clinic_id="OTHER")])
    client = make_client(db)
    resp = client.delete(f"{_BASE}/9")
    assert resp.status_code == 404
