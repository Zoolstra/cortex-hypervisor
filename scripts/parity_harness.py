"""
Parity harness for the dashboard rework (dashboard-rework-plan.md §1.3).

Compares intelligence payloads between two serving paths and reports numeric
drift. Two modes:

  * ``--mode self`` — old-vs-old self-consistency: calls each v1 endpoint twice
    (cache bypassed) and diffs. This is the Phase 0 exit gate: it proves the
    harness itself is deterministic on settled windows before /v2 exists.
  * ``--mode v2``  — v1 vs /v2: the Phase 2 nightly gate. /v2 endpoints that
    404 (not yet implemented) are reported as SKIPPED, not failures, so the
    harness can run green incrementally while /v2 grows.

Methodology contract: resources/methodology-contract.md. Diff rules implemented
here (exclusion keys, settled windows, today-anchored series trimming, calls
row-set comparison) are the ones specified in dashboard-rework-plan.md §1.3.

Auth: mints a Firebase custom token as uid ``parity-harness`` with the
``super_admin`` role claim (developer claims from custom tokens surface at the
top level of the verified ID token, which is exactly what
``api.deps.verify_token`` + ``require_read_access`` read), then exchanges it
for an ID token via the Identity Toolkit REST API. Requires ADC with access to
Secret Manager secrets ``firebase-admin-service-account`` and
``firebase-web-api-key`` — the same credentials story as every other script in
this repo.

Usage (from cortex-hypervisor/):
    venv/bin/python scripts/parity_harness.py --mode self                # all clinics
    venv/bin/python scripts/parity_harness.py --mode self --clinic <id>  # one clinic
    venv/bin/python scripts/parity_harness.py --mode v2 \
        --base-url https://cortex-hypervisor-45007506504.us-central1.run.app
    venv/bin/python scripts/parity_harness.py --mode self --as-of 2026-08-01 \
        --report /tmp/parity-report.json

Exit codes: 0 = green, 1 = drift found, 2 = harness/endpoint errors.

Clinic discovery reads Cloud SQL directly (api.core.db + api.core.orm) when
reachable; otherwise pass ``--clinic`` / ``--instance`` explicitly.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

# Run from the repo root or scripts/ — make `api.*` importable either way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Diff configuration (dashboard-rework-plan.md §1.3) ───────────────────────

# Keys excluded from every diff: LLM copy (non-deterministic by design) and
# anything stamped at render time.
EXCLUDED_KEYS = {"one_thing", "recommendations", "generated_at", "rendered_at"}

# Today-anchored scalar blocks: build_overview's "mom" and the
# system_performance / operational_health KPI blocks are computed from the full
# calendar month containing the window end CLAMPED TO REAL TODAY
# (payloads.py:109-116, 243-277), so they include unsettled days regardless of
# the pinned window and legitimately shift when a data load lands mid-run.
# Their /v2 parity is validated by the back-to-back freshness-tolerant mode
# instead (plan §1.3.4).
TODAY_ANCHORED_KEYS = {"mom", "system_performance", "operational_health"}

# Settle horizons (methodology-contract.md §13): booking reconciliation matures
# over 3 days; clicks/spend restate for 7. Pinned windows end at as_of − 7d so
# every metric class is settled.
SETTLE_DAYS = 7

# Absolute float tolerance — "revenue to the cent".
FLOAT_TOL = 0.005

# Month-keyed series entries at or after this cutoff are trimmed before
# diffing: monthly_contact_trend (and any today-anchored series) always trails
# from the CURRENT month, so the newest buckets legitimately differ between two
# reads that straddle a data load (plan §1.3.3: current + previous month).
MONTH_KEYS = ("month", "month_start", "bucket_month")

CANONICAL_WINDOWS = (30, 90, 365)  # + the 14-day biweekly window, added below


def _settled_windows(as_of: dt.date) -> list[tuple[str, str, str]]:
    """(label, start, end) inclusive-date windows, pinned absolute, settled."""
    end = as_of - dt.timedelta(days=SETTLE_DAYS)
    out = []
    for days in (*CANONICAL_WINDOWS, 14):
        start = end - dt.timedelta(days=days - 1)
        label = f"{days}d"
        out.append((label, start.isoformat(), end.isoformat()))
    return out


def _month_cutoff(as_of: dt.date) -> str:
    """First day of the PREVIOUS month — series buckets >= this are trimmed."""
    first_this = as_of.replace(day=1)
    prev_last = first_this - dt.timedelta(days=1)
    return prev_last.replace(day=1).isoformat()


# ── Auth ─────────────────────────────────────────────────────────────────────

def mint_id_token() -> str:
    """Custom token (uid=parity-harness, role=super_admin) → ID token."""
    import firebase_admin
    from firebase_admin import auth as fb_auth, credentials

    from api.core.secrets import get_secret

    sa = json.loads(get_secret("firebase-admin-service-account"))
    try:
        app = firebase_admin.get_app("parity-harness")
    except ValueError:
        app = firebase_admin.initialize_app(
            credentials.Certificate(sa), name="parity-harness")
    custom = fb_auth.create_custom_token(
        "parity-harness", {"role": "super_admin"}, app=app).decode()

    web_key = get_secret("firebase-web-api-key").strip()
    r = requests.post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken",
        params={"key": web_key},
        json={"token": custom, "returnSecureToken": True},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["idToken"]


# ── Target discovery ─────────────────────────────────────────────────────────

def discover_targets() -> tuple[list[dict], list[dict]]:
    """(clinics, group_instances) from Cloud SQL. Raises if unreachable."""
    from sqlalchemy import select

    from api.core.db import session_scope
    from api.core.orm import Clinic, Instance

    with session_scope() as db:
        clinics = [
            {"clinic_id": c.clinic_id, "clinic_name": c.clinic_name,
             "instance_id": c.instance_id}
            for c in db.scalars(select(Clinic).where(Clinic.deleted_at.is_(None)))
        ]
        groups = [
            {"instance_id": i.instance_id, "instance_name": i.instance_name}
            for i in db.scalars(
                select(Instance).where(Instance.multi_location_group.is_(True)))
        ]
    return clinics, groups


# ── Fetch ────────────────────────────────────────────────────────────────────

TOKEN_MAX_AGE_S = 50 * 60  # Firebase ID tokens expire after 1h; re-mint early


class Fetcher:
    def __init__(self, base_url: str, v2_prefix: str = "/v2"):
        self.base = base_url.rstrip("/")
        self.v2_prefix = v2_prefix
        self.s = requests.Session()
        self._refresh_token()

    def _refresh_token(self) -> None:
        self.s.headers["Authorization"] = f"Bearer {mint_id_token()}"
        self._token_born = time.monotonic()

    def get(self, path: str, params: dict | None = None,
            v2: bool = False) -> tuple[int, Any]:
        if time.monotonic() - self._token_born > TOKEN_MAX_AGE_S:
            self._refresh_token()
        url = self.base + (self.v2_prefix + path if v2 else path)
        r = self.s.get(url, params=params or {}, timeout=300)
        if r.status_code == 401:  # expired mid-flight — re-mint, retry once
            self._refresh_token()
            r = self.s.get(url, params=params or {}, timeout=300)
        ctype = r.headers.get("content-type", "")
        body = r.json() if "json" in ctype else r.text
        return r.status_code, body


# ── Normalization & diff ─────────────────────────────────────────────────────

def _trim_month_series(node: Any, cutoff: str) -> Any:
    """Drop dict items in lists whose month key >= cutoff (recursive)."""
    if isinstance(node, list):
        out = []
        for item in node:
            if isinstance(item, dict):
                mk = next((k for k in MONTH_KEYS if k in item), None)
                if mk and isinstance(item[mk], str) and item[mk][:7] >= cutoff[:7]:
                    continue
                out.append(_trim_month_series(item, cutoff))
            else:
                out.append(_trim_month_series(item, cutoff))
        return out
    if isinstance(node, dict):
        return {k: _trim_month_series(v, cutoff) for k, v in node.items()}
    return node


def _strip_excluded(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_excluded(v) for k, v in node.items()
                if k not in EXCLUDED_KEYS and k not in TODAY_ANCHORED_KEYS}
    if isinstance(node, list):
        return [_strip_excluded(v) for v in node]
    return node


def normalize(payload: Any, month_cutoff: str) -> Any:
    return _trim_month_series(_strip_excluded(copy.deepcopy(payload)), month_cutoff)


def diff(a: Any, b: Any, path: str = "$") -> list[str]:
    """Recursive structural diff. Ints exact; floats to FLOAT_TOL; lists by
    index (payload lists are ordered by contract)."""
    drifts: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                drifts.append(f"{path}.{k}: missing on left")
            elif k not in b:
                drifts.append(f"{path}.{k}: missing on right")
            else:
                drifts += diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            drifts.append(f"{path}: length {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            drifts += diff(x, y, f"{path}[{i}]")
    elif isinstance(a, bool) or isinstance(b, bool):
        if a is not b:
            drifts.append(f"{path}: {a!r} != {b!r}")
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, int) and isinstance(b, int):
            if a != b:
                drifts.append(f"{path}: {a} != {b}")
        elif abs(float(a) - float(b)) > FLOAT_TOL:
            drifts.append(f"{path}: {a} != {b} (tol {FLOAT_TOL})")
    elif a != b:
        drifts.append(f"{path}: {a!r} != {b!r}")
    return drifts


# ── Calls-table comparison (plan §1.3.5) ─────────────────────────────────────

CALLS_LIMIT = 20000


class Truncated(Exception):
    """Calls response hit the limit — comparison skipped, not drift."""
CALL_ROW_FIELDS = ("call_id", "outcome", "customer_type", "revenue_total")


def diff_calls(a: Any, b: Any) -> list[str]:
    """Order-insensitive row-set diff on identity tuples. Refuses to compare
    truncated responses (no unique sort tiebreaker in v1 → nondeterministic
    truncation edge)."""
    rows_a = a.get("calls", a) if isinstance(a, dict) else a
    rows_b = b.get("calls", b) if isinstance(b, dict) else b
    if not isinstance(rows_a, list) or not isinstance(rows_b, list):
        return ["calls: unexpected payload shape"]
    if len(rows_a) >= CALLS_LIMIT or len(rows_b) >= CALLS_LIMIT:
        raise Truncated(f"at limit ({CALLS_LIMIT}) — truncation edge is "
                        "nondeterministic; narrow the window")

    def key(r: dict) -> tuple:
        return tuple(json.dumps(r.get(f), sort_keys=True, default=str)
                     for f in CALL_ROW_FIELDS)

    set_a, set_b = {key(r) for r in rows_a}, {key(r) for r in rows_b}
    drifts = []
    for t in sorted(set_a - set_b):
        drifts.append(f"calls: row only on left: {t}")
    for t in sorted(set_b - set_a):
        drifts.append(f"calls: row only on right: {t}")
    return drifts


# ── HTML drill-downs (informational — days-only endpoints, plan §1.3.4) ─────

HTML_PAGES = ("spam-calls.html", "no-conversation-calls.html",
              "qualified-no-conv-calls.html", "attributed-invoices.html")
_NUM_RE = re.compile(r">\s*\$?([\d,]+(?:\.\d+)?)\s*<")


def html_numbers(html: str) -> list[str]:
    """All numeric text nodes — a stat fingerprint, not a full parse."""
    return sorted(m.group(1) for m in _NUM_RE.finditer(html))


# ── Runner ───────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    as_of = (dt.date.fromisoformat(args.as_of) if args.as_of
             else dt.date.today())
    windows = _settled_windows(as_of)
    cutoff = _month_cutoff(as_of)

    f = Fetcher(args.base_url, args.v2_prefix)

    if args.clinic or args.instance:
        clinics = [{"clinic_id": c, "clinic_name": c, "instance_id": None}
                   for c in (args.clinic or [])]
        groups = [{"instance_id": i, "instance_name": i}
                  for i in (args.instance or [])]
    else:
        clinics, groups = discover_targets()

    report: dict[str, Any] = {
        "mode": args.mode, "as_of": as_of.isoformat(), "base_url": args.base_url,
        "windows": [{"label": l, "start": s, "end": e} for l, s, e in windows],
        "month_cutoff": cutoff, "results": [], "started_at": time.time(),
    }
    n_drift = n_err = n_skip = 0

    def fetch_pair(path: str, params: dict, is_calls: bool = False):
        """Return (left, right, skip_reason). Right side per mode."""
        st1, left = f.get(path, params)
        if st1 != 200:
            return None, None, f"v1 HTTP {st1}"
        if args.mode == "self":
            st2, right = f.get(path, params)
            if st2 != 200:
                return None, None, f"v1 second read HTTP {st2}"
        else:
            st2, right = f.get(path, params, v2=True)
            if st2 == 404:
                return None, None, "v2 not implemented (404) — SKIPPED"
            if st2 != 200:
                return None, None, f"v2 HTTP {st2}"
        return left, right, None

    def record(target: str, endpoint: str, window: str, drifts: list[str] | None,
               skip: str | None):
        nonlocal n_drift, n_err, n_skip
        entry = {"target": target, "endpoint": endpoint, "window": window}
        if skip:
            entry["skipped"] = skip
            n_skip += 1
            if "HTTP" in skip:
                n_err += 1
        elif drifts:
            entry["drift"] = drifts[:50]
            n_drift += 1
        else:
            entry["ok"] = True
        report["results"].append(entry)
        status = ("SKIP " + skip if skip else
                  f"DRIFT ×{len(drifts)}" if drifts else "ok")
        print(f"  [{target}] {endpoint} {window}: {status}")

    for c in clinics:
        cid = c["clinic_id"]
        print(f"clinic {cid} ({c['clinic_name']})")
        for label, start, end in windows:
            base_params = {"start": start, "end": end, "nocache": 1}

            left, right, skip = fetch_pair(
                f"/intelligence/{cid}/overview",
                {**base_params, "skip_llm": 1})
            record(cid, "overview", label,
                   None if skip else diff(normalize(left, cutoff),
                                          normalize(right, cutoff)), skip)

            if label == "14d":
                left, right, skip = fetch_pair(
                    f"/intelligence/{cid}/biweekly", base_params)
                record(cid, "biweekly", label,
                       None if skip else diff(normalize(left, cutoff),
                                              normalize(right, cutoff)), skip)

            if label in ("30d", "90d"):
                left, right, skip = fetch_pair(
                    f"/intelligence/{cid}/calls",
                    {"start": start, "end": end, "limit": CALLS_LIMIT},
                    is_calls=True)
                if not skip:
                    try:
                        drifts = diff_calls(left, right)
                    except Truncated as t:
                        drifts, skip = None, f"truncated: {t}"
                else:
                    drifts = None
                record(cid, "calls", label, drifts, skip)

        # days-only HTML drill-downs: back-to-back, informational (plan §1.3.4)
        if args.html:
            for page in HTML_PAGES:
                st1, h1 = f.get(f"/intelligence/{cid}/{page}", {"days": 30})
                st2, h2 = (f.get(f"/intelligence/{cid}/{page}", {"days": 30})
                           if args.mode == "self"
                           else f.get(f"/intelligence/{cid}/{page}",
                                      {"days": 30}, v2=True))
                if st1 != 200 or st2 != 200:
                    record(cid, page, "30d-trailing", None,
                           f"HTTP {st1}/{st2}")
                    continue
                nums1, nums2 = html_numbers(h1), html_numbers(h2)
                drifts = ([] if nums1 == nums2 else
                          [f"stat fingerprint differs: {len(nums1)} vs "
                           f"{len(nums2)} numbers, first mismatch at index "
                           f"{next((i for i, (x, y) in enumerate(zip(nums1, nums2)) if x != y), -1)}"])
                record(cid, page + " (informational)", "30d-trailing",
                       drifts, None)

    for g in groups:
        iid = g["instance_id"]
        print(f"group {iid} ({g['instance_name']})")
        for label, start, end in windows:
            if label == "14d":
                continue
            left, right, skip = fetch_pair(
                f"/intelligence/group/{iid}/overview",
                {"start": start, "end": end, "nocache": 1, "skip_llm": 1})
            record(iid, "group-overview", label,
                   None if skip else diff(normalize(left, cutoff),
                                          normalize(right, cutoff)), skip)

    report["finished_at"] = time.time()
    report["summary"] = {
        "targets": len(clinics) + len(groups),
        "checks": len(report["results"]),
        "drift": n_drift, "errors": n_err, "skipped": n_skip,
    }
    out = Path(args.report)
    out.write_text(json.dumps(report, indent=1, default=str))
    print(f"\n{report['summary']} → {out}")
    if n_err:
        return 2
    return 1 if n_drift else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--mode", choices=("self", "v2"), required=True)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--v2-prefix", default="/v2")
    ap.add_argument("--clinic", action="append",
                    help="clinic id (repeatable); default: all from Cloud SQL")
    ap.add_argument("--instance", action="append",
                    help="group instance id (repeatable)")
    ap.add_argument("--as-of", help="YYYY-MM-DD anchor (default: today)")
    ap.add_argument("--html", action="store_true",
                    help="also fingerprint the days-only HTML drill-downs")
    ap.add_argument("--report", default="parity-report.json")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
