"""
One-shot backfill: seed voice-agent defaults for every clinic missing them.

Walks every clinic in Cloud SQL and calls ``seed_voice_agent_defaults`` with
``only_if_missing=True`` — safe to re-run, only fills gaps. New clinics get
the same defaults automatically via ``provision_clinic``; this script exists
to backfill clinics that were provisioned before the seed logic landed.

Usage (from cortex-hypervisor/):
    venv/bin/python -m scripts.backfill_voice_agent_defaults                 # all clinics
    venv/bin/python -m scripts.backfill_voice_agent_defaults --clinic <uuid> # one clinic
    venv/bin/python -m scripts.backfill_voice_agent_defaults --dry-run       # report what would change

    # Wipe the literal 4-line opening text from all clinics' opening_overrides
    # so the new stage-based prompt's "style notes" slot stays empty (or
    # clinic-authored only). Idempotent — clinics with empty opening_overrides
    # are skipped.
    venv/bin/python -m scripts.backfill_voice_agent_defaults --clear-opening-overrides
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from api.core.db import _session_factory
from api.core.orm import Clinic, ClinicVoiceAgentScript
from api.voice_agent.defaults import seed_voice_agent_defaults


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clinic", help="UUID of a single clinic to backfill")
    parser.add_argument("--dry-run", action="store_true", help="Roll back instead of committing")
    parser.add_argument(
        "--clear-opening-overrides", action="store_true",
        help=("Wipe opening_overrides on every matching clinic. Use after the "
              "prompt rework that treats opening_overrides as Stage 1 style "
              "notes rather than a literal opening script. Skips clinics whose "
              "field is already empty."),
    )
    args = parser.parse_args()

    Session = _session_factory()
    with Session() as db:
        stmt = select(Clinic)
        if args.clinic:
            stmt = stmt.where(Clinic.clinic_id == args.clinic)
        clinics = list(db.execute(stmt).scalars())

        if not clinics:
            print("No clinics matched.", file=sys.stderr)
            return 1

        # ── Wipe path ──────────────────────────────────────────────────────
        if args.clear_opening_overrides:
            cleared = 0
            for c in clinics:
                row = db.get(ClinicVoiceAgentScript, c.clinic_id)
                if row is None or not (row.opening_overrides or "").strip():
                    continue
                preview = row.opening_overrides.splitlines()[0][:60]
                print(f"  clearing  {c.clinic_id}  {c.clinic_name}  "
                      f"(was: {preview!r}…)")
                row.opening_overrides = None
                cleared += 1
            if args.dry_run:
                db.rollback()
                print(f"\n[dry-run] would clear opening_overrides on {cleared} clinic(s)")
            else:
                db.commit()
                print(f"\nCleared opening_overrides on {cleared} clinic(s).")
            return 0

        # ── Backfill path (default) ────────────────────────────────────────
        totals = {"persona": 0, "script": 0, "buckets": 0}
        for c in clinics:
            summary = seed_voice_agent_defaults(db, c.clinic_id, only_if_missing=True)
            if any([
                summary["persona"] == "created",
                summary["script"] in ("created", "extensions_filled"),
                summary["buckets"] > 0,
            ]):
                print(f"  {c.clinic_id}  {c.clinic_name}  →  {summary}")
            if summary["persona"] == "created":
                totals["persona"] += 1
            if summary["script"] in ("created", "extensions_filled"):
                totals["script"] += 1
            totals["buckets"] += summary["buckets"]

        if args.dry_run:
            db.rollback()
            print(f"\n[dry-run] would touch: persona={totals['persona']} "
                  f"script={totals['script']} buckets={totals['buckets']}")
        else:
            db.commit()
            print(f"\nBackfill committed. persona={totals['persona']} "
                  f"script={totals['script']} buckets={totals['buckets']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
