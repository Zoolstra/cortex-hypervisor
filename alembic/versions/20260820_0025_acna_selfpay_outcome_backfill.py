"""ACNA: backfill the outcome_booking keys that never reached the live config

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-20

Fixes a live broken caller path: **the self-pay annual could not be booked.**

``AppointmentDecisionConfig.outcome_booking`` is a whole-dict field, so the
stored value REPLACES the code default rather than merging with it. ACNA's row
was seeded by migration 0020 with the four outcomes that existed then; every
outcome added to the engine afterwards — the self-pay offer and the three
referral outcomes — was simply absent from the live dict. Absent and
"explicitly None" are indistinguishable at the lookup
(``cfg.outcome_booking.get(outcome)``), so those outcomes came back
``bookable: false``.

For the referral outcomes that is the correct answer, so they were harmless. For
``OFFER_SELF_PAY_ANNUAL_HEARING_TEST`` it was not: the engine emits it for any
patient who is due for a test their plan doesn't fund (rule 6), and the prompt
instructs the agent to quote the price and book on agreement. The caller agreed
and the agent then had nothing to book — while the code default said 207 the
whole time. Self-pay is the same appointment as a funded annual; only the
payment conversation differs.

Backfills all four missing keys rather than just the broken one, so the stored
dict matches the engine's canonical outcome set and the next reader can't
mistake an absent key for a deliberate "not bookable". Referral outcomes are
written as explicit NULL — no behaviour change, just no longer implicit.

Values are hardcoded rather than imported from the config model: a migration
must reproduce the same result later even after the model's defaults move on.

Only ever fills keys that are ABSENT — an operator's explicit choice for a key
is never overwritten, so this is safe to re-run and safe to apply after someone
has tuned the row by hand.
"""
import json
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACNA_CLINIC_ID = "0b5f0929-31fb-4e21-9dd4-030bd040335d"
DECISION_PROTOCOL = "acna_determine_appointment"

ANNUAL_REAL_ETID = 207

# The keys missing from the 0020-seeded row, with the values the engine has
# always intended. None = not bookable by the agent, by definition.
BACKFILL: dict[str, int | None] = {
    "OFFER_SELF_PAY_ANNUAL_HEARING_TEST": ANNUAL_REAL_ETID,
    "REFER_TO_STAFF_PRIOR_AUTHORIZATION": None,
    "REFER_TO_STAFF_PAYER_REVIEW": None,
    "REFER_TO_STAFF_MINOR": None,
}

_MIGRATION_ACTOR = "migration-0025"


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

    outcomes = cfg.get("outcome_booking")
    if not isinstance(outcomes, dict):
        outcomes = {}

    changed = False
    for outcome, etid in BACKFILL.items():
        if outcome not in outcomes:      # absent only — never overwrite intent
            outcomes[outcome] = etid
            changed = True

    if changed:
        cfg["outcome_booking"] = outcomes
        _save_config(conn, cfg)


def downgrade() -> None:
    """Remove the backfilled keys, restoring the pre-0025 key set.

    Drops the keys entirely rather than nulling them: absent is the state
    upgrade() found, and nulling the self-pay mapping would leave the same
    broken booking path behind under a different shape. Only removes a key
    still holding the value this migration wrote, so a later hand edit
    survives a downgrade.
    """
    conn = op.get_bind()
    cfg = _load_config(conn)
    if cfg is None:
        return

    outcomes = cfg.get("outcome_booking")
    if not isinstance(outcomes, dict):
        return

    changed = False
    for outcome, etid in BACKFILL.items():
        if outcome in outcomes and outcomes[outcome] == etid:
            del outcomes[outcome]
            changed = True

    if changed:
        cfg["outcome_booking"] = outcomes
        _save_config(conn, cfg)
