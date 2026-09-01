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

    # Location map: a form serving a whole group asks which site the patient
    # wants. Compare the form's live location options against
    # jotform_form_locations and report what is unmapped:
    python configure_jotform_webhooks.py --locations
    python configure_jotform_webhooks.py --locations --apply
    # …and additionally link options whose leading label is EXACTLY a clinic
    # name of the same instance (the rest still need mapping by hand):
    python configure_jotform_webhooks.py --locations --apply --link-by-name

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
- A webhook URL names ONE clinic. For a group running every site off one form
  that clinic is only a default: ``jotform_form_locations`` maps the patient's
  "choose your location" answer to a clinic and ``api/webforms.py`` re-points the
  row. ``--locations`` maintains that map. Wiring the webhook without it sends
  the whole group's leads to the default clinic — on the Sense of Hearing form
  that is 86% of them.
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
from api.core.orm import Clinic, JotformForm, JotformFormLocation
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
    # Google sends gbraid (cross-device) or wbraid (iOS post-ATT) INSTEAD of a
    # gclid on many clicks, and gad_campaignid IS the campaign id — a paid click
    # is invisible without a field to land in.
    "gclid", "gbraid", "wbraid", "fbclid", "gad_campaignid",
    # The referring host. Kept separate from utm_source on purpose: the sites
    # used to write the referrer INTO utm_source, which made downstream report
    # "google.com" as a campaign. See api/webforms.py::_utm.
    "referrer_host",
    "landing_page",
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


def _location_options(form_id: str, api_key: str) -> list[str]:
    """The form's "choose your location" dropdown options, in builder order.

    A form can carry SEVERAL location dropdowns revealed by condition — the Sense
    of Hearing form has four (adult, 6-17, APD, 10-months-up) with overlapping
    option lists — so every location dropdown's options are pooled and
    de-duplicated. The resolver in api/webforms.py matches on the answer's value
    and does not care which field produced it, so a pooled list is exactly right.

    Selected by label, not by field name: the labels all say "location"
    ("Choose Your Location", "Choose your location (APD)", …) while the unique
    names are the builder's autogenerated chooseYour/chooseYour20/…, which carry
    no meaning and change when fields are reordered. Filtering by label also
    keeps unrelated dropdowns on the same form (preferred contact method,
    appointment time) out of the pool.
    """
    questions = _request("GET", f"/form/{form_id}/questions", api_key).get("content") or {}
    seen: dict[str, None] = {}
    for q in questions.values():
        if not isinstance(q, dict) or q.get("type") != "control_dropdown":
            continue
        if "location" not in (q.get("text") or "").lower():
            continue
        for opt in (q.get("options") or "").split("|"):
            opt = opt.strip()
            if opt:
                seen.setdefault(opt, None)
    return list(seen)


def _leading_label(option: str) -> str:
    """"Burlington: 11 - 1960 Appleby Line" -> "Burlington".

    Split on the FIRST ": " only — several options carry a second colon inside
    the address ("Guelph: 104: 83 Dawson Road").
    """
    return option.split(": ", 1)[0].strip()


def locations(form_filter: str | None, api_key: str, apply: bool,
              link_by_name: bool) -> None:
    """Reconcile each registered form's location options against the map.

    Reports three states per form, all of which silently misattribute leads if
    left alone:
      - unmapped   the form offers the option, nothing routes it (its leads go
                   to the form's default clinic)
      - no clinic  mapped, but the clinic does not exist yet — expected during a
                   group's rollout
      - stale      a mapped option the form no longer offers (renamed in the
                   builder), so its rows can never match again

    With --apply, unmapped options are RECORDED with a NULL clinic_id rather than
    guessed at: an option written to the wrong clinic is worse than one written
    to none, because the fallback is visible in the logs and a wrong mapping is
    not. --link-by-name opts into the one guess that is safe to make — an
    option whose leading label is exactly a clinic name of the same instance.
    """
    registry = [r for r in _load_registry()
                if r["active"] and (not form_filter or r["form_id"] == form_filter)]
    if not registry:
        sys.exit("No active registered form matches "
                 f"{form_filter!r}." if form_filter else "No active registered forms.")

    mode = "APPLY" if apply else "DRY-RUN (use --apply to write)"
    print(f"=== Jotform location map — {mode} ===\n")

    with session_scope() as db:
        for r in registry:
            form_id = r["form_id"]
            print(f"form {form_id}  ({r['clinic_name']} — {r['title']!r})")
            try:
                options = _location_options(form_id, api_key)
            except Exception as exc:
                print(f"  ! option fetch failed: {exc}\n")
                continue
            if not options:
                print("  no location dropdown — single-site form, nothing to map\n")
                continue

            rows = {
                l.option_value: l
                for l in db.execute(
                    select(JotformFormLocation)
                    .where(JotformFormLocation.jotform_form_id == form_id)
                ).scalars()
            }
            # Candidate clinics for --link-by-name: the instance the form's
            # default clinic belongs to. Matching across instances would let one
            # client's form route into another's clinic.
            default_clinic = db.get(Clinic, r["clinic_id"])
            by_name = {
                c.clinic_name.strip().lower(): c
                for c in db.execute(
                    select(Clinic).where(
                        Clinic.instance_id == default_clinic.instance_id,
                        Clinic.deleted_at.is_(None),
                    )
                ).scalars()
            }

            for option in options:
                row = rows.pop(option, None)
                if row is not None and row.clinic_id:
                    clinic = db.get(Clinic, row.clinic_id)
                    name = clinic.clinic_name if clinic else "MISSING CLINIC"
                    flag = "" if row.active else "  (inactive)"
                    print(f"  ✓ {option}\n      -> {name}{flag}")
                    continue

                match = by_name.get(_leading_label(option).lower()) if link_by_name else None
                verb = "mapped" if row is None else "no clinic"
                if not apply:
                    target = f"WOULD LINK -> {match.clinic_name}" if match else \
                             ("WOULD RECORD (no clinic yet)" if row is None else "still unmapped")
                    print(f"  + {option}\n      {target}")
                    continue

                if row is None:
                    row = JotformFormLocation(jotform_form_id=form_id,
                                              option_value=option, clinic_id=None)
                    db.add(row)
                if match:
                    row.clinic_id = match.clinic_id
                    print(f"  + {option}\n      LINKED -> {match.clinic_name}")
                else:
                    print(f"  + {option}\n      RECORDED ({verb}, no clinic yet)")

            for option, row in rows.items():
                # Mapped but no longer offered: a renamed option can never match
                # an incoming submission again, so it is dead weight that looks
                # like coverage.
                print(f"  ! STALE (form no longer offers this option): {option}")
            print()


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
    ap.add_argument("--locations", action="store_true",
                    help="reconcile each form's location options against jotform_form_locations")
    ap.add_argument("--link-by-name", action="store_true",
                    help="with --locations --apply: also link an option whose leading "
                         "label is exactly a clinic name of the same instance")
    args = ap.parse_args()

    if args.link_by_name and not args.locations:
        ap.error("--link-by-name only means anything with --locations")

    api_key = _api_key()

    if args.discover:
        discover(api_key, args.apply)
        return

    if args.locations:
        locations(args.form, api_key, args.apply, args.link_by_name)
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
