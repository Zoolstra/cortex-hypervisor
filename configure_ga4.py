#!/usr/bin/env python3
"""
GA4 property registry maintenance: drift audit between Cloud SQL and the catalog.

Web-traffic data is INGESTED by the ETL job ``ga4-ingest``
(``cortex-data-ingestion/app/ga4/``), which pulls the GA4 Data API for every
property in the Cloud SQL ``google_analytics_properties`` registry. Registering a
property (admin UI → clinic → Campaigns → type ``google_analytics``, or
``POST /campaigns/{clinic_id}``) IS the wiring. The same job also WRITE_TRUNCATEs
``ClinicData.ga4_properties_catalog`` from the GA Admin API — every account and
property the impersonated agency identity (``zoolytics@zoolstra.com``) can see.

This script compares the two:

  --discover   registered properties the catalog no longer lists (access
               revoked / property deleted — the poller will fail on them every
               run until deactivated), and catalog properties that are NOT
               registered (visible to us, collecting data, attributed to nobody).
               Dry-run. Cannot guess the clinic, so ``--apply`` does nothing for
               the unregistered side.
  --apply      with --discover: deactivate registered properties absent from
               the catalog.

Credentials
-----------
- Cloud SQL + BigQuery: standard ADC (same as running the hypervisor locally).

Usage
-----
    cd cortex-hypervisor && source venv/bin/activate
    python configure_ga4.py --discover
    python configure_ga4.py --discover --apply
"""
import argparse
import sys

from google.cloud import bigquery
from sqlalchemy import select

from api.core.db import session_scope
from api.core.orm import Clinic, GoogleAnalyticsProperty, Instance

PROJECT = "project-demo-2-482101"
CATALOG = f"{PROJECT}.ClinicData.ga4_properties_catalog"


def _load_registry() -> list[dict]:
    """Active-first snapshot of google_analytics_properties joined to clinic names."""
    with session_scope() as db:
        rows = db.execute(
            select(GoogleAnalyticsProperty, Clinic.clinic_name, Clinic.instance_id,
                   Instance.instance_name, Instance.ga4_account_id)
            .join(Clinic, Clinic.clinic_id == GoogleAnalyticsProperty.clinic_id)
            .join(Instance, Instance.instance_id == Clinic.instance_id)
            .where(Clinic.deleted_at.is_(None))
            .order_by(GoogleAnalyticsProperty.active.desc(), Instance.instance_name,
                      Clinic.clinic_name)
        ).all()
        return [
            {
                "property_id":   p.ga4_property_id,
                "property_name": p.property_name,
                "clinic_id":     p.clinic_id,
                "clinic_name":   clinic_name,
                "instance_id":   instance_id,
                "instance_name": instance_name,
                "account_id":    account_id,
                "active":        bool(p.active),
            }
            for p, clinic_name, instance_id, instance_name, account_id in rows
        ]


def _load_catalog() -> dict[str, dict]:
    """property_id → catalog row. Empty dict (with a note) if the table is absent."""
    client = bigquery.Client(project=PROJECT)
    sql = f"""
        SELECT CAST(account_id AS STRING) AS account_id, account_name,
               CAST(property_id AS STRING) AS property_id, property_name,
               time_zone, currency_code, primary_hostname, synced_at
        FROM `{CATALOG}`
    """
    try:
        return {r["property_id"]: dict(r) for r in client.query(sql).result()}
    except Exception as exc:  # table not built yet — say so rather than "0 properties"
        print(f"!! could not read {CATALOG}: {exc}\n"
              f"   Has ga4-ingest run at least once? Treating the catalog as unknown.",
              file=sys.stderr)
        return {}


def discover(apply: bool) -> None:
    registry = {r["property_id"]: r for r in _load_registry()}
    catalog = _load_catalog()
    if not catalog:
        print("Catalog unavailable — nothing to compare against, and nothing will be "
              "deactivated on the strength of an empty catalog.")
        return
    synced = max((str(r.get("synced_at") or "") for r in catalog.values()), default="?")
    print(f"catalog: {len(catalog)} propert(ies) across "
          f"{len({r['account_id'] for r in catalog.values()})} account(s), synced {synced}; "
          f"registry: {len(registry)} row(s)\n")

    # Registered account ids → which catalog accounts we consider "ours".
    our_accounts = {r["account_id"] for r in registry.values() if r["account_id"]}

    unregistered = [r for pid, r in catalog.items() if pid not in registry]
    if unregistered:
        print("In the catalog but NOT registered (register in the admin UI if it is a "
              "CORTEX clinic's property; properties under an account already linked to an "
              "instance are marked *):")
        for r in sorted(unregistered, key=lambda r: ((r["account_name"] or "").lower(),
                                                     (r["property_name"] or "").lower())):
            flag = "*" if r["account_id"] in our_accounts else " "
            print(f" {flag} {r['property_id']:>10}  {r['property_name']!r:45}  "
                  f"acct {r['account_id']} {r['account_name']!r}  {r.get('primary_hostname') or ''}")
    else:
        print("Every catalog property is registered.")

    gone = [r for pid, r in registry.items() if pid not in catalog]
    if gone:
        verb = "Deactivating" if apply else "WOULD deactivate (use --apply)"
        print(f"\nRegistered but no longer in the catalog — {verb}:")
        with session_scope() as db:
            for r in gone:
                print(f"  {r['property_id']}  {r['instance_name']} / {r['clinic_name']}  "
                      f"{r['property_name']!r}{'' if r['active'] else '  (already inactive)'}")
                if apply and r["active"]:
                    row = db.execute(select(GoogleAnalyticsProperty).where(
                        GoogleAnalyticsProperty.ga4_property_id == r["property_id"]
                    )).scalar_one()
                    row.active = False
    else:
        print("\nEvery registered property is still visible in the catalog.")

    # Instances whose ga4_account_id disagrees with where their property lives.
    mismatched = [
        (r, catalog[pid]) for pid, r in registry.items()
        if pid in catalog and r["account_id"] and catalog[pid]["account_id"] != r["account_id"]
    ]
    if mismatched:
        print("\nInstance ga4_account_id does not match the property's account (the "
              "campaign picker will not list this property for its own instance):")
        for r, c in mismatched:
            print(f"  {r['instance_name']}: instances.ga4_account_id={r['account_id']} but "
                  f"property {r['property_id']} is under account {c['account_id']}")

    # Registered properties whose catalog name is newer than what we stored.
    renamed = [
        (r, catalog[pid]) for pid, r in registry.items()
        if pid in catalog and catalog[pid].get("property_name")
        and catalog[pid]["property_name"] != r["property_name"]
    ]
    if renamed:
        verb = "Updating" if apply else "WOULD update (use --apply)"
        print(f"\nproperty_name differs from the catalog — {verb}:")
        with session_scope() as db:
            for r, c in renamed:
                print(f"  {r['property_id']}  {r['property_name']!r} → {c['property_name']!r}")
                if apply:
                    row = db.execute(select(GoogleAnalyticsProperty).where(
                        GoogleAnalyticsProperty.ga4_property_id == r["property_id"]
                    )).scalar_one()
                    row.property_name = c["property_name"]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="GA4 property registry maintenance (drift audit vs the ETL catalog).")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--discover", action="store_true",
                      help="audit registry ↔ catalog drift (unregistered catalog properties; "
                           "registered properties that are gone — --apply deactivates those "
                           "and refreshes property_name)")
    args = ap.parse_args()

    if args.discover:
        discover(args.apply)


if __name__ == "__main__":
    main()
