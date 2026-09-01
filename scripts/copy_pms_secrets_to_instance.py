#!/usr/bin/env python3
"""
Copy per-clinic PMS secrets up to the instance scope, and retire the fallback.

Migration 0030 moved PMS config to the account but cannot write Secret Manager,
so a client configured before the cutover still has its credentials under
``clinic_{clinic_id}_blueprint_*``. The ETL and the voice agent read
``instance_{instance_id}_blueprint_*`` first and fall back to a mapped clinic's
own secret, logging a warning each time — which keeps everything working but
leaves the warning firing forever. This closes it.

Copies rather than moves. The clinic-scoped versions are left in place, so
reverting 0030 restores a working configuration; deleting them first would make
that a re-entry job against each client's PMS account. Delete them by hand once
you are confident, or leave them — they cost nothing.

Dry run by default: prints what it would copy, including which clinic each secret
would come from, and writes nothing.

CounselEar needs nothing here. Its secrets are named after the SFTP login
(``{Username}_COUNSELEAR_SFTP_password``, from ``provision_sftp.sh``) rather than
after a clinic id, and that username is itself account-level config now — so the
account row already locates them and no rename happened.

Usage:
    cd cortex-hypervisor
    venv/bin/python scripts/copy_pms_secrets_to_instance.py
    venv/bin/python scripts/copy_pms_secrets_to_instance.py --apply
    venv/bin/python scripts/copy_pms_secrets_to_instance.py --instance-id <uuid>

Prerequisites:
    gcloud auth application-default login
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import select                                    # noqa: E402
from sqlalchemy.orm import Session                               # noqa: E402

from api.core.db import get_engine                               # noqa: E402
from api.core.orm import (                                       # noqa: E402
    Clinic, Instance, InstancePmsConfig, PmsClinicLocation,
)

PROJECT = "project-demo-2-482101"  # mirrors api/core/secrets.py

_KEYS = ("api_key", "aws_access_key_id", "aws_secret_access_key", "zip_password")


def _read(name: str) -> str | None:
    from google.cloud import secretmanager
    sm = secretmanager.SecretManagerServiceClient()
    try:
        resp = sm.access_secret_version(
            request={"name": f"projects/{PROJECT}/secrets/{name}/versions/latest"})
        return resp.payload.data.decode("utf-8")
    except Exception:
        return None


def _write(name: str, value: str) -> None:
    from google.cloud import secretmanager
    sm = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{PROJECT}"
    path = f"{parent}/secrets/{name}"
    try:
        sm.get_secret(request={"name": path})
    except Exception:
        sm.create_secret(request={
            "parent": parent, "secret_id": name,
            "secret": {"replication": {"automatic": {}}}})
    sm.add_secret_version(
        request={"parent": path, "payload": {"data": value.encode("utf-8")}})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance-id", default=None, help="Just this account")
    ap.add_argument("--apply", action="store_true",
                    help="Write. Without this, only prints what it would do.")
    args = ap.parse_args()

    with Session(get_engine()) as db:
        q = select(InstancePmsConfig).where(InstancePmsConfig.pms_type == "blueprint")
        if args.instance_id:
            q = q.where(InstancePmsConfig.instance_id == args.instance_id)
        accounts = list(db.scalars(q))

        if not accounts:
            print("No Blueprint accounts configured.")
            return

        total_copied = total_present = total_missing = 0

        for cfg in accounts:
            instance = db.get(Instance, cfg.instance_id)
            # Every clinic of the account is a candidate source: the credentials
            # were on whichever one happened to be wired first.
            clinic_ids = [
                l.clinic_id for l in db.scalars(
                    select(PmsClinicLocation).where(
                        PmsClinicLocation.instance_id == cfg.instance_id,
                        PmsClinicLocation.pms_type == "blueprint",
                    ).order_by(PmsClinicLocation.vendor_location_key))
                if l.clinic_id
            ]
            print(f"\n{instance.instance_name if instance else cfg.instance_id} "
                  f"({cfg.instance_id})")
            if not clinic_ids:
                print("  no mapped clinics — nothing to copy from")
                continue

            for key in _KEYS:
                dst = f"instance_{cfg.instance_id}_blueprint_{key}"
                if _read(dst):
                    print(f"  {key:<24} already set at account scope")
                    total_present += 1
                    continue
                found_in, value = None, None
                for cid in clinic_ids:
                    value = _read(f"clinic_{cid}_blueprint_{key}")
                    if value:
                        found_in = cid
                        break
                if not value:
                    print(f"  {key:<24} NOT FOUND under any mapped clinic")
                    total_missing += 1
                    continue
                clinic = db.get(Clinic, found_in)
                label = clinic.clinic_name if clinic else found_in
                if args.apply:
                    _write(dst, value)
                    print(f"  {key:<24} copied from {label}")
                else:
                    print(f"  {key:<24} would copy from {label}")
                total_copied += 1

    print(f"\n{'─'*60}")
    verb = "copied" if args.apply else "would copy"
    print(f"  {total_copied} {verb}, {total_present} already at account scope, "
          f"{total_missing} not found")
    if total_missing:
        print("  A missing api_key breaks voice-agent booking; the three AWS/ZIP "
              "keys break the data feed. Enter them under the instance's PMS "
              "settings.")
    if not args.apply:
        print("\n  Re-run with --apply to write.\n")


if __name__ == "__main__":
    main()
