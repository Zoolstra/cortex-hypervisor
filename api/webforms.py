"""
Web-form ingestion — relays form submissions from our own clinic-site backends
into ``ClinicData.webforms`` for the patient-acquisition funnel.

``POST /webforms`` (JSON)
    A form on one of our Next.js sites is submitted; the site's backend POSTs the
    captured fields here (server-to-server, so the shared secret never reaches
    the browser). Auth is the ``X-Webform-Secret`` header.

**Jotform-hosted forms do NOT come through here any more.** They are polled from
the Jotform API by the ETL job ``jotform-ingest``
(``cortex-data-ingestion/app/jotform/``), which owns the table's schema; the
``WEBFORMS_SCHEMA`` below is a pinned mirror. The former webhook relay
``POST /webforms/jotform/{clinic_id}`` was retired 2026-09-04 after the webhooks
were removed from every form (``resources/jotform-api-polling-plan.md``).

``GET /webforms/coverage`` (super_admin) joins the ``jotform_forms`` registry
against what has landed, so a registered-but-silent form is visible.

Auth
----
``webform-webhook-secret`` (Secret Manager) guards ``POST /webforms``.
``clinic_id`` is verified against Cloud SQL before any write — an unknown or
soft-deleted clinic is rejected with 404, so junk never lands in BigQuery.

Storage
-------
Rows go to ``ClinicData.webforms`` via a streaming insert — real-time,
append-only, ``ingest_source = json_relay``. The table is created lazily on
first write and reconciled additively (NULLABLE columns only). Streaming inserts
take the ``raw_fields`` JSON column as an encoded string; the ETL's load jobs
take the object — the two writers differ on purpose.
"""
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException
from google.cloud import bigquery
from google.cloud.exceptions import NotFound
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.db import get_session
from api.core.orm import Clinic, JotformForm
from api.core.secrets import get_secret
from api.deps import bq_client, verify_token
from api.models import WebformSubmission

log = logging.getLogger(__name__)

router = APIRouter()

WEBFORMS_TABLE = "project-demo-2-482101.ClinicData.webforms"

# Created lazily on first write; flipped to True once create_table has run so we
# don't issue a (harmless but wasteful) metadata call on every submission.
_table_ready = False


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_webform_secret(x_webform_secret: str = Header(None)) -> None:
    # Strip both sides: the SM secret may carry a trailing newline (an artifact of
    # how it was created), which a header/URL value never will.
    expected = (get_secret("webform-webhook-secret") or "").strip()
    if not expected or (x_webform_secret or "").strip() != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing webform secret")


# ── Table ───────────────────────────────────────────────────────────────────--

# Schema MIRROR. The source of record is the ETL's ``jotform/schema.py``
# (the poller is the main writer now); both are pinned to the same fixture,
# ``tests/fixtures/webforms_schema.json``, by tests in each repo.
WEBFORMS_SCHEMA = [
    bigquery.SchemaField("clinic_id",    "STRING", mode="REQUIRED"),
    bigquery.SchemaField("clinic_name",  "STRING"),
    bigquery.SchemaField("first_name",   "STRING"),
    bigquery.SchemaField("last_name",    "STRING"),
    bigquery.SchemaField("phone_number", "STRING"),
    bigquery.SchemaField("email",        "STRING"),
    bigquery.SchemaField("utm_source",   "STRING"),
    bigquery.SchemaField("utm_medium",   "STRING"),
    bigquery.SchemaField("utm_campaign", "STRING"),
    bigquery.SchemaField("utm_term",     "STRING"),
    bigquery.SchemaField("utm_content",  "STRING"),
    bigquery.SchemaField("gclid",        "STRING"),
    bigquery.SchemaField("fbclid",       "STRING"),
    # Paid-click identifiers beyond gclid. Google sends `gbraid` (app/web
    # cross-device) or `wbraid` (iOS, post-ATT) INSTEAD of a gclid on a large
    # and growing share of clicks, so a gclid-only capture silently loses
    # them: 7 of the first 8 attributable submissions carried a gbraid.
    # `gad_campaignid` is the strongest key of the three — it IS the campaign
    # id, so it needs no ad_clicks_v2 join and is therefore immune to that
    # table's 7-day click settle window.
    bigquery.SchemaField("gbraid",         "STRING"),
    bigquery.SchemaField("wbraid",         "STRING"),
    bigquery.SchemaField("gad_campaignid", "STRING"),
    # The referring site's host, when the visit carried no UTM parameters.
    # Split out of utm_source by _utm() — see that docstring for why the two
    # were conflated and how they are told apart.
    bigquery.SchemaField("referrer_host",  "STRING"),
    bigquery.SchemaField("landing_page", "STRING"),
    bigquery.SchemaField("customer_type", "STRING"),
    bigquery.SchemaField("message",       "STRING"),
    bigquery.SchemaField("submitted_at", "TIMESTAMP", mode="REQUIRED"),
    # Form provenance + lossless capture. Forms differ in layout (a "New/
    # Returning" radio on one, a service-type radio on another, extra fields
    # like "preferred contact method" on a third), so the typed columns above
    # are a best-effort *core*; ``raw_fields`` keeps EVERY field verbatim so
    # nothing is ever dropped and novel forms need no code change. ``pretty``
    # is Jotform's human-readable "Label: Value" rendering (labels aren't in
    # rawRequest). Promote a raw field to its own column later by backfilling
    # from the JSON.
    bigquery.SchemaField("form_id",     "STRING"),
    bigquery.SchemaField("form_title",  "STRING"),
    bigquery.SchemaField("raw_fields",  "JSON"),
    bigquery.SchemaField("pretty",      "STRING"),
    # Added 2026-09 with the webhook → API-polling move. ``submission_id`` is
    # Jotform's id — the poller's idempotency key, so the webhook and the
    # poller can overlap without double-counting; ``ingest_source`` says which
    # writer landed the row; ``ingested_at`` is when WE wrote it (what
    # submitted_at used to mean before the poller made it Jotform's created_at).
    bigquery.SchemaField("submission_id", "STRING"),
    bigquery.SchemaField("ingest_source", "STRING"),
    bigquery.SchemaField("ingested_at",   "TIMESTAMP"),
]

SOURCE_JSON_RELAY = "json_relay"   # the ETL writes jotform_api / backfill


def _ensure_table() -> None:
    """Create ClinicData.webforms if it doesn't exist. Idempotent; runs once."""
    global _table_ready
    if _table_ready:
        return
    schema = WEBFORMS_SCHEMA
    # Check existence first so we don't issue a create — and log a benign
    # "Already Exists" audit error — on every cold start.
    try:
        existing = bq_client.get_table(WEBFORMS_TABLE)
    except NotFound:
        bq_client.create_table(bigquery.Table(WEBFORMS_TABLE, schema=schema))
        _table_ready = True
        return
    # Additively reconcile: a table created before a column was added keeps its
    # old schema forever, and the insert would then fail on the unknown field.
    # ONLY appends NULLABLE columns — never removes, reorders or retypes, so it
    # cannot destroy data and needs no migration step on deploy.
    have = {f.name for f in existing.schema}
    missing = [f for f in schema if f.name not in have]
    if missing:
        existing.schema = list(existing.schema) + missing
        bq_client.update_table(existing, ["schema"])
        log.info("webforms: added columns %s", ", ".join(f.name for f in missing))
    _table_ready = True


# ── Attribution extraction ────────────────────────────────────────────────────

# Ad-click identifiers we lift off the landing URL. `gad_campaignid` is not a
# click id but rides in the same query string and is the most directly useful of
# the set.
_ATTRIBUTION_PARAMS = ("gclid", "gbraid", "wbraid", "fbclid", "gad_campaignid")


def _query_params(url: str | None) -> dict[str, str]:
    """Query params off a stored ``landing_page``.

    Values are relative paths (``/brand-official?gad_source=1&…``) — urlsplit
    parses the query regardless of a missing scheme/host, so no normalisation is
    needed. ``keep_blank_values=False`` so a bare ``?gclid=`` yields nothing
    rather than an empty-string id that would read as "present".
    """
    if not url or "?" not in url:
        return {}
    try:
        qs = urlsplit(url).query
    except ValueError:      # malformed URL (e.g. bad IPv6 literal)
        return {}
    return {
        k: v[0].strip()
        for k, v in parse_qs(qs, keep_blank_values=False).items()
        if v and v[0].strip()
    }


_UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")

# A bare hostname (`google.com`, `ca.search.yahoo.com`, and Android's
# `com.google.android.googlequicksearchbox`) or the literal `direct`.
_REFERRER_RE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$", re.I)


def _utm(fields: dict) -> dict[str, str | None]:
    """Real UTM parameters, with the site's referrer fallback split back out.

    **The bug this fixes.** Every site feeding these endpoints — Jotform-hosted
    and JSON-relay alike — populates a hidden field named ``utm_source``. When
    the visit carries no UTM parameters the site writes ``document.referrer``'s
    HOST into it instead of leaving it empty. So the column reads `google.com`,
    `bing.com`, `chatgpt.com`, `direct` — referrers, not campaign sources — and
    `utm_medium` is never populated at all. Anything grouping by ``utm_source``
    (``webform_funnel``) was therefore reporting referrers as campaign sources.

    **How they are told apart: provenance, not shape.** A UTM parameter present
    in the landing URL's query string is real, full stop. A value that arrives
    only in the form field, with no ``utm_source=`` in the URL, is the site's
    referrer fallback. Shape alone cannot decide this — `?utm_source=chatgpt.com`
    is a genuine campaign tag whose value happens to be a hostname, and it
    appears in this data — so the URL is checked first and wins.

    The referrer is preserved in its own ``referrer_host`` key rather than
    discarded: knowing a lead arrived from organic Google is useful, it simply
    is not a campaign source.
    """
    params = _query_params(fields.get("landing_page"))
    # An explicitly-sent referrer_host is authoritative: the sites were fixed to
    # send it in its own field rather than smuggled inside utm_source. Seed from
    # it so the salvage path below only ever fills the gap left by an OLD site
    # build — otherwise the fixed sites' value would be silently dropped here.
    explicit_referrer = fields.get("referrer_host")
    explicit_referrer = (explicit_referrer.strip()
                         if isinstance(explicit_referrer, str) else explicit_referrer)
    out: dict[str, str | None] = {"referrer_host": explicit_referrer or None}
    for key in _UTM_KEYS:
        from_url = params.get(key)
        if from_url:
            out[key] = from_url          # a real UTM parameter — authoritative
            continue
        value = fields.get(key)
        value = value.strip() if isinstance(value, str) else value
        if not value:
            out[key] = None
            continue
        if key == "utm_source" and (
            value.lower() == "direct" or _REFERRER_RE.match(value)
        ):
            # Site's referrer fallback wearing a utm_source label. Move it —
            # but never over an explicit referrer_host from a fixed site build.
            out["referrer_host"] = out["referrer_host"] or value
            out[key] = None
        else:
            out[key] = value
    return out


def _attribution(fields: dict) -> dict[str, str | None]:
    """Resolve every ad-click identifier: explicit form field first, landing-page
    query string as fallback.

    Field-first ordering matters for forward compatibility — the site currently
    sends only ``gclid`` as a hidden field, so the URL is the sole source for the
    rest; when the site starts posting them properly the field wins with no code
    change here. It also means a form that captured the click id at a DIFFERENT
    moment than the landing hit is trusted over the URL.
    """
    params = _query_params(fields.get("landing_page"))
    out: dict[str, str | None] = {}
    for key in _ATTRIBUTION_PARAMS:
        explicit = fields.get(key)
        explicit = explicit.strip() if isinstance(explicit, str) else explicit
        # 'nan' is the sentinel the ETL treats as absent (queries.py:574); reject
        # it here too so it never reaches a column that downstream joins on.
        if explicit and explicit.lower() != "nan":
            out[key] = explicit
        else:
            value = params.get(key)
            out[key] = value if value and value.lower() != "nan" else None
    return out


# ── Shared insert ─────────────────────────────────────────────────────────────

def _store_submission(clinic: Clinic, fields: dict, *, source: str) -> None:
    """Ensure the table exists and stream one server-enriched row.

    ``fields`` carries the optional submission columns (``first_name`` … ``message``);
    missing keys become NULL. ``clinic_name``, ``submitted_at`` and ``ingested_at``
    are stamped server-side (both = now; the relay is synchronous). ``source``
    names the writer (``ingest_source``).
    Raises 500 if BigQuery rejects the row.
    """
    _ensure_table()
    attr = _attribution(fields)
    utm = _utm(fields)
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "clinic_id":     clinic.clinic_id,
        "clinic_name":   clinic.clinic_name,
        "first_name":    fields.get("first_name"),
        "last_name":     fields.get("last_name"),
        "phone_number":  fields.get("phone_number"),
        "email":         fields.get("email"),
        # From _utm, NOT straight off `fields`: the sites write the referrer host
        # into utm_source when no campaign tag is present, and that must not be
        # stored as a campaign source.
        "utm_source":    utm["utm_source"],
        "utm_medium":    utm["utm_medium"],
        "utm_campaign":  utm["utm_campaign"],
        "utm_term":      utm["utm_term"],
        "utm_content":   utm["utm_content"],
        "referrer_host": utm["referrer_host"],
        # Click ids come from _attribution (form field, else the landing URL) —
        # NOT straight off `fields`, or the gbraid-only submissions land blank.
        "gclid":          attr["gclid"],
        "fbclid":         attr["fbclid"],
        "gbraid":         attr["gbraid"],
        "wbraid":         attr["wbraid"],
        "gad_campaignid": attr["gad_campaignid"],
        "landing_page":  fields.get("landing_page"),
        "customer_type": fields.get("customer_type"),
        "message":       fields.get("message"),
        "submitted_at":  now,
        "form_id":       fields.get("form_id"),
        "form_title":    fields.get("form_title"),
        # JSON-typed column: streaming insert expects the value as a JSON string.
        "raw_fields":    json.dumps(fields["raw_fields"]) if fields.get("raw_fields") else None,
        "pretty":        fields.get("pretty"),
        "submission_id": fields.get("submission_id"),
        "ingest_source": source,
        "ingested_at":   now,
    }
    errors = bq_client.insert_rows_json(WEBFORMS_TABLE, [row])
    if errors:
        log.error("Webform insert failed for clinic_id=%s: %s", clinic.clinic_id, errors)
        raise HTTPException(status_code=500, detail="Failed to store submission")
    log.info("Stored webform submission for clinic_id=%s", clinic.clinic_id)


# ── Endpoint: JSON (our own site backends) ─────────────────────────────────────

@router.post("/webforms")
def ingest_webform(
    submission: WebformSubmission,
    _: None = Depends(verify_webform_secret),
    db: Session = Depends(get_session),
):
    """Ingest one web-form submission into ``ClinicData.webforms``.

    Returns ``{"status": "accepted"}`` on success. The clinic must exist and not
    be soft-deleted, otherwise 404.
    """
    clinic = db.get(Clinic, submission.clinic_id)
    if clinic is None or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Unknown clinic_id")

    _store_submission(clinic, submission.model_dump(), source=SOURCE_JSON_RELAY)
    return {"status": "accepted"}


# ── Endpoint: coverage (pipeline health) ──────────────────────────────────────

@router.get("/webforms/coverage")
def webform_coverage(
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Registry vs. reality for the webform pipeline, per clinic.

    Joins the ``jotform_forms`` registry (which clinics/forms SHOULD be
    delivering) against what has actually landed in ``ClinicData.webforms``,
    so "never wired", "wired but silent", and "receiving but unregistered"
    are all distinguishable. super_admin only — the response spans every
    instance.

    Per clinic: the registered forms with per-form counts (7d / 30d / total,
    last submission), clinic-level totals (these also cover rows without a
    ``form_id`` — the JSON endpoint and pre-provenance history don't stamp
    one), and ``unregistered_form_ids`` for submissions arriving from forms
    the registry doesn't know about (drift the provisioning script should
    reconcile). Clinics that appear in only one side are still listed.
    """
    if caller.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Access denied")

    registry = db.execute(
        select(JotformForm, Clinic.clinic_name)
        .join(Clinic, Clinic.clinic_id == JotformForm.clinic_id)
        .where(Clinic.deleted_at.is_(None))
    ).all()

    # One grouped scan: per-(clinic, form) stats; clinic rollups are summed
    # client-side so NULL-form_id rows still count toward the clinic totals.
    try:
        rows = list(bq_client.query(f"""
            SELECT
              clinic_id,
              form_id,
              COUNT(*) AS total,
              COUNTIF(submitted_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))  AS last_7d,
              COUNTIF(submitted_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)) AS last_30d,
              MAX(submitted_at) AS last_submission_at
            FROM `{WEBFORMS_TABLE}`
            GROUP BY clinic_id, form_id
        """).result())
    except NotFound:  # table not created yet (no submission has ever landed)
        rows = []

    stats: dict[tuple[str, str | None], dict] = {}
    for r in rows:
        stats[(r["clinic_id"], r["form_id"])] = {
            "total":              r["total"],
            "last_7d":            r["last_7d"],
            "last_30d":           r["last_30d"],
            "last_submission_at": r["last_submission_at"].isoformat() if r["last_submission_at"] else None,
        }

    clinics: dict[str, dict] = {}

    def _clinic_entry(clinic_id: str, clinic_name: str | None) -> dict:
        return clinics.setdefault(clinic_id, {
            "clinic_id":              clinic_id,
            "clinic_name":            clinic_name,
            "forms":                  [],
            "unregistered_form_ids":  [],
            "total": 0, "last_7d": 0, "last_30d": 0,
            "last_submission_at":     None,
        })

    registered_forms = set()
    for form, clinic_name in registry:
        entry = _clinic_entry(form.clinic_id, clinic_name)
        s = stats.get((form.clinic_id, form.jotform_form_id), {})
        registered_forms.add((form.clinic_id, form.jotform_form_id))
        entry["forms"].append({
            "jotform_form_id":    form.jotform_form_id,
            "form_title":         form.form_title,
            "active":             bool(form.active),
            "total":              s.get("total", 0),
            "last_7d":            s.get("last_7d", 0),
            "last_30d":           s.get("last_30d", 0),
            "last_submission_at": s.get("last_submission_at"),
        })

    for (clinic_id, form_id), s in stats.items():
        entry = _clinic_entry(clinic_id, None)
        entry["total"]    += s["total"]
        entry["last_7d"]  += s["last_7d"]
        entry["last_30d"] += s["last_30d"]
        if s["last_submission_at"] and (
            entry["last_submission_at"] is None
            or s["last_submission_at"] > entry["last_submission_at"]
        ):
            entry["last_submission_at"] = s["last_submission_at"]
        if form_id and (clinic_id, form_id) not in registered_forms:
            entry["unregistered_form_ids"].append(form_id)

    # Clinics seen only in BQ have no name from the registry join — resolve it.
    for clinic_id, entry in clinics.items():
        if entry["clinic_name"] is None:
            clinic = db.get(Clinic, clinic_id)
            entry["clinic_name"] = clinic.clinic_name if clinic else None

    return sorted(clinics.values(), key=lambda c: (c["clinic_name"] or "").lower())
