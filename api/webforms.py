"""
Web-form ingestion — relays form submissions from our clinic sites into
``ClinicData.webforms`` for the patient-acquisition funnel.

Two entry points land in the same table:

``POST /webforms`` (JSON)
    A form on one of our sites is submitted; the site's backend POSTs the
    captured fields here (server-to-server, so the shared secret never reaches
    the browser). Auth is the ``X-Webform-Secret`` header.

``POST /webforms/jotform/{clinic_id}`` (Jotform webhook)
    Sites whose forms are hosted by Jotform (e.g. Alto Hearing) configure a
    Jotform webhook here. Jotform POSTs ``multipart/form-data`` and CANNOT send
    custom headers, so the shared secret travels as a ``token`` query param and
    ``clinic_id`` sits in the path (each form's webhook URL targets its clinic).
    The Jotform field payload arrives JSON-encoded in ``rawRequest`` and is
    mapped onto the same columns.

Auth
----
A single shared secret (Secret Manager: ``webform-webhook-secret``) guards both
entry points. ``clinic_id`` is verified against Cloud SQL before any write — an
unknown or soft-deleted clinic is rejected with 404, so junk never lands in
BigQuery.

Storage
-------
Rows go to ``ClinicData.webforms`` (analytics dataset, NOT Blueprint_PHI) via a
streaming insert — real-time, append-only. The table is created lazily on first
write (idempotent), matching the ``transcript_analysis/callscoring.py`` pattern.
Schema is the locked patient-acquisition spec (clinic_id, first/last name,
phone, email, utm_source, utm_content, customer_type, message) plus
server-side enrichment: ``clinic_name``, ``landing_page``, and
``submitted_at``.
"""
import json
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from google.cloud import bigquery
from google.cloud.exceptions import NotFound
from sqlalchemy.orm import Session

from api.core.db import get_session
from api.core.orm import Clinic
from api.core.secrets import get_secret
from api.deps import bq_client
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

def _ensure_table() -> None:
    """Create ClinicData.webforms if it doesn't exist. Idempotent; runs once."""
    global _table_ready
    if _table_ready:
        return
    schema = [
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
        bigquery.SchemaField("landing_page", "STRING"),
        bigquery.SchemaField("customer_type", "STRING"),
        bigquery.SchemaField("message",       "STRING"),
        bigquery.SchemaField("submitted_at", "TIMESTAMP", mode="REQUIRED"),
    ]
    # Check existence first so we don't issue a create — and log a benign
    # "Already Exists" audit error — on every cold start.
    try:
        bq_client.get_table(WEBFORMS_TABLE)
    except NotFound:
        bq_client.create_table(bigquery.Table(WEBFORMS_TABLE, schema=schema))
    _table_ready = True


# ── Shared insert ─────────────────────────────────────────────────────────────

def _store_submission(clinic: Clinic, fields: dict) -> None:
    """Ensure the table exists and stream one server-enriched row.

    ``fields`` carries the optional submission columns (``first_name`` … ``message``);
    missing keys become NULL. ``clinic_name`` and ``submitted_at`` are stamped
    server-side. Raises 500 if BigQuery rejects the row.
    """
    _ensure_table()
    row = {
        "clinic_id":     clinic.clinic_id,
        "clinic_name":   clinic.clinic_name,
        "first_name":    fields.get("first_name"),
        "last_name":     fields.get("last_name"),
        "phone_number":  fields.get("phone_number"),
        "email":         fields.get("email"),
        "utm_source":    fields.get("utm_source"),
        "utm_medium":    fields.get("utm_medium"),
        "utm_campaign":  fields.get("utm_campaign"),
        "utm_term":      fields.get("utm_term"),
        "utm_content":   fields.get("utm_content"),
        "gclid":         fields.get("gclid"),
        "fbclid":        fields.get("fbclid"),
        "landing_page":  fields.get("landing_page"),
        "customer_type": fields.get("customer_type"),
        "message":       fields.get("message"),
        "submitted_at":  datetime.now(timezone.utc).isoformat(),
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

    _store_submission(clinic, submission.model_dump())
    return {"status": "accepted"}


# ── Endpoint: Jotform webhook ──────────────────────────────────────────────────

# Jotform prepends ``q<id>_`` to every field's unique name in ``rawRequest``
# (e.g. ``q7_utm_source``). Strip it to recover the unique name we configured.
# Strip *repeated* prefixes: if a field's Unique Name itself starts with a
# ``q<n>_`` token (e.g. someone set it to ``q2_fullname0``), Jotform emits
# ``q<id>_q2_fullname0`` and a single strip would leave ``q2_fullname0`` — which
# matches no column. Stripping greedily recovers ``fullname0`` either way.
_JOTFORM_PREFIX = re.compile(r"^(?:q\d+_)+")


def _clean(v) -> str | None:
    """Trim a string value; non-strings and blanks collapse to None."""
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v or None


def _camel(snake: str) -> str:
    """utm_source -> utmSource (Jotform's default unique-name casing)."""
    head, *rest = snake.split("_")
    return head + "".join(p.title() for p in rest)


def _parse_jotform(raw_request: str) -> dict:
    """Map a Jotform ``rawRequest`` JSON blob onto our submission columns.

    Jotform names fields in ``rawRequest`` by their auto-generated unique name —
    ``fullname0``, ``email1``, ``phone2``, ``textarea4`` — unless you set a custom
    unique name. So contact fields are matched by **field-type prefix** (robust to
    the numeric suffixes), and tracking fields by exact name (with a camelCase
    fallback). Name/phone fields submit as objects (``{first,last}`` /
    ``{full,...}``); everything else is a plain string. Anything absent maps to
    NULL — extraction never raises.
    """
    try:
        raw = json.loads(raw_request or "{}")
    except (ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    fields = {
        _JOTFORM_PREFIX.sub("", k): v
        for k, v in raw.items()
        if isinstance(k, str)
    }

    # Diagnostic: log the field names Jotform sent (KEYS ONLY — no values, so no
    # PII lands in logs).
    log.info("Jotform rawRequest: %d chars, field keys=%s",
             len(raw_request or ""), sorted(fields.keys()))

    def pick(*prefixes):
        """Value of the first field whose lowercased name starts with a prefix."""
        for k, v in fields.items():
            kl = k.lower()
            if any(kl.startswith(p) for p in prefixes):
                return v
        return None

    def tracking(name):
        """Tracking params keyed by exact snake_case or camelCase unique name."""
        return fields.get(name) or fields.get(_camel(name))

    # Name: full-name field is {first, last}; a plain string splits on first space.
    first = last = None
    name = pick("fullname", "name", "yourname")
    if isinstance(name, dict):
        first = _clean(name.get("first"))
        last = _clean(name.get("last"))
    elif isinstance(name, str):
        parts = name.strip().split(None, 1)
        first = _clean(parts[0]) if parts else None
        last = _clean(parts[1]) if len(parts) > 1 else None

    # Phone: object {full, area, phone, ...} or a plain string.
    phone = pick("phone", "mobile")
    if isinstance(phone, dict):
        phone = phone.get("full") or phone.get("phone")

    # Message: prefer a configured "message", else a textarea / "how can we help".
    message = (pick("message", "comment")
               or pick("textarea", "howcan", "whatcan", "reason", "inquiry", "help"))

    return {
        "first_name":    first,
        "last_name":     last,
        "email":         _clean(pick("email", "e-mail")),
        "phone_number":  _clean(phone),
        "utm_source":    _clean(tracking("utm_source")),
        "utm_medium":    _clean(tracking("utm_medium")),
        "utm_campaign":  _clean(tracking("utm_campaign")),
        "utm_term":      _clean(tracking("utm_term")),
        "utm_content":   _clean(tracking("utm_content")),
        "gclid":         _clean(tracking("gclid")),
        "fbclid":        _clean(tracking("fbclid")),
        "landing_page":  _clean(tracking("landing_page")),
        "customer_type": _clean(pick("newor", "returning", "customertype", "newcustomer")),
        "message":       _clean(message),
    }


@router.post("/webforms/jotform/{clinic_id}")
async def ingest_jotform_webform(
    request: Request,
    clinic_id: str,
    token: str = Query("", description="shared secret; Jotform webhooks can't send headers"),
    db: Session = Depends(get_session),
):
    """Relay a Jotform webhook submission into ``ClinicData.webforms``.

    Auth is the ``token`` query param (Jotform cannot send custom headers), checked
    against the same ``webform-webhook-secret``. ``clinic_id`` is path-scoped and
    validated against Cloud SQL. Jotform POSTs multipart form-data; the field
    values arrive JSON-encoded in the ``rawRequest`` part.
    """
    expected = (get_secret("webform-webhook-secret") or "").strip()
    if not expected or token.strip() != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing webform token")

    clinic = db.get(Clinic, clinic_id)
    if clinic is None or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Unknown clinic_id")

    form = await request.form()
    # Diagnostic: which parts did Jotform actually send? (KEYS ONLY — no values.)
    log.info("Jotform webhook clinic_id=%s top-level keys=%s", clinic_id, sorted(form.keys()))
    raw_request = form.get("rawRequest") or ""

    _store_submission(clinic, _parse_jotform(raw_request))
    log.info("Stored Jotform submission clinic_id=%s form=%s submission=%s",
             clinic_id, form.get("formID"), form.get("submissionID"))
    return {"status": "accepted"}
