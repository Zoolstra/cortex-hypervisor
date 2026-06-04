#!/usr/bin/env python3
"""
Verify a clinic's Blueprint OMS REST API key actually authenticates.

Reads the clinic's non-secret config (api_url, clinic_code) from Cloud SQL
``clinic_blueprint_config`` and the API key from Secret Manager
(``clinic_{clinic_id}_blueprint_api_key``), then makes a live
``GET /rest/clinicConfiguration/`` call and reports a clear pass/fail.

No Firebase auth needed — this talks to Cloud SQL + Secret Manager directly
via ADC, so it authenticates as whatever identity ADC resolves to (locally,
the SA you impersonated with ``./dev.sh auth``; in prod, the runtime SA).

Usage:
    cd cortex-hypervisor && source venv/bin/activate

    python verify_blueprint_key.py --clinic-id 5e256c93-1369-4f6b-9106-456ab08a1b55
    python verify_blueprint_key.py --clinic-name "Calgary Ear Center"
    python verify_blueprint_key.py --all          # check every blueprint clinic

How to read the result:
    200  PASS  — key is valid; Blueprint returned the clinic config.
    403  FAIL  — key rejected. Either the key is wrong/revoked OR REST API
                 access for this clinic was never enabled on Blueprint's side.
                 Both look identical from here (empty-body 403).
    404  FAIL  — the api_url path (instance / clinic slug) is wrong; the
                 clinic instance isn't even found on the server.
    400  FAIL  — request shape rejected (e.g. the apiKey param didn't arrive).
"""
from __future__ import annotations

import argparse
import sys

import httpx
from sqlalchemy import select

from api.core.db import session_scope
from api.core.orm import Clinic, ClinicBlueprintConfig
from api.core.secrets import get_secret
from api.voice_agent.pms.blueprint import _blueprint_base


def _inspect_key(key: str) -> list[str]:
    """Return human-readable notes about any suspicious bytes in the key."""
    notes: list[str] = []
    if key != key.strip():
        notes.append("has leading/trailing whitespace")
    bad = sorted({c for c in key if c not in "0123456789abcdefABCDEF-"})
    if bad:
        notes.append(
            "contains non-UUID chars: "
            + ", ".join(f"{c!r}(U+{ord(c):04X})" for c in bad)
        )
    if len(key.strip()) != 36:
        notes.append(f"length is {len(key.strip())}, expected 36 for a UUID")
    return notes


def _resolve_clinics(clinic_id: str | None, clinic_name: str | None, do_all: bool):
    """Yield (clinic_id, clinic_name, api_url) tuples for the requested target(s)."""
    with session_scope() as db:
        stmt = select(Clinic).where(Clinic.pms_type == "blueprint")
        if clinic_id:
            stmt = select(Clinic).where(Clinic.clinic_id == clinic_id)
        elif clinic_name:
            stmt = select(Clinic).where(Clinic.clinic_name.like(f"%{clinic_name}%"))
        elif not do_all:
            raise SystemExit("Pass one of --clinic-id, --clinic-name, or --all")

        out = []
        for c in db.execute(stmt).scalars().all():
            bp = db.get(ClinicBlueprintConfig, c.clinic_id)
            out.append((c.clinic_id, c.clinic_name, c.pms_type, bp.api_url if bp else None))
        return out


def _check(clinic_id: str, clinic_name: str, pms_type: str, api_url: str | None) -> bool:
    print("=" * 72)
    print(f"  {clinic_name}  ({clinic_id})")
    print("=" * 72)

    if pms_type != "blueprint":
        print(f"  SKIP — pms_type is {pms_type!r}, not 'blueprint'")
        return False
    if not api_url:
        print("  FAIL — no api_url in clinic_blueprint_config")
        return False

    try:
        key = get_secret(f"clinic_{clinic_id}_blueprint_api_key")
    except Exception as e:
        print(f"  FAIL — API key not in Secret Manager ({type(e).__name__})")
        return False

    base = _blueprint_base({"api_url": api_url})
    notes = _inspect_key(key)
    print(f"  api_url : {api_url}")
    print(f"  base    : {base}")
    print(f"  key     : len={len(key)} clean={'no — ' + '; '.join(notes) if notes else 'yes'}")

    url = f"{base}/clinicConfiguration/"
    try:
        r = httpx.get(url, params={"apiKey": key}, timeout=20)
    except Exception as e:
        print(f"  FAIL — request error: {type(e).__name__}: {e}")
        return False

    status = r.status_code
    size = len(r.content)
    if status == 200:
        print(f"  RESULT  : 200 OK ({size} bytes) — PASS, key is valid ✓")
        return True

    reason = {
        403: "key rejected (wrong/revoked) OR REST API access not enabled for this clinic",
        404: "path not found — api_url instance/slug is wrong",
        400: "bad request — apiKey param missing/malformed",
    }.get(status, "unexpected status")
    print(f"  RESULT  : {status} ({size} bytes) — FAIL: {reason} ✗")
    return False


def main() -> int:
    p = argparse.ArgumentParser(description="Verify a clinic's Blueprint REST API key")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--clinic-id", help="exact clinic_id (UUID)")
    g.add_argument("--clinic-name", help="substring match on clinic_name")
    g.add_argument("--all", action="store_true", help="check every blueprint clinic")
    args = p.parse_args()

    clinics = _resolve_clinics(args.clinic_id, args.clinic_name, args.all)
    if not clinics:
        print("No matching clinic(s) found.")
        return 1

    results = [_check(*c) for c in clinics]
    passed = sum(results)
    print("=" * 72)
    print(f"  {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
