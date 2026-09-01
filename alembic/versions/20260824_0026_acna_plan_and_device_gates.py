"""ACNA: make the 2026-08-24 plan + device gates explicit in the stored config

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-24

Follow-up to a live test call. A patient whose only care-plan label was the
feed placeholder 'Other', with no hearing aid on file and no insurer on record,
was told "you're due for an annual hearing test and it's covered by your plan"
and offered times. Nothing had established coverage: three independent
fail-open defaults stacked (unknown plan → funded, payer 'Other' → funded, and
no device check at all).

The engine now (a) refers an unrecognized plan to staff instead of assuming
coverage and (b) requires a hearing aid on file before a FUNDED annual — the
benefit is a care-plan benefit and a care plan services a device. Both gates
fire only when the caller is DUE.

**This migration is cosmetic-but-load-bearing, not functional.** The new code
defaults already produce the right behaviour for ACNA's row: the retired
``unknown_plan_allows_annual`` key is ignored by the config model
(``extra: ignore``), and the two new fields default to the clinic's confirmed
answer. What the row would otherwise keep is a LIE — a dead
``unknown_plan_allows_annual: true`` that reads, to the next operator opening
the record, as "unknown plans are funded here". Same argument 0025 made for
absent-vs-explicit outcome keys.

So this: drops the dead key, writes the two new ones explicitly, and adds
``REFER_TO_STAFF_PLAN_REVIEW`` to ``outcome_booking`` as an explicit NULL
(referrals are never agent-bookable; absent already behaved that way).

Values are hardcoded rather than imported from the config model — a migration
must reproduce the same result later even after the model's defaults move on.
Only ever fills keys that are ABSENT, so an operator's explicit tuning is never
overwritten and the migration is safe to re-run.
"""
import json
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACNA_CLINIC_ID = "0b5f0929-31fb-4e21-9dd4-030bd040335d"
DECISION_PROTOCOL = "acna_determine_appointment"

# Retired by this revision. Superseded by the three-valued unknown_plan_action:
# a bool could not express "hand it to a person".
LEGACY_KEY = "unknown_plan_allows_annual"

NEW_SCALARS: dict[str, object] = {
    "unknown_plan_action": "refer",
    "require_hearing_aid_for_funded_annual": True,
}

NEW_OUTCOME = "REFER_TO_STAFF_PLAN_REVIEW"

_MIGRATION_ACTOR = "migration-0026"


def _load_config(conn) -> dict | None:
    row = conn.execute(
        text(
            "SELECT config FROM clinic_protocols "
            "WHERE clinic_id = :c AND protocol_id = :p"
        ),
        {"c": ACNA_CLINIC_ID, "p": DECISION_PROTOCOL},
    ).fetchone()
    if row is None:
        return None
    if not row[0]:
        return {}
    parsed = json.loads(row[0])
    return parsed if isinstance(parsed, dict) else {}


def _save_config(conn, config: dict) -> None:
    conn.execute(
        text(
            "UPDATE clinic_protocols SET config = :cfg, updated_by = :by "
            "WHERE clinic_id = :c AND protocol_id = :p"
        ),
        {
            "cfg": json.dumps(config),
            "by": _MIGRATION_ACTOR,
            "c": ACNA_CLINIC_ID,
            "p": DECISION_PROTOCOL,
        },
    )


def upgrade() -> None:
    conn = op.get_bind()
    cfg = _load_config(conn)
    if cfg is None:
        return  # protocol not enabled for this clinic — nothing to patch

    changed = cfg.pop(LEGACY_KEY, None) is not None

    for key, value in NEW_SCALARS.items():
        if key not in cfg:           # absent only — never overwrite intent
            cfg[key] = value
            changed = True

    outcomes = cfg.get("outcome_booking")
    if not isinstance(outcomes, dict):
        outcomes = {}
    if NEW_OUTCOME not in outcomes:
        outcomes[NEW_OUTCOME] = None
        cfg["outcome_booking"] = outcomes
        changed = True

    if changed:
        _save_config(conn, cfg)


def downgrade() -> None:
    """Restore the pre-0026 shape: legacy key back, new keys gone.

    ``unknown_plan_allows_annual: true`` is rewritten because that WAS the
    stored value before this revision — reverting the code without it would
    leave the row silently on the model default. Only removes a new key still
    holding the value this migration wrote, so a later hand edit survives.
    """
    conn = op.get_bind()
    cfg = _load_config(conn)
    if cfg is None:
        return

    changed = False
    if LEGACY_KEY not in cfg:
        cfg[LEGACY_KEY] = True
        changed = True

    for key, value in NEW_SCALARS.items():
        if key in cfg and cfg[key] == value:
            del cfg[key]
            changed = True

    outcomes = cfg.get("outcome_booking")
    if isinstance(outcomes, dict) and outcomes.get(NEW_OUTCOME, "x") is None:
        del outcomes[NEW_OUTCOME]
        cfg["outcome_booking"] = outcomes
        changed = True

    if changed:
        _save_config(conn, cfg)
