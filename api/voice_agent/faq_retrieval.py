"""FAQ semantic retrieval — embeddings in BigQuery, approval state in Cloud SQL.

The voice agent reaches clinic FAQ content through a TOOL, never the system
prompt. That is a deliberate architectural constraint, not a preference: the
ACNA assistant is a role-compiled single-job specialist because a generalist
prompt with several competing jobs made gpt-4o skip its booking tools (see
``api/voice_agent/roles.py``). A corpus that grows every time staff approve an
answer would reintroduce exactly that dilution, and would force an assistant
re-sync on every edit. Retrieval keeps the prompt fixed-size and makes an
approval effective immediately.

Two stores, split along the codebase's existing line:

  * **Cloud SQL** (``clinic_voice_agent_faq``) — curated question/answer text
    and approval state. Config, so it lives here by architectural rule.
  * **BigQuery** (``ClinicData.faq_embeddings``) — the vectors and the
    ``VECTOR_SEARCH``, because MySQL 8 has no vector type.

``clinic_voice_agent_faq.id`` is the join key between them (``faq_id`` below) —
stable, already unique, and it makes unapproving an exact single-row delete.
Hashing the question text instead would orphan a vector on every wording edit.

**Writes use DML, never ``insert_rows_json``.** Streamed rows sit in a buffer
where BigQuery refuses MERGE/UPDATE/DELETE for up to ~90 minutes; the same
constraint ``deps.py::bq_update`` raises 409 on. DML has no buffer, so an
approve → edit → unapprove sequence works immediately.

No vector index is created. BigQuery requires a substantial row count before an
IVF index is permitted, and a hand-curated per-clinic FAQ set is orders of
magnitude below it, so ``VECTOR_SEARCH`` runs brute force. At this corpus size
that is the right choice anyway — exact results, no index to refresh.

⚠️ ``EMBEDDING_MODEL_ENDPOINT`` and ``_EMBED_FN`` are the two values that could
not be verified against the live project (no BigQuery access when this was
written). They are isolated here on purpose: confirm them once against
``resources/faq-vector-setup.sql`` and nothing else needs to change.
"""
from __future__ import annotations

import logging

from google.cloud import bigquery

from api.deps import PROJECT, bq_client

log = logging.getLogger(__name__)


FAQ_EMBEDDINGS_TABLE = f"{PROJECT}.ClinicData.faq_embeddings"
# BQML remote model wrapping a Vertex text-embedding endpoint. Created once by
# resources/faq-vector-setup.sql — this code never creates it.
EMBEDDING_MODEL = f"{PROJECT}.ClinicData.faq_embedding_model"
# Recorded on every row so a model change is detectable rather than silently
# mixing incompatible vector spaces (embeddings from different models are not
# comparable, and the search would degrade quietly rather than error).
EMBEDDING_MODEL_ENDPOINT = "text-embedding-005"
_EMBED_FN = "ML.GENERATE_EMBEDDING"

# Vertex embedding models are asymmetric: documents and queries are embedded
# with different task types, and using one for both measurably degrades recall.
_TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
_TASK_QUERY = "RETRIEVAL_QUERY"

# COSINE matches how the text-embedding models are trained/normalised.
_DISTANCE_TYPE = "COSINE"

# Guard rails on tool-supplied values.
_MAX_TOP_K = 10
_DEFAULT_TOP_K = 3
# Cosine DISTANCE (0 = identical). Anything past this is a non-answer; serving
# it would have the agent confidently read out an unrelated FAQ.
_DEFAULT_MAX_DISTANCE = 0.35


def _embed_subquery(task_type: str, param: str = "question") -> str:
    """SQL producing a single-row ``embedding`` column for @<param>.

    ``flatten_json_output`` gives the plain ARRAY<FLOAT64> rather than the
    wrapped JSON, which is what both the table column and VECTOR_SEARCH want.
    """
    return f"""
      SELECT ml_generate_embedding_result AS embedding
      FROM {_EMBED_FN}(
        MODEL `{EMBEDDING_MODEL}`,
        (SELECT @{param} AS content),
        STRUCT(TRUE AS flatten_json_output, '{task_type}' AS task_type)
      )
    """


def sync_faq_embedding(
    *, clinic_id: str, faq_id: int, question: str, answer: str,
) -> None:
    """Embed one approved FAQ and upsert it into the serving table.

    Idempotent on ``(clinic_id, faq_id)`` — re-approving or editing an answer
    overwrites in place. The answer is stored alongside the vector so retrieval
    is a single query with no Cloud SQL round trip mid-call.
    """
    sql = f"""
    MERGE `{FAQ_EMBEDDINGS_TABLE}` T
    USING (
      SELECT
        @clinic_id AS clinic_id,
        @faq_id    AS faq_id,
        @question  AS question,
        @answer    AS answer,
        embedding,
        @model     AS model,
        CURRENT_TIMESTAMP() AS embedded_at
      FROM ({_embed_subquery(_TASK_DOCUMENT)})
    ) S
    ON T.clinic_id = S.clinic_id AND T.faq_id = S.faq_id
    WHEN MATCHED THEN UPDATE SET
      question = S.question, answer = S.answer, embedding = S.embedding,
      model = S.model, embedded_at = S.embedded_at
    WHEN NOT MATCHED THEN INSERT
      (clinic_id, faq_id, question, answer, embedding, model, embedded_at)
      VALUES (S.clinic_id, S.faq_id, S.question, S.answer, S.embedding,
              S.model, S.embedded_at)
    """
    params = [
        bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
        bigquery.ScalarQueryParameter("faq_id", "INT64", int(faq_id)),
        bigquery.ScalarQueryParameter("question", "STRING", question),
        bigquery.ScalarQueryParameter("answer", "STRING", answer),
        bigquery.ScalarQueryParameter("model", "STRING", EMBEDDING_MODEL_ENDPOINT),
    ]
    bq_client.query(
        sql, job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result()
    log.info("faq embedding synced clinic=%s faq_id=%s", clinic_id, faq_id)


def delete_faq_embedding(*, clinic_id: str, faq_id: int) -> None:
    """Remove one FAQ from the serving table (unapproved or deleted).

    Scoped by clinic as well as id so a mismatched pair can never delete
    another clinic's row.
    """
    sql = f"""
    DELETE FROM `{FAQ_EMBEDDINGS_TABLE}`
    WHERE clinic_id = @clinic_id AND faq_id = @faq_id
    """
    params = [
        bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
        bigquery.ScalarQueryParameter("faq_id", "INT64", int(faq_id)),
    ]
    bq_client.query(
        sql, job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result()
    log.info("faq embedding deleted clinic=%s faq_id=%s", clinic_id, faq_id)


def search_faqs(
    *,
    clinic_id: str,
    question: str,
    top_k: int = _DEFAULT_TOP_K,
    max_distance: float = _DEFAULT_MAX_DISTANCE,
) -> list[dict]:
    """Semantic-search this clinic's approved FAQs. Returns closest first.

    Each result is ``{question, answer, distance}``. An empty list means no
    stored FAQ was close enough — the caller MUST treat that as "I don't know"
    rather than letting the model improvise an answer.

    **Clinic scoping is inside the VECTOR_SEARCH base subquery, not a WHERE on
    the result.** Filtering after the fact would let a nearer FAQ from another
    clinic consume a top_k slot and be returned — a cross-tenant content leak.
    Pre-filtering is the only correct form here; same mandatory-filter
    discipline as the PHI queries in ``pms/blueprint.py``.

    ``top_k`` is clamped and inlined as an integer literal because BigQuery
    does not accept a query parameter for that named argument. It is an int
    after clamping, so there is no injection surface.
    """
    k = max(1, min(int(top_k), _MAX_TOP_K))
    sql = f"""
    SELECT
      base.question  AS question,
      base.answer    AS answer,
      distance
    FROM VECTOR_SEARCH(
      (
        SELECT faq_id, question, answer, embedding
        FROM `{FAQ_EMBEDDINGS_TABLE}`
        WHERE clinic_id = @clinic_id          -- MANDATORY: see docstring
      ),
      'embedding',
      ({_embed_subquery(_TASK_QUERY)}),
      query_column_to_search => 'embedding',
      top_k => {k},
      distance_type => '{_DISTANCE_TYPE}'
    )
    WHERE distance <= @max_distance
    ORDER BY distance
    """
    params = [
        bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
        bigquery.ScalarQueryParameter("question", "STRING", question),
        bigquery.ScalarQueryParameter("max_distance", "FLOAT64", float(max_distance)),
    ]
    rows = bq_client.query(
        sql, job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result()
    return [
        {
            "question": r["question"],
            "answer": r["answer"],
            "distance": round(float(r["distance"]), 4),
        }
        for r in rows
    ]
