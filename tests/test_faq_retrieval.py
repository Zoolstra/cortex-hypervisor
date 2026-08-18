"""Tests for FAQ semantic retrieval.

Two properties matter most here and neither is visible from a passing search:

  1. **Clinic scoping happens INSIDE the VECTOR_SEARCH base subquery.** If it
     were a WHERE on the results, a nearer FAQ belonging to another clinic would
     consume a top_k slot and be returned — a cross-tenant content leak that no
     functional test would notice, because the search still "works".
  2. **Documents and queries use different task types.** Vertex embedding models
     are asymmetric; using one task type for both silently degrades recall
     rather than erroring.

Both are pinned against the generated SQL, since there is no BigQuery in the
test environment.
"""
from __future__ import annotations

import pytest

from api.voice_agent import faq_retrieval


CLINIC = "0b5f0929-31fb-4e21-9dd4-030bd040335d"
OTHER_CLINIC = "11111111-2222-3333-4444-555555555555"


class _FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class BQCapture:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.rows: list[dict] = []

    def query(self, sql, job_config=None):
        params = {}
        if job_config is not None and job_config.query_parameters:
            params = {p.name: p.value for p in job_config.query_parameters}
        self.calls.append((sql, params))
        return _FakeJob(self.rows)

    @property
    def last_sql(self) -> str:
        return self.calls[-1][0]

    @property
    def last_params(self) -> dict:
        return self.calls[-1][1]


@pytest.fixture
def cap(monkeypatch):
    c = BQCapture()
    monkeypatch.setattr("api.voice_agent.faq_retrieval.bq_client", c)
    return c


# ── Tenant isolation ─────────────────────────────────────────────────────────


def test_clinic_filter_is_inside_the_vector_search_base(cap):
    faq_retrieval.search_faqs(clinic_id=CLINIC, question="do you have parking?")
    sql = cap.last_sql
    base_start = sql.index("VECTOR_SEARCH(")
    # The clinic predicate must appear before the column-to-search argument that
    # closes the base subquery — i.e. it is part of the scanned set, not a
    # post-filter on the ranked results.
    col_arg = sql.index("'embedding',", base_start)
    base_subquery = sql[base_start:col_arg]
    assert "WHERE clinic_id = @clinic_id" in base_subquery
    assert cap.last_params["clinic_id"] == CLINIC


def test_clinic_id_is_parameterised_not_interpolated(cap):
    faq_retrieval.search_faqs(clinic_id=OTHER_CLINIC, question="hours?")
    assert OTHER_CLINIC not in cap.last_sql
    assert cap.last_params["clinic_id"] == OTHER_CLINIC


def test_delete_is_scoped_by_clinic_and_id(cap):
    faq_retrieval.delete_faq_embedding(clinic_id=CLINIC, faq_id=7)
    sql = cap.last_sql
    assert "clinic_id = @clinic_id" in sql and "faq_id = @faq_id" in sql
    assert cap.last_params == {"clinic_id": CLINIC, "faq_id": 7}


def test_merge_matches_on_both_clinic_and_faq_id(cap):
    faq_retrieval.sync_faq_embedding(
        clinic_id=CLINIC, faq_id=42, question="q", answer="a",
    )
    sql = cap.last_sql
    assert "T.clinic_id = S.clinic_id AND T.faq_id = S.faq_id" in sql


# ── Embedding task types ─────────────────────────────────────────────────────


def test_documents_are_embedded_as_retrieval_document(cap):
    faq_retrieval.sync_faq_embedding(
        clinic_id=CLINIC, faq_id=1, question="q", answer="a",
    )
    assert "RETRIEVAL_DOCUMENT" in cap.last_sql
    assert "RETRIEVAL_QUERY" not in cap.last_sql


def test_queries_are_embedded_as_retrieval_query(cap):
    faq_retrieval.search_faqs(clinic_id=CLINIC, question="what should I bring?")
    assert "RETRIEVAL_QUERY" in cap.last_sql
    assert "RETRIEVAL_DOCUMENT" not in cap.last_sql


def test_embedding_output_is_flattened_to_an_array(cap):
    # Without flatten_json_output the column is wrapped JSON and neither the
    # table column nor VECTOR_SEARCH can consume it.
    faq_retrieval.search_faqs(clinic_id=CLINIC, question="q")
    assert "flatten_json_output" in cap.last_sql


# ── Writes avoid the streaming buffer ────────────────────────────────────────


def test_sync_uses_dml_merge_not_streaming_insert(cap):
    # Streamed rows are un-MERGEable for ~90 minutes, so an approve → edit →
    # unapprove sequence would fail. DML has no buffer.
    faq_retrieval.sync_faq_embedding(
        clinic_id=CLINIC, faq_id=1, question="q", answer="a",
    )
    assert cap.last_sql.strip().startswith("MERGE")


# ── Guard rails ──────────────────────────────────────────────────────────────


def test_top_k_is_clamped_and_inlined_as_an_integer(cap):
    faq_retrieval.search_faqs(clinic_id=CLINIC, question="q", top_k=999)
    assert f"top_k => {faq_retrieval._MAX_TOP_K}" in cap.last_sql
    faq_retrieval.search_faqs(clinic_id=CLINIC, question="q", top_k=0)
    assert "top_k => 1" in cap.last_sql


def test_top_k_cannot_carry_sql_through_the_literal(cap):
    # It is interpolated, so prove non-ints are rejected rather than embedded.
    with pytest.raises((ValueError, TypeError)):
        faq_retrieval.search_faqs(
            clinic_id=CLINIC, question="q", top_k="3; DROP TABLE x",
        )


def test_distance_threshold_filters_and_is_parameterised(cap):
    faq_retrieval.search_faqs(clinic_id=CLINIC, question="q", max_distance=0.2)
    assert "WHERE distance <= @max_distance" in cap.last_sql
    assert cap.last_params["max_distance"] == 0.2


def test_results_are_ordered_closest_first(cap):
    faq_retrieval.search_faqs(clinic_id=CLINIC, question="q")
    assert "ORDER BY distance" in cap.last_sql


# ── Result shape ─────────────────────────────────────────────────────────────


def test_rows_are_mapped_to_question_answer_distance(cap):
    cap.rows = [
        {"question": "Do you have parking?", "answer": "Yes, free out front.",
         "distance": 0.10412},
    ]
    out = faq_retrieval.search_faqs(clinic_id=CLINIC, question="parking?")
    assert out == [{
        "question": "Do you have parking?",
        "answer": "Yes, free out front.",
        "distance": 0.1041,
    }]


def test_no_matches_returns_empty_list(cap):
    cap.rows = []
    assert faq_retrieval.search_faqs(clinic_id=CLINIC, question="anything") == []


# ── Protocol contract ────────────────────────────────────────────────────────


def test_protocol_is_registered_and_toggleable():
    from api.voice_agent.protocols import (
        PROTOCOL_METADATA_BY_ID, PROTOCOL_REGISTRY, toggleable_protocols,
    )
    from api.voice_agent.protocols.faq_lookup import FaqLookupProtocol

    assert PROTOCOL_REGISTRY["faq_lookup"] is FaqLookupProtocol
    # Must appear in the dashboard list, or it can never be turned on.
    assert "faq_lookup" in PROTOCOL_METADATA_BY_ID
    # NOT always-on: a clinic with no approved FAQs shouldn't carry a tool that
    # can only ever return nothing.
    assert FaqLookupProtocol.always_on is False
    assert FaqLookupProtocol in toggleable_protocols()


def test_protocol_is_pms_and_clinic_agnostic():
    from api.voice_agent.protocols.faq_lookup import FaqLookupProtocol
    # Nothing about FAQ retrieval is PMS- or clinic-specific.
    assert FaqLookupProtocol.supported_pms is None
    assert FaqLookupProtocol.supported_clinics is None
    assert FaqLookupProtocol.depends_on == ()


def test_protocol_exposes_one_tool_with_a_question_parameter():
    from api.voice_agent.protocols.faq_lookup import FaqLookupProtocol
    proto = FaqLookupProtocol(
        clinic_id=CLINIC, clinic_name="ACNA", pms_type="blueprint",
        credential_id="cred_test",
    )
    tools = proto.tools()
    assert len(tools) == 1
    assert tools[0]["name"] == "answer_clinic_question"
    assert tools[0]["type"] == "apiRequest"
    assert tools[0]["body"]["required"] == ["question"]
    assert tools[0]["url"].endswith(f"/clinics/{CLINIC}/voice_agent/faq/search")


def test_config_rejects_out_of_range_thresholds():
    from pydantic import ValidationError
    from api.voice_agent.protocols.faq_lookup import FaqLookupConfig

    assert FaqLookupConfig().top_k == 3
    with pytest.raises(ValidationError):
        FaqLookupConfig(top_k=0)
    with pytest.raises(ValidationError):
        FaqLookupConfig(top_k=50)
    with pytest.raises(ValidationError):
        FaqLookupConfig(max_distance=-0.1)


def test_prompt_fragment_carries_instructions_not_answers():
    from api.voice_agent.protocols.faq_lookup import FaqLookupProtocol
    proto = FaqLookupProtocol(
        clinic_id=CLINIC, clinic_name="ACNA", pms_type="none",
        credential_id="cred_test",
    )
    frag = proto.prompt_fragment
    assert "answer_clinic_question" in frag
    # The two guards that keep a retrieval miss from becoming a hallucination
    # and keep this tool away from patient data.
    assert "Never improvise an answer" in frag
    assert "General information only" in frag
