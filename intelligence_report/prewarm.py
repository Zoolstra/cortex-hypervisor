"""
Pre-warm the intelligence report's shared cache.

Renders every active clinic's report at the canonical date ranges
(``report.CANONICAL_RANGES``) into the BigQuery-backed shared cache
(``report._shared_cache_put``), so an interactive load of a standard range is
served from cache instead of paying the ~20s cold render. Intended to run right
after each analytics / PHI ETL load — the data_version baked into the cache key
means the freshly-warmed entries supersede the prior day's automatically.

Idempotent: a render whose (clinic, range, data_version) is already cached
returns from the shared cache and skips recompute, so re-runs are cheap.

    cd cortex-hypervisor
    python -m intelligence_report.prewarm                  # all clinics, all ranges
    python -m intelligence_report.prewarm --clinic <id>    # one clinic
    python -m intelligence_report.prewarm --ranges 90,365  # specific windows
"""
from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import select

from api.core.db import session_scope
from api.core.orm import Clinic, GoogleAdsCampaign, InvocaCampaign
from intelligence_report.report import CANONICAL_RANGES, generate_report_with_campaigns

log = logging.getLogger(__name__)


def _active_clinics_with_campaigns(db) -> list[tuple[str, str, list[str], list[str]]]:
    """(clinic_id, clinic_name, invoca_ids, google_ads_ids) for every non-deleted
    clinic that has at least one active campaign — clinics with no campaigns
    render an almost-empty report not worth warming."""
    out: list[tuple[str, str, list[str], list[str]]] = []
    clinics = list(db.scalars(select(Clinic).where(Clinic.deleted_at.is_(None))))
    for c in clinics:
        inv = [str(x) for x in db.scalars(select(InvocaCampaign.invoca_campaign_id).where(
            InvocaCampaign.clinic_id == c.clinic_id, InvocaCampaign.active.is_(True)))]
        ga = [str(x) for x in db.scalars(select(GoogleAdsCampaign.google_ads_campaign_id).where(
            GoogleAdsCampaign.clinic_id == c.clinic_id, GoogleAdsCampaign.active.is_(True)))]
        if inv or ga:
            out.append((c.clinic_id, c.clinic_name, inv, ga))
    return out


def prewarm(clinic_id: str | None = None, ranges: list[int] | None = None) -> dict:
    ranges = ranges or CANONICAL_RANGES
    with session_scope() as db:
        targets = _active_clinics_with_campaigns(db)
    if clinic_id:
        targets = [t for t in targets if t[0] == clinic_id]

    log.info("Pre-warming %d clinic(s) × %d range(s)", len(targets), len(ranges))
    ok = fail = 0
    for cid, name, inv, ga in targets:
        for days in ranges:
            t0 = time.perf_counter()
            try:
                generate_report_with_campaigns(
                    clinic_id=cid, clinic_name=name,
                    invoca_campaign_ids=inv, google_ads_campaign_ids=ga,
                    days=days, use_cache=True,
                )
                ok += 1
                log.info("  warmed %s (%s) %dd in %.1fs", name, cid, days,
                         time.perf_counter() - t0)
            except Exception:
                fail += 1
                log.exception("  FAILED %s (%s) %dd", name, cid, days)
    log.info("Pre-warm complete: %d ok, %d failed", ok, fail)
    return {"clinics": len(targets), "ranges": len(ranges), "ok": ok, "failed": fail}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Pre-warm the intelligence report cache")
    ap.add_argument("--clinic", default=None, help="Only this clinic_id")
    ap.add_argument("--ranges", default=None,
                    help="Comma-separated day windows (default: canonical)")
    args = ap.parse_args()
    ranges = [int(x) for x in args.ranges.split(",")] if args.ranges else None
    res = prewarm(clinic_id=args.clinic, ranges=ranges)
    print(res)


if __name__ == "__main__":
    main()
