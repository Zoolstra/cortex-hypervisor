"""ACNA technician clean-and-check: placeholder pair 8→204 + outcome mapping

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-20

Makes the decision engine's ``BOOK_TECHNICIAN_CLEAN_AND_CHECK`` outcome
bookable. Until now only the Annual pair (200→207) was configured, so every
other outcome fell through to "take a message" — including the clean-and-check,
which the decision table reaches for any existing patient who is neither due
for an annual nor overdue for a clinician visit.

Why 8→204, and why nothing else:

  The pairing was derived from ACNA's own booked history (333k appointment rows
  in ``Blueprint_PHI.Appointments``, joined to the live API grid on event_id to
  recover the eventTypeIds that ``clinicConfiguration`` reports as name=null),
  not from the type list:

    * ``Z Maintenance/RA`` (etid 8, 30m) is a placeholder grid — 0 of 2,262
      rows are patient-linked — carries only the two technicians, and titles
      its lanes "C&C RA 1"/"C&C RA 2". It is the clean-and-check grid.
    * ``Service`` (etid 204, 30m) is the real type: 473 of 475 rows are
      patient-linked, and the technician is its single largest provider.
    * Duration matches at 30m, and 40 booked Service appointments sit exactly
      beside a ``Z Maintenance/RA`` placeholder for the same technician.

  A CLINICIAN service visit gets no pair, deliberately. Of 1,786 booked Service
  appointments only 11% sit beside any placeholder, and those are
  duration-mismatched (a 30m Service dropped into a 60m/90m clinician space) —
  staff book clinician service straight onto a calendar, with no grid to search.
  Since ``_resolve_pair`` keys on real_event_type_id, 204 can map to exactly one
  grid; pointing it at the technician grid and leaving
  ``BOOK_CLINICIAN_SERVICE_VISIT`` unmapped keeps a clinician visit from being
  silently booked with a technician.

  New-patient intake stays unmapped too — no placeholder pairing is evidenced
  for it.

Both configs are patched by MERGE, not overwrite: the live rows were seeded by
migration 0020 and predate the engine's self-pay / warranty-bundling /
minor-threshold keys, so a blind rewrite would either drop keys or resurrect
stale ones. Idempotent — re-running adds nothing.
"""
import json
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACNA_CLINIC_ID = "0b5f0929-31fb-4e21-9dd4-030bd040335d"

SEARCH_PROTOCOL = "acna_search_availability"
DECISION_PROTOCOL = "acna_determine_appointment"

OUTCOME = "BOOK_TECHNICIAN_CLEAN_AND_CHECK"

PLACEHOLDER_ETID = 8      # Z Maintenance/RA  (30m, technician C&C lanes)
REAL_ETID = 204           # Service           (30m, patient-linked)
DISPLAY_NAME = "Hearing Aid Clean and Check"

_MIGRATION_ACTOR = "migration-0024"


def _load_config(conn, protocol_id: str) -> dict | None:
    """The protocol's stored config as a dict, or None if the row is absent.

    A NULL/``"null"``/empty config means "protocol enabled, defaults apply" —
    that is a dict-shaped starting point, not a missing row, so it comes back
    as ``{}`` and gets patched normally.
    """
    row = conn.execute(
        text(
            "SELECT config FROM clinic_protocols "
            "WHERE clinic_id = :c AND protocol_id = :p"
        ),
        {"c": ACNA_CLINIC_ID, "p": protocol_id},
    ).fetchone()
    if row is None:
        return None
    raw = row[0]
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _save_config(conn, protocol_id: str, config: dict) -> None:
    conn.execute(
        text(
            "UPDATE clinic_protocols SET config = :cfg, updated_by = :by "
            "WHERE clinic_id = :c AND protocol_id = :p"
        ),
        {
            "cfg": json.dumps(config),
            "by": _MIGRATION_ACTOR,
            "c": ACNA_CLINIC_ID,
            "p": protocol_id,
        },
    )


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Append the clean-and-check TypePair to the search protocol ────────
    cfg = _load_config(conn, SEARCH_PROTOCOL)
    if cfg is not None:
        pairs = cfg.get("type_pairs")
        if not isinstance(pairs, list):
            pairs = []
        # Keyed on real_event_type_id — that's what _resolve_pair looks up, so
        # a second entry for 204 would be ambiguous rather than additive.
        if not any(
            isinstance(p, dict) and p.get("real_event_type_id") == REAL_ETID
            for p in pairs
        ):
            pairs.append({
                "placeholder_event_type_id": PLACEHOLDER_ETID,
                "real_event_type_id": REAL_ETID,
                "display_name": DISPLAY_NAME,
            })
            cfg["type_pairs"] = pairs
            _save_config(conn, SEARCH_PROTOCOL, cfg)

    # ── 2. Point the technician outcome at the real type ────────────────────
    cfg = _load_config(conn, DECISION_PROTOCOL)
    if cfg is not None:
        outcomes = cfg.get("outcome_booking")
        if not isinstance(outcomes, dict):
            outcomes = {}
        if outcomes.get(OUTCOME) != REAL_ETID:
            outcomes[OUTCOME] = REAL_ETID
            cfg["outcome_booking"] = outcomes
            _save_config(conn, DECISION_PROTOCOL, cfg)


def downgrade() -> None:
    """Revert to Annual-only booking: drop the pair, unmap the outcome.

    Removes only what upgrade() added, leaving every other config key — and any
    pair or outcome added since — untouched.
    """
    conn = op.get_bind()

    cfg = _load_config(conn, SEARCH_PROTOCOL)
    if cfg is not None:
        pairs = cfg.get("type_pairs")
        if isinstance(pairs, list):
            kept = [
                p for p in pairs
                if not (
                    isinstance(p, dict)
                    and p.get("real_event_type_id") == REAL_ETID
                    and p.get("placeholder_event_type_id") == PLACEHOLDER_ETID
                )
            ]
            if len(kept) != len(pairs):
                cfg["type_pairs"] = kept
                _save_config(conn, SEARCH_PROTOCOL, cfg)

    cfg = _load_config(conn, DECISION_PROTOCOL)
    if cfg is not None:
        outcomes = cfg.get("outcome_booking")
        if isinstance(outcomes, dict) and outcomes.get(OUTCOME) is not None:
            outcomes[OUTCOME] = None
            cfg["outcome_booking"] = outcomes
            _save_config(conn, DECISION_PROTOCOL, cfg)
