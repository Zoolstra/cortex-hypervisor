"""
One-off loader: Google Ads geotargets CSV → ClinicData.geo_targets.

`ad_clicks_v2.click_view_area_of_interest_region` stores values like
`geoTargetConstants/20113` — the numeric suffix is Google Ads' criterion ID.
This table lets queries resolve them to human-readable names (e.g. "Calgary,
Alberta, Canada").

Usage:
    cd cortex-hypervisor
    venv/bin/python -m intelligence_report.load_geo_targets <ZIP_URL>

Where <ZIP_URL> is the latest dated ZIP link from
    https://developers.google.com/google-ads/api/reference/data/geotargets
(e.g. https://developers.google.com/static/google-ads/api/data/geo/geotargets-2026-05-05.csv.zip).

Also accepts a raw CSV URL or local path, in case Google switches formats.

WRITE_TRUNCATE — safe to re-run. Target table: ClinicData.geo_targets.
"""
from __future__ import annotations

import csv
import io
import sys
import urllib.request
import zipfile

from google.cloud import bigquery

PROJECT = "project-demo-2-482101"
DATASET = "ClinicData"
TABLE = "geo_targets"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    url = sys.argv[1]

    print(f"Fetching {url}…")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    print(f"  fetched {len(raw):,} bytes")

    if url.endswith(".zip") or raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                print(f"No CSV inside ZIP. Members: {zf.namelist()}", file=sys.stderr)
                return 1
            with zf.open(csv_names[0]) as f:
                text = f.read().decode("utf-8", errors="replace")
        print(f"  extracted {csv_names[0]} ({len(text):,} chars)")
    else:
        text = raw.decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for r in reader:
        try:
            criterion_id = int(r["Criteria ID"])
        except (KeyError, ValueError):
            continue
        rows.append({
            "criterion_id": criterion_id,
            "name": (r.get("Name") or "").strip(),
            "canonical_name": (r.get("Canonical Name") or "").strip(),
            "parent_id": int(r["Parent ID"]) if r.get("Parent ID", "").strip().isdigit() else None,
            "country_code": (r.get("Country Code") or "").strip(),
            "target_type": (r.get("Target Type") or "").strip(),
            "status": (r.get("Status") or "").strip(),
        })

    if not rows:
        print("No rows parsed — is the CSV header what we expect "
              "(Criteria ID, Name, Canonical Name, Parent ID, Country Code, Target Type, Status)?",
              file=sys.stderr)
        return 1
    print(f"  parsed {len(rows):,} rows")

    client = bigquery.Client(project=PROJECT)
    table_ref = f"{PROJECT}.{DATASET}.{TABLE}"
    job_config = bigquery.LoadJobConfig(
        schema=[
            bigquery.SchemaField("criterion_id",   "INT64", mode="REQUIRED"),
            bigquery.SchemaField("name",           "STRING"),
            bigquery.SchemaField("canonical_name", "STRING"),
            bigquery.SchemaField("parent_id",      "INT64"),
            bigquery.SchemaField("country_code",   "STRING"),
            bigquery.SchemaField("target_type",    "STRING"),
            bigquery.SchemaField("status",         "STRING"),
        ],
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    job = client.load_table_from_json(rows, table_ref, job_config=job_config)
    job.result()
    print(f"  loaded → {table_ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
