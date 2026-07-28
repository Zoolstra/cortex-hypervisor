"""Resync ACNA's live VAPI assistant to the current factory config.

Tokenless equivalent of POST /clinics/{clinic_id}/voice_agent/assistant — builds
the agent config from Cloud SQL (clinic_protocols, already migrated by 0014) and
pushes it to the existing VAPI assistant. Run this AFTER the prod hypervisor code
is deployed, so the assistant's /placeholder/* tool URLs resolve.

Tool callback URLs are built from CORTEX_API_BASE_URL — this MUST be the prod URL
or the live agent will call the wrong host. The script refuses to run against a
localhost base to prevent pushing dev URLs to a live assistant.

    CORTEX_API_BASE_URL=https://cortex-hypervisor-45007506504.us-central1.run.app \
      PYTHONPATH=. venv/bin/python scripts/resync_acna_assistant.py            # dry-run
    CORTEX_API_BASE_URL=https://cortex-hypervisor-45007506504.us-central1.run.app \
      PYTHONPATH=. venv/bin/python scripts/resync_acna_assistant.py --apply    # push
"""
import os
import sys

from api.core.db import session_scope
from api.core.orm import Clinic, ClinicVoiceAgentConfiguration
from api.voice_agent import vapi as vapi_client
from api.voice_agent.factory import build_agent_config

ACNA = "0b5f0929-31fb-4e21-9dd4-030bd040335d"


def main() -> int:
    apply = "--apply" in sys.argv
    base = os.environ.get("CORTEX_API_BASE_URL", "")
    if "localhost" in base or not base.startswith("https://"):
        print(f"REFUSING: CORTEX_API_BASE_URL must be the prod https URL, got {base!r}")
        return 2

    with session_scope() as db:
        clinic = db.get(Clinic, ACNA)
        va = db.get(ClinicVoiceAgentConfiguration, ACNA)
        if not va or not va.vapi_assistant_id:
            print("ACNA has no provisioned VAPI assistant — nothing to resync.")
            return 1
        config = build_agent_config(db, clinic)

        tools = config.get("model", {}).get("tools") or config.get("tools") or []
        print(f"assistant_id: {va.vapi_assistant_id}")
        print(f"base URL:     {base}")
        print(f"tools ({len(tools)}):")
        for t in tools:
            n = t.get("name")
            url = t.get("url", "")
            print(f"  - {n:<32} {url}")

        placeholder_ok = all(
            "/placeholder/" in t.get("url", "")
            for t in tools
            if t.get("name") in ("list_appointment_types", "find_available_slots", "book_appointment")
        )
        print(f"appointment tools on /placeholder/* endpoints: {placeholder_ok}")

        if not apply:
            print("\nDRY-RUN. Re-run with --apply to push to VAPI.")
            return 0

        vapi_client.update_assistant(va.vapi_assistant_id, config)
        print("\nPUSHED to VAPI. Assistant updated.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
