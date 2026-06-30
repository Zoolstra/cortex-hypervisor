"""
One-off backfill: load a Paperform CSV export of website form submissions into
``ClinicData.webforms`` for a single clinic (built for Calgary Ear Centre).

Live form submissions arrive via ``POST /webforms`` (api/webforms.py); this
script handles the *historical* export that predates that wiring. It mirrors the
same target schema, adding the two columns introduced alongside this backfill
(``customer_type``, ``message``).

What it does
------------
1. Resolves the clinic via Cloud SQL (same source of truth as the report
   endpoint) — by ``--clinic-id`` if given, else by name ``LIKE`` match.
2. Ensures ``customer_type`` / ``message`` columns exist on the live table
   (idempotent ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``).
3. Transforms CSV rows → table rows (strips the ``ph: `` phone prefix, parses
   ``Submitted At`` as UTC, blanks → NULL, stamps clinic_id/clinic_name).
4. Refuses to run if rows already exist for this clinic in the CSV's date range
   (re-run guard) unless ``--force`` is passed.
5. Loads via a load job (WRITE_APPEND) — not a streaming insert — so the rows
   are immediately queryable and never sit in the streaming buffer.

Usage
-----
    cd cortex-hypervisor
    PYTHONPATH=. python scripts/backfill_calgary_webforms.py --dry-run
    PYTHONPATH=. python scripts/backfill_calgary_webforms.py
    PYTHONPATH=. python scripts/backfill_calgary_webforms.py \
        --csv "../Calgary Ear Centre (2).csv" --clinic-name-like calgary
    PYTHONPATH=. python scripts/backfill_calgary_webforms.py --clinic-id <uuid> --force
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import re
import sys

from google.cloud import bigquery
from google.cloud.exceptions import NotFound
from sqlalchemy import select

from api.core.db import session_scope
from api.core.orm import Clinic

log = logging.getLogger("backfill_calgary_webforms")

PROJECT = "project-demo-2-482101"
TABLE = f"{PROJECT}.ClinicData.webforms"

DEFAULT_CSV = "../Calgary Ear Centre (2).csv"
DEFAULT_NAME_LIKE = "calgary"

# CSV header → table column. Columns not listed here are dropped.
COL = {
    "submitted_at":  "Submitted At",
    "first_name":    "First Name?",
    "last_name":     "Last Name?",
    "email":         "Email",
    "phone_number":  "Phone Number",
    "customer_type": "Are You A New Or Returning Customer",
    "message":       "How Can We Help?",
    "utm_source":    "UTM Source",
    "utm_medium":    "UTM Medium",
    "utm_campaign":  "UTM Campaign",
    "utm_term":      "UTM Term",
    "utm_content":   "UTM Content",
}

# Target schema (matches api/webforms.py::_ensure_table, including the two new
# columns). Explicit so the load job types submitted_at as TIMESTAMP.
SCHEMA = [
    bigquery.SchemaField("clinic_id",     "STRING", mode="REQUIRED"),
    bigquery.SchemaField("clinic_name",   "STRING"),
    bigquery.SchemaField("first_name",    "STRING"),
    bigquery.SchemaField("last_name",     "STRING"),
    bigquery.SchemaField("phone_number",  "STRING"),
    bigquery.SchemaField("email",         "STRING"),
    bigquery.SchemaField("utm_source",    "STRING"),
    bigquery.SchemaField("utm_medium",    "STRING"),
    bigquery.SchemaField("utm_campaign",  "STRING"),
    bigquery.SchemaField("utm_term",      "STRING"),
    bigquery.SchemaField("utm_content",   "STRING"),
    bigquery.SchemaField("gclid",         "STRING"),
    bigquery.SchemaField("fbclid",        "STRING"),
    bigquery.SchemaField("landing_page",  "STRING"),
    bigquery.SchemaField("customer_type", "STRING"),
    bigquery.SchemaField("message",       "STRING"),
    bigquery.SchemaField("submitted_at",  "TIMESTAMP", mode="REQUIRED"),
]

_PHONE_PREFIX = re.compile(r"^\s*ph:\s*", re.IGNORECASE)


def _clean(v: str | None) -> str | None:
    """Trim; collapse blanks to None (mirrors the WebformSubmission validator)."""
    if v is None:
        return None
    v = v.strip()
    return v or None


def _clean_phone(v: str | None) -> str | None:
    v = _clean(v)
    if v is None:
        return None
    return _clean(_PHONE_PREFIX.sub("", v))


def _parse_submitted_at(v: str | None) -> str:
    """Paperform 'YYYY-MM-DD HH:MM:SS' → UTC ISO-8601. The export carries no
    timezone; we treat it as UTC, consistent with the live endpoint which stamps
    ``datetime.now(timezone.utc)``. Good enough for day-granularity windows."""
    v = (v or "").strip()
    naive = dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=dt.timezone.utc).isoformat()


def _resolve_clinic(clinic_id: str | None, name_like: str) -> tuple[str, str]:
    with session_scope() as db:
        if clinic_id:
            clinic = db.get(Clinic, clinic_id)
            if clinic is None or clinic.deleted_at is not None:
                sys.exit(f"clinic_id {clinic_id!r} not found / deleted in Cloud SQL")
            return clinic.clinic_id, clinic.clinic_name
        rows = db.execute(
            select(Clinic).where(
                Clinic.clinic_name.ilike(f"%{name_like}%"),
                Clinic.deleted_at.is_(None),
            )
        ).scalars().all()
    if not rows:
        sys.exit(f"no live clinic matching name LIKE %{name_like}%")
    if len(rows) > 1:
        listing = "\n  ".join(f"{c.clinic_id}  {c.clinic_name}" for c in rows)
        sys.exit(f"multiple clinics match %{name_like}% — pass --clinic-id:\n  {listing}")
    return rows[0].clinic_id, rows[0].clinic_name


def _transform(csv_path: str, clinic_id: str, clinic_name: str) -> list[dict]:
    out: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for i, raw in enumerate(csv.DictReader(fh), start=2):  # line 1 = header
            try:
                submitted_at = _parse_submitted_at(raw.get(COL["submitted_at"]))
            except ValueError:
                log.warning("row %d: unparseable 'Submitted At'=%r — skipped",
                            i, raw.get(COL["submitted_at"]))
                continue
            out.append({
                "clinic_id":     clinic_id,
                "clinic_name":   clinic_name,
                "first_name":    _clean(raw.get(COL["first_name"])),
                "last_name":     _clean(raw.get(COL["last_name"])),
                "phone_number":  _clean_phone(raw.get(COL["phone_number"])),
                "email":         _clean(raw.get(COL["email"])),
                "utm_source":    _clean(raw.get(COL["utm_source"])),
                "utm_medium":    _clean(raw.get(COL["utm_medium"])),
                "utm_campaign":  _clean(raw.get(COL["utm_campaign"])),
                "utm_term":      _clean(raw.get(COL["utm_term"])),
                "utm_content":   _clean(raw.get(COL["utm_content"])),
                "gclid":         None,
                "fbclid":        None,
                "landing_page":  None,
                "customer_type": _clean(raw.get(COL["customer_type"])),
                "message":       _clean(raw.get(COL["message"])),
                "submitted_at":  submitted_at,
            })
    return out


def _ensure_table(client: bigquery.Client) -> None:
    """Create ``webforms`` with the full schema if absent (the live endpoint
    creates it lazily on first POST, which may not have happened yet); otherwise
    additively ensure the two new columns exist. Idempotent either way."""
    try:
        client.get_table(TABLE)
    except NotFound:
        client.create_table(bigquery.Table(TABLE, schema=SCHEMA))
        log.info("created table %s", TABLE)
        return
    client.query(
        f"""
        ALTER TABLE `{TABLE}`
          ADD COLUMN IF NOT EXISTS customer_type STRING,
          ADD COLUMN IF NOT EXISTS message       STRING
        """
    ).result()


def _existing_in_range(client: bigquery.Client, clinic_id: str,
                       lo: str, hi: str) -> int:
    rows = list(client.query(
        f"""
        SELECT COUNT(*) AS n FROM `{TABLE}`
        WHERE clinic_id = @clinic_id
          AND submitted_at BETWEEN @lo AND @hi
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ScalarQueryParameter("lo", "TIMESTAMP", lo),
            bigquery.ScalarQueryParameter("hi", "TIMESTAMP", hi),
        ]),
    ).result())
    return int(rows[0].n or 0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=DEFAULT_CSV, help="path to the Paperform CSV export")
    ap.add_argument("--clinic-id", default=None, help="explicit clinic UUID (skips name lookup)")
    ap.add_argument("--clinic-name-like", default=DEFAULT_NAME_LIKE,
                    help="case-insensitive substring to match a clinic by name")
    ap.add_argument("--dry-run", action="store_true",
                    help="transform + report only; no ALTER, no load")
    ap.add_argument("--force", action="store_true",
                    help="load even if rows already exist for this clinic in range")
    ap.add_argument("--replace", action="store_true",
                    help="DELETE this clinic's existing webform rows first, then load "
                         "(per-clinic snapshot; use when re-loading an authoritative export)")
    args = ap.parse_args()

    clinic_id, clinic_name = _resolve_clinic(args.clinic_id, args.clinic_name_like)
    log.info("clinic: %s  (%s)", clinic_name, clinic_id)

    rows = _transform(args.csv, clinic_id, clinic_name)
    if not rows:
        sys.exit("no rows parsed from CSV — nothing to load")
    lo = min(r["submitted_at"] for r in rows)
    hi = max(r["submitted_at"] for r in rows)
    log.info("parsed %d rows  (submitted_at %s … %s)", len(rows), lo, hi)
    log.info("sample row: %s", rows[0])

    if args.dry_run:
        log.info("dry-run — no writes. %d rows would be appended to %s", len(rows), TABLE)
        return

    client = bigquery.Client(project=PROJECT)
    _ensure_table(client)

    if args.replace:
        # Per-clinic snapshot: drop this clinic's rows, then load the export.
        # Only this clinic_id is touched, so other clinics' rows are untouched.
        del_job = client.query(
            f"DELETE FROM `{TABLE}` WHERE clinic_id = @clinic_id",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            ]),
        )
        del_job.result()
        log.info("replace: deleted %s existing row(s) for clinic %s",
                 del_job.num_dml_affected_rows, clinic_id)
    else:
        existing = _existing_in_range(client, clinic_id, lo, hi)
        if existing and not args.force:
            sys.exit(
                f"{existing} row(s) already exist for clinic {clinic_id} in "
                f"[{lo} … {hi}] — looks already backfilled. Re-run with --replace "
                f"(snapshot) or --force (append anyway)."
            )

    job = client.load_table_from_json(
        rows, TABLE,
        job_config=bigquery.LoadJobConfig(
            schema=SCHEMA,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        ),
    )
    job.result()
    log.info("loaded %d rows into %s", len(rows), TABLE)


if __name__ == "__main__":
    main()
