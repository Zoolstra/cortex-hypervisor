"""clinic_voice_agent_faq: curated FAQ set the voice agent can retrieve

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-10

FAQ content reaches the agent through a RETRIEVAL TOOL, never the system prompt
— the ACNA assistant is a role-compiled single-job specialist (see
``api/voice_agent/roles.py``) and pasting a growing corpus into that prompt is
exactly the salience inversion the role architecture exists to fix.

Why approval lives HERE and not on ``ClinicData.faq.voice_assistant``:

  * ``ClinicData.faq`` is ETL-owned and append-only. Nothing in the repo has
    ever written ``voice_assistant = TRUE`` — the extractor hardcodes False and
    only the retired standalone builder ever read the flag, so there was no
    approval path at all.
  * BigQuery row mutation is the wrong tool for a dashboard toggle:
    ``deps.py::bq_update`` raises 409 while a row is in the streaming buffer.
  * Approval is CONFIG, and config lives in Cloud SQL by architectural rule;
    BigQuery is for analytics/PHI reads.

So the split is: curated text + approval state in Cloud SQL (this table), and
embeddings + ``VECTOR_SEARCH`` in BigQuery (``ClinicData.faq_embeddings``),
because MySQL 8 has no vector type. ``embedding_synced_at`` is the seam between
them — NULL means "approved but not yet embedded", which is the state a failed
or pending sync leaves behind and the only way to spot drift between the two
stores.

``source`` distinguishes an LLM extraction from a human-authored entry. Rows
imported from ``ClinicData.faq`` are transcript-derived guesses and land
``approved = 0``; a patient-facing voice answer should never go live on the
strength of an unreviewed extraction.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "clinic_voice_agent_faq",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "clinic_id", sa.CHAR(36),
            sa.ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("question", sa.String(512), nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        sa.Column(
            "source",
            sa.Enum("etl", "manual", name="va_faq_source_enum"),
            nullable=False, server_default="manual",
        ),
        # complete_call_id the extraction came from, for provenance during
        # review. Null on hand-authored rows.
        sa.Column("source_call_id", sa.String(128)),
        sa.Column("approved", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("approved_by", sa.String(255)),
        # NULL = not embedded (never approved, or a sync that hasn't landed).
        sa.Column("embedding_synced_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.text(
                      "CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")),
        # Re-importing suggestions must be idempotent, and the same question
        # must not exist twice for one clinic. Question is the natural key;
        # MySQL caps a utf8mb4 index key at 3072 bytes so 512 chars fits.
        sa.UniqueConstraint("clinic_id", "question",
                            name="uq_va_faq_clinic_question"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    # The retrieval-sync path and the dashboard both filter on approval state.
    op.create_index(
        "ix_va_faq_clinic_approved",
        "clinic_voice_agent_faq", ["clinic_id", "approved"],
    )


def downgrade() -> None:
    op.drop_index("ix_va_faq_clinic_approved", table_name="clinic_voice_agent_faq")
    op.drop_table("clinic_voice_agent_faq")
