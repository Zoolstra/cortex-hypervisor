"""Enable ACNA placeholder-grid protocols; disable ACNA's generic search/book

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-20

Audiology Clinic of Northern Alberta (ACNA) books annuals against a
placeholder-grid model that Blueprint's native online-booking availability
can't express (see api/voice_agent/protocols/acna_placeholder.py). This
migration switches ACNA over to the two clinic-scoped protocols:

  - enables  ``acna_search_availability`` (seeded with the Annual type-pair
    {placeholder 200 'Z Space RA12M' → real 207 'Annual'}) and
    ``acna_book_appointment``;
  - disables ACNA's generic ``search_appointment_availability`` and
    ``book_appointment`` so the assistant doesn't get two tools named
    ``find_available_slots`` / ``book_appointment``.

Writes BOTH ``clinic_protocols`` (the read source of truth) and the legacy
``voice_agent_capabilities`` table, matching the dual-write the toggle
endpoint performs, so a code rollback stays consistent. Idempotent via
INSERT ... ON DUPLICATE KEY UPDATE. Clinic-scoped, so it hardcodes ACNA's id.
"""
import json
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACNA_CLINIC_ID = "0b5f0929-31fb-4e21-9dd4-030bd040335d"

_SEARCH_CONFIG = json.dumps({
    "type_pairs": [
        {"placeholder_event_type_id": 200, "real_event_type_id": 207,
         "display_name": "Annual"},
    ]
})

# (protocol_id, enabled, config-json-or-None)
_ENABLE = [
    ("acna_search_availability", 1, _SEARCH_CONFIG),
    ("acna_book_appointment", 1, None),
]
_DISABLE = [
    ("search_appointment_availability", 0, None),
    ("book_appointment", 0, None),
]


def _upsert(bind, table: str, id_col: str, clinic_id: str, pid: str,
            enabled: int, config: str | None, updater: str) -> None:
    # config=None means "leave existing config untouched"; a JSON string is
    # cast so it lands as a JSON object, not a JSON string scalar.
    if config is None:
        bind.execute(
            text(
                f"INSERT INTO {table} (clinic_id, {id_col}, enabled, updated_by) "
                f"VALUES (:cid, :pid, :en, :ub) "
                f"ON DUPLICATE KEY UPDATE enabled = :en, updated_by = :ub"
            ),
            {"cid": clinic_id, "pid": pid, "en": enabled, "ub": updater},
        )
    else:
        bind.execute(
            text(
                f"INSERT INTO {table} (clinic_id, {id_col}, enabled, config, updated_by) "
                f"VALUES (:cid, :pid, :en, CAST(:cfg AS JSON), :ub) "
                f"ON DUPLICATE KEY UPDATE enabled = :en, config = CAST(:cfg AS JSON), "
                f"updated_by = :ub"
            ),
            {"cid": clinic_id, "pid": pid, "en": enabled, "cfg": config, "ub": updater},
        )


def _apply(rows: list[tuple[str, int, str | None]], updater: str) -> None:
    bind = op.get_bind()
    for pid, enabled, config in rows:
        _upsert(bind, "clinic_protocols", "protocol_id",
                ACNA_CLINIC_ID, pid, enabled, config, updater)
        _upsert(bind, "voice_agent_capabilities", "capability_id",
                ACNA_CLINIC_ID, pid, enabled, config, updater)


def upgrade() -> None:
    _apply(_ENABLE + _DISABLE, "migration-0014")


def downgrade() -> None:
    # Re-enable the generic protocols and turn the ACNA-specific ones off.
    _apply(
        [(pid, 1, None) for pid, _, _ in _DISABLE]
        + [(pid, 0, None) for pid, _, _ in _ENABLE],
        "migration-0014-downgrade",
    )
