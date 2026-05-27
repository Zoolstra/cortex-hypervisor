"""rename + collapse protocol ids for the v2 protocol set

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-27

The v2 protocol set introduces:
  - ``verify_caller_identification`` (rename of ``patient_match``)
  - ``search_appointment_availability`` (collapse of ``list_appointment_types``
    + ``find_available_slots`` into one multi-tool protocol)
  - new toggleable protocols: ``locate_appointment``, ``book_appointment``,
    ``cancel_appointment``, ``reschedule_appointment``

This migration only renames + collapses existing rows in
``clinic_protocols`` (and the legacy ``voice_agent_capabilities`` table,
which is still dual-written during the migration window). New protocols
land with no rows — they're opt-in per clinic via the toggle endpoint.

Collapse semantics: a clinic with EITHER ``list_appointment_types`` OR
``find_available_slots`` enabled gets ``search_appointment_availability``
enabled. Updated_at uses the latest of the two source rows; updated_by
uses the source row's value (preferring the most recent).

Orphan rows (``search_availability`` and any other unknown id) are left
untouched — the factory ignores unknown ids, so they're harmless.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Rename map: ``old_id`` → ``new_id``. Applied to both tables.
_RENAMES = {
    "patient_match": "verify_caller_identification",
}

# Collapse: the set of source ids that should be folded into a single
# destination id, ORed on enablement. ``updated_at`` / ``updated_by`` /
# ``config`` come from the row with the most recent ``updated_at``.
_COLLAPSE_DEST = "search_appointment_availability"
_COLLAPSE_SOURCES = ("list_appointment_types", "find_available_slots")


def _rename_simple(table: str, old_id_col: str) -> None:
    """Issue a straightforward UPDATE for the simple renames."""
    for old, new in _RENAMES.items():
        op.execute(
            sa.text(
                f"UPDATE {table} SET {old_id_col} = :new WHERE {old_id_col} = :old"
            ).bindparams(new=new, old=old)
        )


def _collapse(table: str, id_col: str) -> None:
    """Collapse the source rows into one destination row per clinic.

    Strategy (MySQL-safe, no CTEs that touch the modified table mid-statement):

      1. INSERT/UPDATE the destination row from the MAX(enabled) and the
         most-recent metadata of the source rows.
      2. DELETE the source rows.
    """
    sources_in = ", ".join(f"'{s}'" for s in _COLLAPSE_SOURCES)

    # MySQL 8 supports ON DUPLICATE KEY UPDATE on composite PKs. Wrap the
    # MAX(enabled) etc. so an existing search_appointment_availability row
    # (rare but possible if a developer pre-toggled the new id) is merged
    # with the legacy data rather than overwritten.
    op.execute(sa.text(f"""
        INSERT INTO {table}
            (clinic_id, {id_col}, enabled, config, updated_by,
             created_at, updated_at)
        SELECT
            clinic_id,
            '{_COLLAPSE_DEST}' AS {id_col},
            MAX(enabled) AS enabled,
            (SELECT config FROM {table} s2
              WHERE s2.clinic_id = s1.clinic_id
                AND s2.{id_col} IN ({sources_in})
              ORDER BY updated_at DESC LIMIT 1) AS config,
            (SELECT updated_by FROM {table} s2
              WHERE s2.clinic_id = s1.clinic_id
                AND s2.{id_col} IN ({sources_in})
              ORDER BY updated_at DESC LIMIT 1) AS updated_by,
            MIN(created_at) AS created_at,
            MAX(updated_at) AS updated_at
        FROM {table} s1
        WHERE {id_col} IN ({sources_in})
        GROUP BY clinic_id
        ON DUPLICATE KEY UPDATE
            enabled    = GREATEST({table}.enabled, VALUES(enabled)),
            updated_at = GREATEST({table}.updated_at, VALUES(updated_at)),
            updated_by = COALESCE(VALUES(updated_by), {table}.updated_by)
    """))

    # Delete the source rows now that the destination is populated.
    op.execute(sa.text(
        f"DELETE FROM {table} WHERE {id_col} IN ({sources_in})"
    ))


def upgrade() -> None:
    # Both tables in lock-step so the dual-write invariant from step 3
    # holds across the rename.
    for table, id_col in [
        ("clinic_protocols", "protocol_id"),
        ("voice_agent_capabilities", "capability_id"),
    ]:
        _rename_simple(table, id_col)
        _collapse(table, id_col)


def downgrade() -> None:
    # Reverse the simple rename; collapse is non-reversible (we don't
    # know how the merged row split between the two source ids).
    # The collapse direction is one-way by design — to revert, restore
    # from the pre-0005 snapshot or recreate the rows manually.
    for table, id_col in [
        ("clinic_protocols", "protocol_id"),
        ("voice_agent_capabilities", "capability_id"),
    ]:
        for old, new in _RENAMES.items():
            op.execute(
                sa.text(
                    f"UPDATE {table} SET {id_col} = :old WHERE {id_col} = :new"
                ).bindparams(new=new, old=old)
            )
