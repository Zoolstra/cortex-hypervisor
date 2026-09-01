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

The same rule applies to the WINDOW: send exactly the query string the SPA sends,
including sending none where the SPA sends none. Recomputing a window here that
the endpoint derives from its own `days=` default warms a key nobody requests —
which is how the biweekly warm silently did nothing for its whole life (the SPA
sends no dates; `Window.from_days(14)` is today-14..today, not today-13..today).

WHAT IS WARMABLE: only the endpoints that actually cache. Per SPA page:

    Dashboard     /overview            + /pipeline-revenue   both cached
    Calls         /overview            + /calls              /calls is NOT cached
    Ad campaigns  /overview                                  cached
    Web forms     /overview                                  cached
    Leads         /active-leads                              cached
    Reactivation  /clinics/{id}/worklists/*                  not cached, other router
    Biweekly      /biweekly                                  cached

`/calls` (both the Calls table and the Dashboard's "today's callbacks" card) is
uncached, so requesting it here would do work and keep nothing. The four
`*.html` drill-downs are uncached too. Everything cached is warmed below.

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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.parity_harness import mint_id_token  # noqa: E402 — reuse the auth dance

log = logging.getLogger("prewarm")

PROD = "https://cortex-hypervisor-45007506504.us-central1.run.app"

# Must mirror the ranges the SPA actually requests, or we warm keys nobody asks
# for. `defaultRange()` in cortex-spa/src/lib/date-utils.ts is MIN_DATE -> today
# (the widest and most expensive window — and the one every landing page hits),
# plus the 7/30/90/365 presets.
MIN_DATE = "2025-12-04"
PRESET_DAYS = (7, 30, 90, 365)

# Cached, window-scoped endpoints. LANDING is what the Dashboard route itself
# fires — BOTH of them, so neither is optional and a visitor's first page is only
# warm when both are. BEHIND_A_CLICK is reached from the nav.
LANDING = ("overview", "pipeline-revenue")
BEHIND_A_CLICK = ("active-leads",)
RANGED = LANDING + BEHIND_A_CLICK

# A group request fans every per-clinic reader across the instance, so it costs
# roughly N clinic pages apiece. See --concurrency below for why this is not
# serial any more.
DEFAULT_CONCURRENCY = 4

# Firebase ID tokens live 60 min. A fully-cold run (every clinic's data_version
# rotated in the same hour, which is what the 06:00 run after `blueprint-sync`
# looks like) can outlast that, and the tail would 401 — reported as a warming
# failure with a misleading cause. Re-mint well inside the window instead.
_TOKEN_MAX_AGE = 2_400.0


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
    # Warming the same key twice is pure waste, and the duplicate would silently
    # reappear as history grows.
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


def plan(clinics: list[dict], groups: list[dict],
         wins: list[tuple[str, str, str]]) -> list[tuple[str, dict, str]]:
    """The ordered work list: (path, params, human label).

    Ordered in tiers so that a run cut short by the task timeout has warmed the
    pages people actually land on. Tier 1 is every scope's Dashboard at the
    default window — the two requests every visitor's first page fires. Only
    then do the narrower presets and the tabs behind a click get warmed.
    """
    default_win, preset_wins = wins[0], wins[1:]
    scopes = (
        [(f"/intelligence/{c['clinic_id']}", (c.get("clinic_name") or c["clinic_id"])[:24])
         for c in clinics]
        + [(f"/intelligence/group/{g['instance_id']}",
            (g.get("instance_name") or g["instance_id"])[:24] + " group")
           for g in groups]
    )
    work: list[tuple[str, dict, str]] = []

    def add(base: str, name: str, ep: str, label: str, s: str, e: str) -> None:
        work.append((f"{base}/{ep}", {"start": s, "end": e},
                     f"{name} {ep} {label}"))

    dlabel, ds, de = default_win
    # Tier 1 — the landing Dashboard, every scope.
    for base, name in scopes:
        for ep in LANDING:
            add(base, name, ep, dlabel, ds, de)
    # Tier 2 — the tabs behind a click, still at the default window.
    for base, name in scopes:
        for ep in BEHIND_A_CLICK:
            add(base, name, ep, dlabel, ds, de)
    # Tier 3 — the presets, widest first.
    for label, s, e in preset_wins:
        for base, name in scopes:
            for ep in RANGED:
                add(base, name, ep, label, s, e)
    # Tier 4 — biweekly. Clinic-only (the group route is a "not on rollup" stub),
    # and deliberately NO start/end: the SPA sends none, so the window is the
    # endpoint's own `days=14` default. Sending our own dates warms a different
    # key than the one the page asks for.
    for c in clinics:
        cid = c["clinic_id"]
        work.append((f"/intelligence/{cid}/biweekly", {},
                     f"{(c.get('clinic_name') or cid)[:24]} biweekly (endpoint default)"))
    return work


class Warmer:
    """Issues the requests. Thread-safe: one `requests.Session` per worker
    thread, one shared auto-renewing token."""

    def __init__(self, base: str, timeout: int):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.ok = 0
        self.failed = 0
        self._counts = threading.Lock()
        self._local = threading.local()
        self._token_lock = threading.Lock()
        self._token = ""
        self._minted = 0.0

    def _auth(self) -> str:
        with self._token_lock:
            if not self._token or (time.monotonic() - self._minted) > _TOKEN_MAX_AGE:
                self._token = mint_id_token()
                self._minted = time.monotonic()
            return f"Bearer {self._token}"

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            self._local.session = s
        return s

    def warm(self, path: str, params: dict, what: str) -> None:
        t = time.perf_counter()
        try:
            # No skip_llm and no nocache: both bypass the cache, so passing
            # either would make this job do nothing useful.
            r = self._session().get(f"{self.base}{path}", params=params,
                                    headers={"Authorization": self._auth()},
                                    timeout=self.timeout)
            ms = round((time.perf_counter() - t) * 1000)
            if r.status_code == 200:
                with self._counts:
                    self.ok += 1
                # A fast response means it was already warm for this data_version.
                log.info("  ok   %-52s %6d ms%s", what, ms,
                         "  (already warm)" if ms < 2000 else "")
            elif r.status_code == 404:
                # Expected: a group endpoint 404s when multi_location_group is off.
                log.info("  skip %-52s 404", what)
            else:
                with self._counts:
                    self.failed += 1
                log.warning("  FAIL %-52s %s %s", what, r.status_code, r.text[:90])
        except Exception as exc:  # noqa: BLE001 — one target must not sink the run
            with self._counts:
                self.failed += 1
            log.warning("  FAIL %-52s %s", what, exc)


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
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help="parallel in-flight requests. Serial (1) does not fit "
                         "the task timeout on a fully-cold run: every clinic's "
                         "data_version rotates together when blueprint-sync "
                         "lands, and the cold cost then sums past an hour. The "
                         "requests are independent reads and the shared cache "
                         "makes any completed warm durable, so overlapping them "
                         "is safe; keep it modest because each cold Overview "
                         "also spends two Claude calls.")
    args = ap.parse_args()

    today = dt.date.today()
    wins = windows(today)
    clinics, groups = targets(args.clinic, args.instance)
    work = plan(clinics, groups, wins)

    log.info("prewarm: %d clinic(s), %d group instance(s), %d window(s) -> "
             "%d requests, %d at a time",
             len(clinics), len(groups), len(wins), len(work), args.concurrency)

    if args.dry_run:
        for label, s, e in wins:
            log.info("  window %-14s %s .. %s", label, s, e)
        for path, params, what in work:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            log.info("  %-56s %s%s", what, path, f"?{qs}" if qs else "")
        log.info("dry run: nothing requested")
        return

    warmer = Warmer(args.base_url, args.timeout)
    t_all = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        for path, params, what in work:
            pool.submit(warmer.warm, path, params, what)

    log.info("prewarm complete: %d ok, %d failed, %.1f min total",
             warmer.ok, warmer.failed, (time.perf_counter() - t_all) / 60)
    sys.exit(1 if warmer.failed else 0)


if __name__ == "__main__":
    main()
