"""
One-off loader: Google Ads geotargets CSV → ClinicData.geo_targets.

`ad_clicks_v2.click_view_area_of_interest_region` stores values like
`geoTargetConstants/20113` — the numeric suffix is Google Ads' criterion ID.
This table lets queries resolve them to human-readable names (e.g. "Calgary,
Alberta, Canada") and, for city-level targets, to a map coordinate.

Coordinates come from GeoNames (cities500: every populated place with 500+
people, CC BY 4.0). Google's CSV carries none, and the paid-call map on the Ads
tab needs a point per city. A target is matched on (name, admin-1 name, country)
— "Calgary" + "Alberta" + CA — against GeoNames' name, ASCII name and alternate
names. There is deliberately NO country-only fallback: it resolved "Vernon,
Wisconsin" to Vernon, Texas, and a wrong point is worse than a missing one.
Unmatched targets (hamlets under 500 people, postal codes, regions) keep NULL
coordinates and the UI reports them as unplaced.

Usage:
    cd cortex-hypervisor
    venv/bin/python -m intelligence_report.load_geo_targets <ZIP_URL> [--no-coords]
        [--geonames <cities500 zip URL or path>] [--admin1 <admin1CodesASCII URL or path>]
        [--ndjson <path>]

``--ndjson`` writes the rows to a newline-delimited JSON file and touches
BigQuery not at all, for when the Python client has no working ADC but the
``bq`` CLI does. Load the file with ``bq load --replace
--source_format=NEWLINE_DELIMITED_JSON ClinicData.geo_targets <path> <schema>``
where <schema> lists the nine columns of SCHEMA below as ``name:TYPE`` pairs.

Where <ZIP_URL> is the latest dated ZIP link from
    https://developers.google.com/google-ads/api/reference/data/geotargets
(e.g. https://developers.google.com/static/google-ads/api/data/geo/geotargets-2026-05-05.csv.zip).

Also accepts a raw CSV URL or local path, in case Google switches formats.

WRITE_TRUNCATE — safe to re-run. Target table: ClinicData.geo_targets.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import unicodedata
import urllib.request
import zipfile

from google.cloud import bigquery

PROJECT = "project-demo-2-482101"
DATASET = "ClinicData"
TABLE = "geo_targets"

GEONAMES_CITIES_URL = "https://download.geonames.org/export/dump/cities500.zip"
GEONAMES_ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"

SCHEMA = [
    bigquery.SchemaField("criterion_id",   "INT64", mode="REQUIRED"),
    bigquery.SchemaField("name",           "STRING"),
    bigquery.SchemaField("canonical_name", "STRING"),
    bigquery.SchemaField("parent_id",      "INT64"),
    bigquery.SchemaField("country_code",   "STRING"),
    bigquery.SchemaField("target_type",    "STRING"),
    bigquery.SchemaField("status",         "STRING"),
    # GeoNames-sourced; NULL for anything not placed (see the module note).
    bigquery.SchemaField("latitude",       "FLOAT64"),
    bigquery.SchemaField("longitude",      "FLOAT64"),
]

# Target types that name a populated place a GeoNames city row can stand for.
# Anything else (postal codes, counties, provinces, airports) would either not
# match or match the wrong thing — a "County" called Hamilton is not the city.
COORD_TARGET_TYPES = {"City", "Municipality", "Borough", "Post town"}


def _fetch(url: str) -> bytes:
    """Read a URL or a local path."""
    if "://" not in url:
        with open(url, "rb") as f:
            return f.read()
    print(f"Fetching {url}…")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    print(f"  fetched {len(raw):,} bytes")
    return raw


def _unzip_text(raw: bytes, suffix: str) -> str:
    """The first member ending in ``suffix`` from a ZIP, or the bytes as text."""
    if raw[:2] != b"PK":
        return raw.decode("utf-8", errors="replace")
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(suffix)]
        if not names:
            raise SystemExit(f"No {suffix} inside ZIP. Members: {zf.namelist()}")
        with zf.open(names[0]) as f:
            text = f.read().decode("utf-8", errors="replace")
    print(f"  extracted {names[0]} ({len(text):,} chars)")
    return text


def _norm(s: str) -> str:
    """Fold accents and case, strip generic words, so "Ville de Québec" and
    "Quebec City" both reduce to "quebec"."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(city|town|village|municipality|county|of|the|de|du|la|le)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def build_coord_index(cities_text: str, admin1_text: str) -> dict[tuple[str, str, str], tuple[float, float]]:
    """``{(norm name, norm admin-1 name, country code): (lat, lon)}`` from the
    GeoNames dump. Where several places share a key the most populous wins,
    and a hit via an alternate name ranks just below the same place's own
    name so a real "Vernon" beats a town merely also known as Vernon."""
    admin1: dict[str, str] = {}
    for line in admin1_text.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 3:
            admin1[parts[0]] = _norm(parts[2])
    best: dict[tuple[str, str, str], tuple[int, float, float]] = {}

    def put(key, pop, lat, lon):
        cur = best.get(key)
        if cur is None or pop > cur[0]:
            best[key] = (pop, lat, lon)

    for line in cities_text.splitlines():
        f = line.split("\t")
        if len(f) < 15:
            continue
        try:
            lat, lon, pop = float(f[4]), float(f[5]), int(f[14] or 0)
        except ValueError:
            continue
        cc, a1 = f[8], admin1.get(f"{f[8]}.{f[10]}", "")
        if not a1:
            continue
        for n in {_norm(f[1]), _norm(f[2])}:
            if n:
                put((n, a1, cc), pop, lat, lon)
        for alt in (f[3].split(",") if f[3] else []):
            n = _norm(alt)
            if n:
                put((n, a1, cc), pop - 1, lat, lon)
    return {k: (v[1], v[2]) for k, v in best.items()}


def attach_coords(rows: list[dict], index: dict) -> int:
    """Set ``latitude``/``longitude`` on every row the index can place. The
    admin-1 name is the second-to-last part of Google's canonical name
    ("Hamilton,Hamilton,Ontario,Canada" → "Ontario"). Returns the hit count."""
    hits = 0
    for r in rows:
        r["latitude"] = None
        r["longitude"] = None
        if r["target_type"] not in COORD_TARGET_TYPES:
            continue
        parts = [p.strip() for p in r["canonical_name"].split(",")]
        if len(parts) < 3:
            continue
        hit = index.get((_norm(r["name"]), _norm(parts[-2]), r["country_code"]))
        if hit:
            r["latitude"], r["longitude"] = hit
            hits += 1
    return hits


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0].startswith("--"):
        print(__doc__, file=sys.stderr)
        return 2
    url = args[0]
    with_coords = "--no-coords" not in args

    def opt(flag: str, default: str) -> str:
        return args[args.index(flag) + 1] if flag in args else default

    text = _unzip_text(_fetch(url), ".csv")

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

    if with_coords:
        index = build_coord_index(
            _unzip_text(_fetch(opt("--geonames", GEONAMES_CITIES_URL)), ".txt"),
            _fetch(opt("--admin1", GEONAMES_ADMIN1_URL)).decode("utf-8", errors="replace"),
        )
        eligible = sum(1 for r in rows if r["target_type"] in COORD_TARGET_TYPES)
        hits = attach_coords(rows, index)
        print(f"  coordinates: {hits:,} of {eligible:,} city-level targets placed")
    else:
        for r in rows:
            r["latitude"] = r["longitude"] = None

    if "--ndjson" in args:
        path = opt("--ndjson", "")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"  wrote {len(rows):,} rows → {path} (BigQuery untouched)")
        return 0

    client = bigquery.Client(project=PROJECT)
    table_ref = f"{PROJECT}.{DATASET}.{TABLE}"
    job_config = bigquery.LoadJobConfig(
        schema=SCHEMA,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    job = client.load_table_from_json(rows, table_ref, job_config=job_config)
    job.result()
    print(f"  loaded → {table_ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
