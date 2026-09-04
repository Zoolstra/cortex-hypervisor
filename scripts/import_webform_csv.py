"""One-off importer: Jotform CSV export → ``ClinicData.webforms``.

WHY THIS EXISTS. Virsono's "Earlens Appointment Assessment" form was collecting
submissions for three years without a webhook into our pipeline, so
``ClinicData.webforms`` held **zero** rows for every Virsono clinic while the
form itself had 448. Everything downstream that reads web forms — the Web forms
tab, ``webform_drivers``, and (via cross-channel first touch) Revenue attributed
— therefore reported nothing for that instance. This loads the history.

THIS IS A BACKFILL, NOT THE FIX. The durable fix is registering the form in
Cloud SQL ``jotform_forms`` and provisioning its webhook (see
``configure_jotform.py`` and resources/jotform-webform-setup.md), so new
submissions arrive on their own. Run this once for the history, then wire the
webhook, or the two will overlap on whatever the export covers.

SAFE TO RE-RUN. Every row carries its Jotform submission id in
``raw_fields.jotform_submission_id``; the loader reads back the ids already
present for the target clinics and skips them. That matters because the table is
append-only with no natural key — without the check, a second run silently
doubles every figure it feeds.

Writes via a LOAD JOB rather than streaming inserts, deliberately: streamed rows
sit in the streaming buffer for ~90 minutes during which they cannot be DELETEd,
so a bad load would be unfixable exactly when you most want to undo it. Load-job
rows are DML-deletable immediately.

Dry-run by default. Pass --commit to write.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timezone

from google.cloud import bigquery

log = logging.getLogger("webform_import")

PROJECT = "project-demo-2-482101"
TABLE = f"{PROJECT}.ClinicData.webforms"
CONTACTS = f"{PROJECT}.PMS_Unified.patient_contacts"

# ── Location → clinic ────────────────────────────────────────────────────────
# The form's "Choose your location" is free-form label text, so this map is the
# only thing tying a submission to a clinic. Verified 2026-08-20 against the
# distinct `clinic` labels in CounselEar_PHI.appointments.
#
# ONLY VIRSONO CLINICS THAT EXIST IN OUR SYSTEM ARE HERE. The export is an
# Earlens-network form covering 29 locations across MS/GA/NC/FL/MO/CA/NJ/VA/TX/MD
# — 25 of them are other practices we do not hold data for. Those rows are
# counted and reported, never loaded: a clinic_id is required and there is no
# honest value to invent.
LOCATION_MAP: dict[str, tuple[str, str]] = {
    "Princeton, NJ":  ("9d5fabff-8d06-4f81-bdf9-de5773c7b995", "VHC-NJ-Princeton-PP-103348"),
    "Sarasota, FL":   ("dd78d24e-d4d2-486b-9889-f9e5fb73ab41", "VHC - Sarasota, FL - 104510"),
    "Greenville, NC": ("92b0e3e9-ae1b-4f12-9932-f86cfd361ec0", "VHC-NC-Greenville-EC-106100"),
    "Poway, CA":      ("59fe3145-e8cd-41ff-bef9-41c7353e25ed", "VHC-CA-Poway-RENT-108051"),
}

# Locations matching more than one clinic, resolved per-row by patient match
# (see resolve_ambiguous). "San Diego, CA" is two Virsono clinics that both draw
# San Diego and Chula Vista patients — nothing in the submission distinguishes
# them, so the PMS is asked instead of guessing.
AMBIGUOUS: dict[str, list[tuple[str, str]]] = {
    "San Diego, CA": [
        ("8e54ba46-64ee-4946-a318-83befcdf39e0", "VHC-CA-SanDiego-SENT-107680"),
        ("40967710-f87c-418a-961f-713554096de0", "VHC-CA-SanDiego-CHNS-108529"),
    ],
}

# DELIBERATELY NOT MAPPED. Southlake Hope Hearing & Tinnitus is a Virsono clinic
# in Texas, and "Dallas, TX" is the only plausible CSV label for it — but those
# 16 submissions are dated 2023-08 → 2024-03 while Southlake's CounselEar history
# begins 2026-06-01. Loading them would credit a clinic that was not in our
# system yet with submissions that can never match a patient. Left for a human.
DO_NOT_MAP = {"Dallas, TX": "Southlake Hope — form rows predate PMS history by 2+ years"}

FORM_TITLE = "Earlens Appointment Assessment"


def norm_phone(v: str) -> str:
    """Last 10 digits, the same key every PMS match in this codebase uses."""
    return re.sub(r"\D", "", v or "")[-10:]


def norm_medium(medium: str, source: str) -> str | None:
    """Map Jotform's medium vocabulary onto the one `_form_medium_case_sql` reads.

    Two rewrites, both load-bearing — measured over the 118 mappable rows, they
    are the difference between 38 submissions classified and 38 filed as
    "No data":

    ``none`` → ``direct``  (33 rows) Jotform writes the literal string "none"
        for medium with source "direct" when there was no campaign. Our CASE
        tests ``utm_medium = 'direct'``, so "none" falls through every branch to
        the referrer fallback — and this export carries no referrer_host, so it
        would land in No data.

    ``paid_social`` → ``social``  (5 rows) Not in ``_SOCIAL_MEDIUMS``, and the
        social branch reads ``referrer_host``, never ``utm_source`` — so
        paid_social/facebook would also reach No data. Mapping it to the Social
        bucket matches how the same traffic is already treated when it arrives
        with an ``fbclid``, which the CASE buckets Social rather than Paid.

    Normalising here rather than widening the CASE is the narrower change: the
    CASE is shared with live Jotform traffic and with the Paid definition the
    ads tab depends on.
    """
    m = (medium or "").strip().lower()
    s = (source or "").strip().lower()
    if not m or m == "null":
        return None
    if m == "none":
        return "direct" if s == "direct" else None
    if m == "paid_social":
        return "social"
    return m


def norm_customer_type(v: str) -> str | None:
    """CSV phrasing → the labels already in the column ("New Customer" is 203 of
    299 existing rows). Free text downstream, so matching the majority spelling
    is the whole requirement."""
    v = (v or "").strip().lower()
    if "new" in v:
        return "New Customer"
    if "existing" in v or "returning" in v:
        return "Returning Customer"
    return None


def clean(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


def parse_ts(v: str) -> str | None:
    """Jotform's 'YYYY-MM-DD HH:MM:SS'. Stamped UTC — the export carries no
    offset, so this ASSUMES the account's timezone is UTC. It shifts nothing
    material (window edges only), but it is an assumption, not a fact."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(v.strip(), fmt).replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def resolve_ambiguous(client: bigquery.Client, rows: list[dict],
                      candidates: list[tuple[str, str]]) -> None:
    """Assign each row in `rows` to whichever candidate clinic holds a patient
    matching its phone or email. Rows matching both, or neither, are left
    unresolved — a coin-flip on a real person's submission is worse than a gap
    the summary names.

    Mutates `rows`, setting `clinic_id` / `clinic_name` where resolved.
    """
    if not rows:
        return
    ids = [c[0] for c in candidates]
    label = {c[0]: c[1] for c in candidates}
    sql = f"""
        WITH f AS (
          SELECT ix[OFFSET(o)] AS ix, ph[OFFSET(o)] AS ph, em[OFFSET(o)] AS em
          FROM (SELECT @ix AS ix, @ph AS ph, @em AS em),
               UNNEST(GENERATE_ARRAY(0, ARRAY_LENGTH(@ix) - 1)) AS o
        ),
        p AS (
          SELECT DISTINCT _clinic_id, phone_norm, email_norm
          FROM `{CONTACTS}` WHERE _clinic_id IN UNNEST(@ids)
        )
        SELECT f.ix, ARRAY_AGG(DISTINCT p._clinic_id IGNORE NULLS) AS hits
        FROM f LEFT JOIN p
          ON (LENGTH(f.ph) = 10 AND f.ph = p.phone_norm)
          OR (f.em != '' AND f.em = p.email_norm)
        GROUP BY f.ix
    """
    params = [
        bigquery.ArrayQueryParameter("ix", "STRING", [str(i) for i in range(len(rows))]),
        bigquery.ArrayQueryParameter("ph", "STRING", [r["_ph"] for r in rows]),
        bigquery.ArrayQueryParameter("em", "STRING", [(r["email"] or "").lower() for r in rows]),
        bigquery.ArrayQueryParameter("ids", "STRING", ids),
    ]
    got = {r.ix: list(r.hits) for r in client.query(
        sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()}
    for i, row in enumerate(rows):
        hits = got.get(str(i), [])
        if len(hits) == 1:
            row["clinic_id"] = hits[0]
            row["clinic_name"] = label[hits[0]]


def existing_submission_ids(client: bigquery.Client, table: str,
                            clinic_ids: list[str]) -> set[str]:
    """Submission ids already loaded for these clinics — the re-run guard."""
    if not clinic_ids:
        return set()
    sql = f"""
        SELECT DISTINCT JSON_VALUE(raw_fields, '$.jotform_submission_id') AS sid
        FROM `{table}`
        WHERE clinic_id IN UNNEST(@ids)
          AND JSON_VALUE(raw_fields, '$.jotform_submission_id') IS NOT NULL
    """
    params = [bigquery.ArrayQueryParameter("ids", "STRING", clinic_ids)]
    return {r.sid for r in client.query(
        sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()}


def build_rows(csv_path: str) -> tuple[list[dict], Counter, Counter]:
    """Parse the CSV into webforms-shaped dicts. Returns (rows, skipped, media)."""
    out: list[dict] = []
    skipped: Counter = Counter()
    media: Counter = Counter()
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for rec in csv.DictReader(fh):
            loc = (rec.get("Choose your location") or "").strip()
            if loc in DO_NOT_MAP:
                skipped[f"{loc} (not mapped: {DO_NOT_MAP[loc]})"] += 1
                continue
            mapped = LOCATION_MAP.get(loc)
            if mapped is None and loc not in AMBIGUOUS:
                skipped[f"{loc or '(blank)'} (no clinic in our system)"] += 1
                continue

            ts = parse_ts(rec.get("Submitted At") or "")
            if ts is None:
                skipped["(unparseable submitted_at)"] += 1
                continue

            medium = norm_medium(rec.get("UTM Medium") or "", rec.get("UTM Source") or "")
            media[medium or "(none)"] += 1

            # Preferences the webforms schema has no column for. They are real
            # answers the clinic will want when calling back, so they go in the
            # human-readable blob rather than being dropped — but NOT into
            # structured columns, which would imply a shape the table doesn't have.
            prefs = [
                f"Preferred time: {rec.get('What is your preferred appointment time?')}"
                if clean(rec.get("What is your preferred appointment time?")) else None,
                f"Preferred contact: {rec.get('What is your preferred contact method?')}"
                if clean(rec.get("What is your preferred contact method?")) else None,
            ]

            row = {
                "clinic_id": mapped[0] if mapped else None,
                "clinic_name": mapped[1] if mapped else None,
                "first_name": clean(rec.get("First Name")),
                "last_name": clean(rec.get("Last Name")),
                "phone_number": clean(rec.get("Phone Number")),
                "email": clean(rec.get("Email")),
                "utm_source": clean(rec.get("UTM Source")),
                "utm_medium": medium,
                "utm_campaign": clean(rec.get("UTM Campaign")),
                "utm_term": clean(rec.get("UTM Term")),
                "utm_content": clean(rec.get("UTM Content")),
                "customer_type": norm_customer_type(rec.get("Are you a new customer?") or ""),
                "submitted_at": ts,
                "form_title": FORM_TITLE,
                "pretty": " | ".join(p for p in prefs if p) or None,
                # Date of birth and street address are in the export and are
                # deliberately NOT carried: ClinicData is not one of the
                # BAA-scoped PHI datasets, and neither field contributes to
                # attribution, which keys on phone and email. Device type and IP
                # are dropped for the same reason — no reader wants them.
                "raw_fields": {
                    "jotform_submission_id": clean(rec.get("ID")),
                    "source_location_label": loc,
                    "import": "csv_backfill/earlens_appointment_assessment",
                },
                "_ph": norm_phone(rec.get("Phone Number") or ""),
                "_loc": loc,
            }
            out.append(row)
    return out, skipped, media


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--table", default=TABLE, help="override for testing against a scratch copy")
    ap.add_argument("--commit", action="store_true", help="actually write (default: dry run)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    client = bigquery.Client(project=PROJECT)
    rows, skipped, media = build_rows(args.csv)

    # Ambiguous locations: ask the PMS which clinic each row belongs to.
    for loc, cands in AMBIGUOUS.items():
        group = [r for r in rows if r["_loc"] == loc]
        resolve_ambiguous(client, group, cands)
        for r in group:
            if r["clinic_id"] is None:
                skipped[f"{loc} (ambiguous: no single clinic matched)"] += 1
        log.info("%s: resolved %d/%d by patient match",
                 loc, sum(1 for r in group if r["clinic_id"]), len(group))
    rows = [r for r in rows if r["clinic_id"]]

    # Re-run guard.
    clinic_ids = sorted({r["clinic_id"] for r in rows})
    already = existing_submission_ids(client, args.table, clinic_ids)
    fresh = [r for r in rows
             if (r["raw_fields"]["jotform_submission_id"] or "") not in already]
    dupes = len(rows) - len(fresh)

    log.info("")
    log.info("loadable rows      %d", len(fresh))
    log.info("already loaded     %d (skipped)", dupes)
    log.info("skipped (no map)   %d", sum(skipped.values()))
    for reason, n in skipped.most_common():
        log.info("    %-58s %d", reason[:58], n)
    log.info("by clinic:")
    for cid, n in Counter(r["clinic_name"] for r in fresh).most_common():
        log.info("    %-42s %d", cid, n)
    log.info("utm_medium after normalisation:")
    for m, n in media.most_common():
        log.info("    %-20s %d", m, n)

    if not fresh:
        log.info("\nnothing to load.")
        return 0
    if not args.commit:
        log.info("\nDRY RUN — nothing written. Re-run with --commit to load.")
        return 0

    payload = "\n".join(
        json.dumps({k: v for k, v in r.items() if not k.startswith("_")})
        for r in fresh)
    job = client.load_table_from_file(
        __import__("io").BytesIO(payload.encode()),
        args.table,
        job_config=bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        ),
    )
    job.result()
    log.info("\nloaded %d rows into %s", len(fresh), args.table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
