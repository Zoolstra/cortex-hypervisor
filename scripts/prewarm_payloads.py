"""
Pre-warm the intelligence JSON payload cache after an ETL load.

WHY: a cold Overview costs ~40-60s (14 BigQuery readers, each paying a ~2-3s
per-query floor); the same payload warm costs ~0.4s. So the user-facing problem
was never query speed, it was cache misses. This job makes sure the first real
visitor after each data load never pays cold.

It only works because the payload cache now has a SHARED (GCS-backed) layer:
Cloud Run runs `--workers 1` per instance and scales to zero, so warming an
in-process dict from an external job would have warmed nothing durable. See the
shared-cache section in api/intelligence.py.

DESIGN NOTE — this calls the HTTP endpoints rather than invoking the payload
builders in-process. That is deliberate: the cache key is built inside the
endpoint (window + tier + data_version), so calling the endpoint makes key drift
structurally impossible. Reimplementing the key here would be faster and would
eventually warm the wrong keys silently.

Idempotent and cheap to re-run: because keys are data-versioned, a second run
within the same data version is served from cache in milliseconds. Re-running
after a load is what actually does work.

Run:
    venv/bin/python scripts/prewarm_payloads.py --dry-run
    venv/bin/python scripts/prewarm_payloads.py --base-url http://localhost:8000
    venv/bin/python scripts/prewarm_payloads.py          # prod (default)

Exit codes: 0 = all warmed, 1 = some failed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.parity_harness import mint_id_token  # noqa: E402 — reuse the auth dance

log = logging.getLogger("prewarm")

PROD = "https://cortex-hypervisor-45007506504.us-central1.run.app"

# Must mirror the ranges the SPA actually requests, or we warm keys nobody asks
# for. `defaultRange()` in cortex-spa/src/lib/date-utils.ts is MIN_DATE -> today
# (the widest and most expensive window — and the one every landing page hits),
# plus the 7/30/90/365 presets. Biweekly uses a trailing fortnight.
MIN_DATE = "2025-12-04"
PRESET_DAYS = (7, 30, 90, 365)
BIWEEKLY_DAYS = 14


def windows(today: dt.date) -> list[tuple[str, str, str]]:
    """(label, start, end) inclusive ranges to warm, widest first.

    Widest first on purpose: it is the default view and the slowest, so it should
    be warm earliest if the job is cut short.
    """
    out = [("default(full)", MIN_DATE, today.isoformat())]
    for d in PRESET_DAYS:
        start = (today - dt.timedelta(days=d - 1)).isoformat()
        out.append((f"{d}d", max(start, MIN_DATE), today.isoformat()))
    # Dedupe identical ranges: a preset wider than the available history clamps to
    # MIN_DATE and becomes the default window, so 365d currently duplicates it.
    # Warming the same key twice is pure waste (13 requests at today's clinic
    # count), and the duplicate would silently reappear as history grows.
    seen: set[tuple[str, str]] = set()
    unique = []
    for label, s, e in out:
        if (s, e) in seen:
            continue
        seen.add((s, e))
        unique.append((label, s, e))
    return unique


def targets(clinic: str | None, instance: str | None):
    """(clinics, group_instances) from Cloud SQL, or the explicit overrides."""
    if clinic or instance:
        return ([{"clinic_id": clinic}] if clinic else [],
                [{"instance_id": instance}] if instance else [])
    from sqlalchemy import select

    from api.core.db import session_scope
    from api.core.orm import Clinic, Instance

    with session_scope() as db:
        clinics = [{"clinic_id": c.clinic_id, "clinic_name": c.clinic_name}
                   for c in db.scalars(select(Clinic).where(Clinic.deleted_at.is_(None)))]
        groups = [{"instance_id": i.instance_id, "instance_name": i.instance_name}
                  for i in db.scalars(
                      select(Instance).where(Instance.multi_location_group.is_(True)))]
    return clinics, groups


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Warm the intelligence payload cache")
    ap.add_argument("--base-url", default=PROD)
    ap.add_argument("--clinic", help="single clinic id (default: all active)")
    ap.add_argument("--instance", help="single group instance id")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be warmed; make no requests")
    ap.add_argument("--timeout", type=int, default=300,
                    help="per-request seconds; a cold Overview can take ~60s")
    args = ap.parse_args()

    today = dt.date.today()
    wins = windows(today)
    clinics, groups = targets(args.clinic, args.instance)

    plan = len(clinics) * (len(wins) + 1) + len(groups) * len(wins)
    log.info("prewarm: %d clinic(s), %d group instance(s), %d window(s) -> %d requests",
             len(clinics), len(groups), len(wins), plan)

    if args.dry_run:
        for label, s, e in wins:
            log.info("  window %-14s %s .. %s", label, s, e)
        log.info("dry run: nothing requested")
        return

    base = args.base_url.rstrip("/")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {mint_id_token()}"

    ok = failed = 0
    t_all = time.perf_counter()

    def warm(path: str, params: dict, what: str) -> None:
        nonlocal ok, failed
        t = time.perf_counter()
        try:
            # No skip_llm and no nocache: both bypass the cache, so passing
            # either would make this job do nothing useful.
            r = session.get(f"{base}{path}", params=params, timeout=args.timeout)
            ms = round((time.perf_counter() - t) * 1000)
            if r.status_code == 200:
                ok += 1
                # A fast response means it was already warm for this data_version.
                log.info("  ok   %-46s %6d ms%s", what, ms,
                         "  (already warm)" if ms < 2000 else "")
            elif r.status_code == 404:
                # Expected: group endpoint 404s when multi_location_group is off.
                log.info("  skip %-46s 404", what)
            else:
                failed += 1
                log.warning("  FAIL %-46s %s %s", what, r.status_code, r.text[:90])
        except Exception as exc:  # noqa: BLE001 — one target must not sink the run
            failed += 1
            log.warning("  FAIL %-46s %s", what, exc)

    for c in clinics:
        cid = c["clinic_id"]
        name = (c.get("clinic_name") or cid)[:24]
        for label, s, e in wins:
            warm(f"/intelligence/{cid}/overview", {"start": s, "end": e},
                 f"{name} overview {label}")
        bw_start = (today - dt.timedelta(days=BIWEEKLY_DAYS - 1)).isoformat()
        warm(f"/intelligence/{cid}/biweekly",
             {"start": max(bw_start, MIN_DATE), "end": today.isoformat()},
             f"{name} biweekly 14d")

    for g in groups:
        iid = g["instance_id"]
        name = (g.get("instance_name") or iid)[:24]
        for label, s, e in wins:
            warm(f"/intelligence/group/{iid}/overview", {"start": s, "end": e},
                 f"{name} group {label}")

    log.info("prewarm complete: %d ok, %d failed, %.1f min total",
             ok, failed, (time.perf_counter() - t_all) / 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
