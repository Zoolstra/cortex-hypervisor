#!/usr/bin/env python3
"""
Provision Jotform lead-capture: webhooks + hidden UTM fields, per clinic.

Wires Jotform-hosted lead forms into ``ClinicData.webforms`` by (a) adding a
webhook that POSTs each submission to ``/webforms/jotform/{clinic_id}`` and
(b) optionally adding the hidden UTM fields that carry campaign attribution.
Idempotent: existing webhooks/fields are detected and left alone, so re-runs
are safe.

The form → clinic mapping lives in Cloud SQL ``jotform_forms`` (alembic 0019)
— the registry shared with the admin UI (campaign type ``jotform``) and the
``GET /webforms/coverage`` health endpoint. This script only READS it (except
``--discover --apply``, which backfills it); add or remove mappings via the
admin UI or ``POST /campaigns/{clinic_id}``.

Why a script and not the claude.ai Jotform connector: that connector is
READ-ONLY (its OAuth token 401s on webhook/field creation). Management calls
need a real Jotform API key with full access.

Credentials
-----------
- Jotform API key: env ``JOTFORM_API_KEY`` or Secret Manager ``jotform-api-key``.
  Create one at Jotform → Settings → API → Create New Key (Full Access), for the
  account that owns the forms (the shared "Dean_Matt" agency account).
- Webhook shared secret: Secret Manager ``webform-webhook-secret`` (same secret
  the hypervisor validates on the ``token`` query param).
- Cloud SQL: standard ADC (same as running the hypervisor locally).

Usage
-----
    cd cortex-hypervisor && source venv/bin/activate

    # Preview every change without writing (default is dry-run):
    python configure_jotform_webhooks.py

    # Apply webhooks only:
    python configure_jotform_webhooks.py --apply

    # Apply webhooks AND add hidden UTM fields:
    python configure_jotform_webhooks.py --apply --with-utm

    # Restrict to one form:
    python configure_jotform_webhooks.py --apply --form 261767450350053

    # Audit: scan EVERY form in the Jotform account for webhooks pointing at
    # the hypervisor and report drift against the registry, both directions.
    # With --apply, forms wired in Jotform but missing from the registry are
    # inserted (clinic_id is recovered from the webhook URL path):
    python configure_jotform_webhooks.py --discover
    python configure_jotform_webhooks.py --discover --apply

Notes
-----
- INLINED forms (e.g. the CEC Astro site pastes the form's source directly into
  the page rather than iframing it) need the hidden UTM inputs to physically
  exist in the site's HTML too — adding the server-side field alone is not enough
  because the inlined snapshot is static. With --with-utm this script prints the
  exact ``q{qid}_{name}`` input tags to paste into the inlined form component.
- IFRAME forms (Alto/Prairie) only need the server-side hidden fields; the site
  appends the params to the iframe URL and Jotform prefills by unique name.
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

from sqlalchemy import select

from api.core.db import session_scope
from api.core.orm import Clinic, JotformForm
from api.core.secrets import get_secret

JOTFORM_API = "https://api.jotform.com"
HYPERVISOR = "https://cortex-hypervisor-45007506504.us-central1.run.app"

# Inlined-form clinics: --with-utm prints paste-ready hidden <input> tags because
# the field must exist in the site's static HTML, not just server-side. This is
# presentation-only (which snippet to print), so it stays script-local rather
# than in the registry.
INLINED_FORMS = {"261767450350053", "261766594045062"}

# Jotform unique names the hypervisor parser reads (api/webforms.py::_parse_jotform
# tracking()). Order preserved for stable field ordering on the form.
UTM_FIELDS = [
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "landing_page",
]

# clinic_id sits in the webhook URL path: .../webforms/jotform/{clinic_id}?token=…
_WEBHOOK_CLINIC = re.compile(r"/webforms/jotform/([0-9a-fA-F-]{36})")


def _api_key() -> str:
    key = os.environ.get("JOTFORM_API_KEY") or (get_secret("jotform-api-key") or "").strip()
    if not key:
        sys.exit("No Jotform API key: set JOTFORM_API_KEY or Secret Manager 'jotform-api-key'.")
    return key


def _request(method: str, path: str, api_key: str, fields: dict | None = None) -> dict:
    """Call the Jotform REST API. POST/PUT send x-www-form-urlencoded (bracket
    notation), which is what Jotform expects — JSON bodies are rejected."""
    sep = "&" if "?" in path else "?"
    url = f"{JOTFORM_API}{path}{sep}apiKey={urllib.parse.quote(api_key)}"
    data = urllib.parse.urlencode(fields).encode() if fields else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _webhook_url(clinic_id: str, secret: str) -> str:
    return f"{HYPERVISOR}/webforms/jotform/{clinic_id}?token={secret}"


def _form_webhooks(form_id: str, api_key: str) -> list[str]:
    existing = _request("GET", f"/form/{form_id}/webhooks", api_key).get("content") or []
    # content is a dict {index: url} or a list depending on Jotform version.
    return list(existing.values()) if isinstance(existing, dict) else list(existing)


def _load_registry() -> list[dict]:
    """Active-first snapshot of jotform_forms joined to clinic names."""
    with session_scope() as db:
        rows = db.execute(
            select(JotformForm, Clinic.clinic_name)
            .join(Clinic, Clinic.clinic_id == JotformForm.clinic_id)
            .where(Clinic.deleted_at.is_(None))
            .order_by(JotformForm.active.desc(), Clinic.clinic_name)
        ).all()
        return [
            {
                "form_id":     f.jotform_form_id,
                "clinic_id":   f.clinic_id,
                "clinic_name": clinic_name,
                "title":       f.form_title,
                "active":      bool(f.active),
            }
            for f, clinic_name in rows
        ]


def ensure_webhook(form_id: str, clinic_id: str, secret: str, api_key: str, apply: bool) -> None:
    urls = _form_webhooks(form_id, api_key)
    target = _webhook_url(clinic_id, secret)
    if any(clinic_id in u and "/webforms/jotform/" in u for u in urls):
        print(f"  webhook  ✓ already set ({len(urls)} hook(s))")
        return
    if not apply:
        print(f"  webhook  + WOULD ADD -> …/webforms/jotform/{clinic_id}")
        return
    _request("POST", f"/form/{form_id}/webhooks", api_key, {"webhookURL": target})
    print(f"  webhook  + ADDED -> …/webforms/jotform/{clinic_id}")


def ensure_utm_fields(form_id: str, api_key: str, apply: bool) -> dict:
    """Add any missing hidden UTM fields. Returns {unique_name: qid} for all
    present-or-added fields (used to emit inlined-form input tags)."""
    qs = _request("GET", f"/form/{form_id}/questions", api_key).get("content") or {}
    by_name = {q.get("name"): qid for qid, q in qs.items()}
    result: dict[str, str] = {}
    order = max((int(q.get("order", 0)) for q in qs.values()), default=0)
    for name in UTM_FIELDS:
        if name in by_name:
            result[name] = by_name[name]
            print(f"  field    ✓ {name} (qid {by_name[name]})")
            continue
        if not apply:
            print(f"  field    + WOULD ADD hidden {name}")
            continue
        order += 1
        resp = _request("POST", f"/form/{form_id}/questions", api_key, {
            "question[type]": "control_textbox",
            "question[text]": name,
            "question[name]": name,
            "question[hidden]": "Yes",
            "question[order]": str(order),
            "question[validation]": "None",
        })
        qid = (resp.get("content") or {}).get("qid")
        result[name] = qid
        print(f"  field    + ADDED hidden {name} (qid {qid})")
    return result


def discover(api_key: str, apply: bool) -> None:
    """Audit Jotform ↔ registry drift, both directions.

    Walks every non-deleted form in the account (one webhook call per form —
    slow on the 150+-form agency account, expect a minute or two) and extracts
    hypervisor-pointing webhooks. Reports:
      - wired in Jotform but missing from the registry (--apply inserts them)
      - registered active but with NO webhook on the Jotform side (fix by
        running the default provisioning mode)
    """
    registry = {r["form_id"]: r for r in _load_registry()}

    forms = _request("GET", "/user/forms?limit=1000", api_key).get("content") or []
    forms = [f for f in forms if f.get("status") != "DELETED"]
    print(f"Scanning {len(forms)} form(s) for hypervisor webhooks…\n")

    wired: dict[str, dict] = {}  # form_id -> {clinic_id, title}
    for f in forms:
        form_id = str(f.get("id"))
        try:
            urls = _form_webhooks(form_id, api_key)
        except Exception as exc:
            print(f"  ! {form_id} webhook fetch failed: {exc}")
            continue
        for u in urls:
            m = _WEBHOOK_CLINIC.search(u)
            if m and HYPERVISOR in u:
                wired[form_id] = {"clinic_id": m.group(1), "title": f.get("title")}

    missing_from_registry = {fid: w for fid, w in wired.items() if fid not in registry}
    not_wired = [r for r in registry.values() if r["active"] and r["form_id"] not in wired]

    print(f"Jotform-side wirings found: {len(wired)}")
    for fid, w in sorted(wired.items()):
        mark = "✓ registered" if fid in registry else "✗ NOT IN REGISTRY"
        print(f"  {fid}  clinic {w['clinic_id']}  {w['title']!r}  {mark}")

    if not_wired:
        print("\nRegistered active but NO webhook on Jotform (run default mode to fix):")
        for r in not_wired:
            print(f"  {r['form_id']}  {r['clinic_name']}  {r['title']!r}")

    if missing_from_registry:
        verb = "Inserting" if apply else "WOULD insert (use --apply)"
        print(f"\n{verb} {len(missing_from_registry)} registry row(s):")
        with session_scope() as db:
            for fid, w in sorted(missing_from_registry.items()):
                clinic = db.get(Clinic, w["clinic_id"])
                if clinic is None or clinic.deleted_at is not None:
                    print(f"  ! {fid} webhook targets unknown clinic {w['clinic_id']} — skipped")
                    continue
                print(f"  {fid} -> {clinic.clinic_name} ({w['title']!r})")
                if apply:
                    db.add(JotformForm(
                        clinic_id=w["clinic_id"],
                        jotform_form_id=fid,
                        form_title=w["title"],
                    ))

    if not missing_from_registry and not not_wired:
        print("\nRegistry and Jotform agree — no drift.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Provision Jotform webhooks + UTM fields.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--with-utm", action="store_true", help="also add hidden UTM fields")
    ap.add_argument("--form", help="only this form_id")
    ap.add_argument("--discover", action="store_true",
                    help="audit Jotform↔registry drift; with --apply, backfill registry rows")
    args = ap.parse_args()

    api_key = _api_key()

    if args.discover:
        discover(api_key, args.apply)
        return

    secret = (get_secret("webform-webhook-secret") or "").strip()
    if not secret:
        sys.exit("No webform-webhook-secret in Secret Manager.")

    registry = _load_registry()
    if args.form:
        registry = [r for r in registry if r["form_id"] == args.form]
        if not registry:
            sys.exit(f"{args.form} not in the jotform_forms registry "
                     "(add it via the admin UI or POST /campaigns).")

    mode = "APPLY" if args.apply else "DRY-RUN (use --apply to write)"
    print(f"=== Jotform provisioning — {mode} ===\n")
    for r in registry:
        if not r["active"]:
            print(f"form {r['form_id']}  ({r['clinic_name']} — {r['title']!r})  SKIPPED: inactive\n")
            continue
        print(f"form {r['form_id']}  ->  clinic {r['clinic_id']}  ({r['clinic_name']} — {r['title']!r})")
        ensure_webhook(r["form_id"], r["clinic_id"], secret, api_key, args.apply)
        if args.with_utm:
            qids = ensure_utm_fields(r["form_id"], api_key, args.apply)
            if r["form_id"] in INLINED_FORMS and args.apply:
                print("  ↳ INLINED form — paste these hidden inputs into the site's <form>:")
                for name, qid in qids.items():
                    print(f'       <input type="hidden" id="input_{qid}" '
                          f'name="q{qid}_{name}" data-utm="{name}" />')
        print()


if __name__ == "__main__":
    main()
