"""enable acna_troubleshooting protocol for ACNA

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-20

Turns on the clinic-scoped ``acna_troubleshooting`` protocol for ACNA. Config is
left empty for now — the prompt fragment then renders a safe generic stance
(offer a service appointment or take a message for any hearing-aid issue). Once
the clinic supplies the actual self-check steps, they go into this row's
``config`` JSON (``{"entries": [...]}``); no further migration needed.

Dual-writes ``clinic_protocols`` (read source of truth) and legacy
``voice_agent_capabilities``, matching the toggle endpoint + migration 0014.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACNA_CLINIC_ID = "0b5f0929-31fb-4e21-9dd4-030bd040335d"
PROTOCOL_ID = "acna_troubleshooting"


def _set_enabled(enabled: int, updater: str) -> None:
    bind = op.get_bind()
    for table, id_col in (
        ("clinic_protocols", "protocol_id"),
        ("voice_agent_capabilities", "capability_id"),
    ):
        bind.execute(
            text(
                f"INSERT INTO {table} (clinic_id, {id_col}, enabled, updated_by) "
                f"VALUES (:cid, :pid, :en, :ub) "
                f"ON DUPLICATE KEY UPDATE enabled = :en, updated_by = :ub"
            ),
            {"cid": ACNA_CLINIC_ID, "pid": PROTOCOL_ID, "en": enabled, "ub": updater},
        )


def upgrade() -> None:
    _set_enabled(1, "migration-0018")


def downgrade() -> None:
    _set_enabled(0, "migration-0018-downgrade")
