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
import json
import httpx
from sqlalchemy import select

from api.core.db import session_scope
from api.core.orm import Clinic, ClinicBlueprintConfig
from api.core.secrets import get_secret
from api.voice_agent.pms.blueprint import _blueprint_base

EVENT_TYPE_ID = 3  # overridden by --event-type-id


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


def availability_search(base: str, key: str) -> None:
    url = f"{base}/availability/search/"
    params = {"apiKey": key, "startTime": 1780898400, "endTime": 1782194400}
    try:
        r = httpx.post(
            url,
            json=params,
            timeout=20,
        )
    except Exception as e:
        print(f"  FAIL — request error: {type(e).__name__}: {e}")
        return
    
    status = r.status_code
    size = len(r.content)
    if status == 200:

        print(f"  RESULT  : 200 OK ({size} bytes)")
        params.pop("apiKey")
        print(f" params: {json.dumps(params, indent=10)}")
        print(f" payload: {json.dumps(r.json(), indent=10)}")





def availability_find(base: str, key: str, event_type_id: int) -> None:
    """Raw GET /rest/availability/ — the exact call find_available_slots makes."""
    url = f"{base}/availability/"
    params = {
        "apiKey": key,
        "startTime": 1780898400,
        "endTime": 1782194400,
        "eventTypeId": event_type_id,
        "bookingTimeSlotInterval": "DURATION",
        "minimumAdvanceBookingTime": 30,
    }
    print(f"\n  --- GET /availability/ (eventTypeId={event_type_id}) ---")
    try:
        r = httpx.get(url, params=params, timeout=20)
    except Exception as e:
        print(f"  FAIL — request error: {type(e).__name__}: {e}")
        return
    if r.status_code != 200:
        print(f"  RESULT  : {r.status_code} ({len(r.content)} bytes) {r.text[:300]}")
        return
    data = r.json()
    avail_days = [d for d in data if isinstance(d, dict) and d.get("available")]
    print(f"  RESULT  : 200 OK ({len(r.content)} bytes) — "
          f"{len(data)} days, {len(avail_days)} with available=true")
    print(f"  payload: {json.dumps(data, indent=10)}")


def list_types(base: str, key: str) -> None:
    """Dump appointment types so we can see what eventTypeId=3 is."""
    print("\n  --- GET /clinicConfiguration/ (appointment types) ---")
    try:
        r = httpx.get(f"{base}/clinicConfiguration/", params={"apiKey": key}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"  FAIL — {type(e).__name__}: {e}")
        return
    types = [
        {"id": t["id"], "name": t.get("name"), "duration": t.get("duration")}
        for t in r.json().get("appointmentTypes", [])
        if t.get("name")
    ]
    print(f"  payload: {json.dumps(types, indent=10)}")


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

    # availability_search(base, key)
    list_types(base, key)
    availability_find(base, key, EVENT_TYPE_ID)

   



def main() -> int:
    p = argparse.ArgumentParser(description="Verify a clinic's Blueprint REST API key")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--clinic-id", help="exact clinic_id (UUID)")
    g.add_argument("--clinic-name", help="substring match on clinic_name")
    g.add_argument("--all", action="store_true", help="check every blueprint clinic")
    p.add_argument("--event-type-id", type=int, default=3,
                   help="event type to probe in GET /availability/ (default 3)")
    args = p.parse_args()

    global EVENT_TYPE_ID
    EVENT_TYPE_ID = args.event_type_id

    clinics = _resolve_clinics(args.clinic_id, args.clinic_name, args.all)
    if not clinics:
        print("No matching clinic(s) found.")
        return 1

    _check(clinics[0][0], clinics[0][1], clinics[0][2], clinics[0][3])


if __name__ == "__main__":
    sys.exit(main())
