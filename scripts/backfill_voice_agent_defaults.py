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
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from api.core.db import _session_factory
from api.core.orm import Clinic
from api.voice_agent.defaults import seed_voice_agent_defaults


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clinic", help="UUID of a single clinic to backfill")
    parser.add_argument("--dry-run", action="store_true", help="Roll back instead of committing")
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
