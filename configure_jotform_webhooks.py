#!/usr/bin/env python3
"""
Provision Jotform lead-capture: webhooks + hidden UTM fields, per clinic.

Wires Jotform-hosted lead forms into ``ClinicData.webforms`` by (a) adding a
webhook that POSTs each submission to ``/webforms/jotform/{clinic_id}`` and
(b) optionally adding the hidden UTM fields that carry campaign attribution.
Idempotent: existing webhooks/fields are detected and left alone, so re-runs
are safe.

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

The form → clinic_id MAPPING below is the source of truth for which form feeds
which clinic. Map forms by the clinic's OWNED landing-page site, not by the
Jotform account listing (the shared account holds 150+ forms across many
businesses). clinic_ids come from Cloud SQL ``clinics`` (see --list-clinics in
the repo notes).

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
import sys
import urllib.parse
import urllib.request

from api.core.secrets import get_secret

JOTFORM_API = "https://api.jotform.com"
HYPERVISOR = "https://cortex-hypervisor-45007506504.us-central1.run.app"

# form_id -> clinic_id. Keep in sync with the owned-site → clinic mapping.
MAPPING = {
    # Calgary Ear Centre (clinic 5e256c93) — INLINED forms on the CEC Astro site.
    "261767450350053": "5e256c93-1369-4f6b-9106-456ab08a1b55",  # CortexHQ Lead (contact)
    "261766594045062": "5e256c93-1369-4f6b-9106-456ab08a1b55",  # CortexHQ Lead LP (appt request)
    # Alto Hearing (clinic 1ce69d99) — iframe embeds; contact+book already wired,
    # this closes the missing "Check Your Hearing" survey webhook.
    "261067886127061": "1ce69d99-ec12-4d74-9d31-54806201987f",  # hearing-survey
    # Prairie Hearing Centers (clinic 07752b12, etl_enabled=0 — wire anyway so
    # data accumulates before ETL is switched on) — iframe embeds on prairie site.
    "260826975341060": "07752b12-31e5-4168-af37-1e894b0707e6",  # main lead form
    "260836177941263": "07752b12-31e5-4168-af37-1e894b0707e6",  # landing-page opt-in
}

# Inlined-form clinics: --with-utm prints paste-ready hidden <input> tags because
# the field must exist in the site's static HTML, not just server-side.
INLINED_FORMS = {"261767450350053", "261766594045062"}

# Jotform unique names the hypervisor parser reads (api/webforms.py::_parse_jotform
# tracking()). Order preserved for stable field ordering on the form.
UTM_FIELDS = [
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "landing_page",
]


def _api_key() -> str:
    key = os.environ.get("JOTFORM_API_KEY") or (get_secret("jotform-api-key") or "").strip()
    if not key:
        sys.exit("No Jotform API key: set JOTFORM_API_KEY or Secret Manager 'jotform-api-key'.")
    return key


def _request(method: str, path: str, api_key: str, fields: dict | None = None) -> dict:
    """Call the Jotform REST API. POST/PUT send x-www-form-urlencoded (bracket
    notation), which is what Jotform expects — JSON bodies are rejected."""
    url = f"{JOTFORM_API}{path}?apiKey={urllib.parse.quote(api_key)}"
    data = urllib.parse.urlencode(fields).encode() if fields else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _webhook_url(clinic_id: str, secret: str) -> str:
    return f"{HYPERVISOR}/webforms/jotform/{clinic_id}?token={secret}"


def ensure_webhook(form_id: str, clinic_id: str, secret: str, api_key: str, apply: bool) -> None:
    existing = _request("GET", f"/form/{form_id}/webhooks", api_key).get("content") or []
    # content is a dict {index: url} or a list depending on Jotform version.
    urls = list(existing.values()) if isinstance(existing, dict) else list(existing)
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Provision Jotform webhooks + UTM fields.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--with-utm", action="store_true", help="also add hidden UTM fields")
    ap.add_argument("--form", help="only this form_id")
    args = ap.parse_args()

    api_key = _api_key()
    secret = (get_secret("webform-webhook-secret") or "").strip()
    if not secret:
        sys.exit("No webform-webhook-secret in Secret Manager.")

    forms = {args.form: MAPPING[args.form]} if args.form else MAPPING
    if args.form and args.form not in MAPPING:
        sys.exit(f"{args.form} not in MAPPING.")

    mode = "APPLY" if args.apply else "DRY-RUN (use --apply to write)"
    print(f"=== Jotform provisioning — {mode} ===\n")
    for form_id, clinic_id in forms.items():
        print(f"form {form_id}  ->  clinic {clinic_id}")
        ensure_webhook(form_id, clinic_id, secret, api_key, args.apply)
        if args.with_utm:
            qids = ensure_utm_fields(form_id, api_key, args.apply)
            if form_id in INLINED_FORMS and args.apply:
                print("  ↳ INLINED form — paste these hidden inputs into the site's <form>:")
                for name, qid in qids.items():
                    print(f'       <input type="hidden" id="input_{qid}" '
                          f'name="q{qid}_{name}" data-utm="{name}" />')
        print()


if __name__ == "__main__":
    main()
