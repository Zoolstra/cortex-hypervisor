#!/usr/bin/env python3
"""
Jotform lead-form maintenance: hidden UTM fields, the location map, drift audit.

Submissions are INGESTED by the ETL job ``jotform-ingest``
(``cortex-data-ingestion/app/jotform/``), which polls the Jotform API for every
form in the Cloud SQL ``jotform_forms`` registry. Registering a form (admin UI →
clinic → Campaigns → type ``jotform``, or ``POST /campaigns/{clinic_id}``) IS the
wiring — there is no webhook to provision any more (removed 2026-09-04; see
``resources/jotform-api-polling-plan.md``). What still has to be done on the
Jotform side, and what this script does:

  --with-utm       add the hidden UTM/click-id fields the site prefills through
                   the iframe URL (idempotent; prints paste-ready <input> tags for
                   INLINED forms)
  --locations      a group's shared form asks the patient which site they want:
                   reconcile its live dropdown options against
                   ``jotform_form_locations`` and report/record what is unmapped
  --discover       drift audit: forms in the account with submissions in the last
                   90 days that are NOT registered (their leads land nowhere), and
                   registered forms Jotform no longer knows
  --remove-webhooks residue check: report/delete any hypervisor-pointing webhook
                   still on a registered form (none should exist)

Why a script and not the claude.ai Jotform connector: that connector is
READ-ONLY (its OAuth token 401s on field creation). Management calls need a real
Jotform API key with full access.

Credentials
-----------
- Jotform API key: env ``JOTFORM_API_KEY`` or Secret Manager ``jotform-api-key``.
- Cloud SQL: standard ADC (same as running the hypervisor locally).

Usage
-----
    cd cortex-hypervisor && source venv/bin/activate

    python configure_jotform.py --with-utm                  # dry-run
    python configure_jotform.py --with-utm --apply [--form 261767450350053]
    python configure_jotform.py --locations [--apply] [--link-by-name] [--form ID]
    python configure_jotform.py --discover
    python configure_jotform.py --remove-webhooks [--apply]

Notes
-----
- INLINED forms (e.g. the CEC Astro site pastes the form's source directly into
  the page rather than iframing it) need the hidden UTM inputs to physically
  exist in the site's HTML too — adding the server-side field alone is not enough
  because the inlined snapshot is static. With --with-utm this script prints the
  exact ``q{qid}_{name}`` input tags to paste into the inlined form component.
- IFRAME forms (Alto/Prairie) only need the server-side hidden fields; the site
  appends the params to the iframe URL and Jotform prefills by unique name.
- A registry row names ONE clinic. For a group running every site off one form
  that clinic is only a default: ``jotform_form_locations`` maps the patient's
  "choose your location" answer to a clinic and the ETL re-points the row.
  ``--locations`` maintains that map. Registering a group's form without it
  sends the whole group's leads to the default clinic — on the Sense of Hearing
  form that is 86% of them.
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

# Jotform unique names the ETL parser reads (cortex-data-ingestion/app/jotform/
# parse.py::parse_fields → tracking()). Order preserved for stable field ordering.
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

# The retired webhook URL shape, still matched by --remove-webhooks so any
# straggler can be found: .../webforms/jotform/{clinic_id}?token=…
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




def _form_webhooks_indexed(form_id: str, api_key: str) -> dict[str, str]:
    """``{webhook_index: url}`` — the index is what ``DELETE …/webhooks/{id}`` takes."""
    existing = _request("GET", f"/form/{form_id}/webhooks", api_key).get("content") or []
    # content is a dict {index: url} or a list depending on Jotform version.
    if isinstance(existing, dict):
        return {str(k): v for k, v in existing.items()}
    return {str(i): u for i, u in enumerate(existing)}


def _form_webhooks(form_id: str, api_key: str) -> list[str]:
    return list(_form_webhooks_indexed(form_id, api_key).values())


def remove_webhooks(form_filter: str | None, api_key: str, apply: bool) -> None:
    """Delete every hypervisor-pointing webhook from the registered forms.

    Was the cutover step of the webhook → API-polling move (run 2026-09-04: seven
    hooks removed). Kept as a residue check — the endpoint they pointed at no
    longer exists, so a straggler would only produce 404s in the hypervisor log.
    Only URLs matching ``_WEBHOOK_CLINIC`` on the hypervisor host are touched —
    a form may carry unrelated webhooks (Zapier, email tools) that must survive.
    Inactive registry rows are included: a stale webhook on a form we stopped
    polling would otherwise keep pushing into a table nothing reconciles.
    """
    registry = [r for r in _load_registry()
                if not form_filter or r["form_id"] == form_filter]
    if not registry:
        sys.exit("No registered form matches "
                 f"{form_filter!r}." if form_filter else "No registered forms.")

    mode = "APPLY" if apply else "DRY-RUN (use --apply to write)"
    print(f"=== Jotform webhook removal — {mode} ===\n")
    removed = kept = 0
    for r in registry:
        form_id = r["form_id"]
        print(f"form {form_id}  ({r['clinic_name']} — {r['title']!r})")
        try:
            hooks = _form_webhooks_indexed(form_id, api_key)
        except Exception as exc:
            print(f"  ! webhook fetch failed: {exc}\n")
            continue
        if not hooks:
            print("  no webhooks\n")
            continue
        for idx, url in hooks.items():
            if HYPERVISOR in url and _WEBHOOK_CLINIC.search(url):
                if apply:
                    _request("DELETE", f"/form/{form_id}/webhooks/{idx}", api_key)
                    print(f"  - REMOVED  [{idx}] …/webforms/jotform/{_WEBHOOK_CLINIC.search(url).group(1)}")
                else:
                    print(f"  - WOULD REMOVE [{idx}] …/webforms/jotform/{_WEBHOOK_CLINIC.search(url).group(1)}")
                removed += 1
            else:
                kept += 1
                print(f"  ✓ kept     [{idx}] {url[:80]}  (not ours)")
        print()
    verb = "removed" if apply else "would remove"
    print(f"{verb} {removed} hypervisor webhook(s); {kept} unrelated webhook(s) left alone.")


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


def discover(api_key: str, apply: bool, recent_days: int = 90) -> None:
    """Audit registry ↔ Jotform account drift, both directions.

    Since ingestion is polled from the registry, the drift that matters is:
      - a form in the account that is COLLECTING submissions (``last_submission``
        within ``recent_days``) but is not registered — its leads reach nobody.
        Reported with its title so it can be registered in the admin UI; this
        script cannot guess the clinic, so ``--apply`` does nothing here.
      - a registered form the account no longer has (deleted/moved) — the poller
        will fail on it every run until the row is deactivated. ``--apply``
        deactivates those rows.
    One ``/user/forms`` call; no per-form requests.
    """
    registry = {r["form_id"]: r for r in _load_registry()}
    forms = {str(f["id"]): f for f in
             (_request("GET", "/user/forms?limit=1000", api_key).get("content") or [])
             if f.get("status") != "DELETED"}
    print(f"account: {len(forms)} form(s); registry: {len(registry)} row(s)\n")

    import datetime as _dt
    cutoff = _dt.datetime.now() - _dt.timedelta(days=recent_days)
    unregistered_active = []
    for fid, f in forms.items():
        if fid in registry:
            continue
        last = f.get("last_submission")
        try:
            last_dt = _dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S") if last else None
        except ValueError:
            last_dt = None
        if last_dt and last_dt >= cutoff:
            unregistered_active.append((last_dt, fid, f.get("title"), f.get("count")))

    if unregistered_active:
        print(f"Collecting submissions in the last {recent_days}d but NOT registered "
              "(register in the admin UI if it is a CORTEX clinic's form):")
        for last_dt, fid, title, count in sorted(unregistered_active, reverse=True):
            print(f"  {fid}  last {last_dt:%Y-%m-%d}  total {count:>5}  {title!r}")
    else:
        print(f"No unregistered form has collected submissions in the last {recent_days}d.")

    gone = [r for r in registry.values() if r["form_id"] not in forms]
    if gone:
        verb = "Deactivating" if apply else "WOULD deactivate (use --apply)"
        print(f"\nRegistered but no longer in the account — {verb}:")
        with session_scope() as db:
            for r in gone:
                print(f"  {r['form_id']}  {r['clinic_name']}  {r['title']!r}"
                      f"{'' if r['active'] else '  (already inactive)'}")
                if apply and r["active"]:
                    row = db.execute(select(JotformForm).where(
                        JotformForm.jotform_form_id == r["form_id"])).scalar_one()
                    row.active = False
    else:
        print("\nEvery registered form still exists in the account.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Jotform lead-form maintenance (UTM fields, location map, drift audit).")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--form", help="only this form_id")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--with-utm", action="store_true",
                      help="add the hidden UTM/click-id fields to each registered form")
    mode.add_argument("--locations", action="store_true",
                      help="reconcile each form's location options against jotform_form_locations")
    mode.add_argument("--discover", action="store_true",
                      help="audit registry ↔ account drift (unregistered collecting forms; "
                           "registered forms that are gone — --apply deactivates those)")
    mode.add_argument("--remove-webhooks", action="store_true",
                      help="residue check: delete any hypervisor-pointing webhook still on a "
                           "registered form")
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
    if args.remove_webhooks:
        remove_webhooks(args.form, api_key, args.apply)
        return
    if args.locations:
        locations(args.form, api_key, args.apply, args.link_by_name)
        return

    # --with-utm
    registry = _load_registry()
    if args.form:
        registry = [r for r in registry if r["form_id"] == args.form]
        if not registry:
            sys.exit(f"{args.form} not in the jotform_forms registry "
                     "(add it via the admin UI or POST /campaigns).")

    mode_s = "APPLY" if args.apply else "DRY-RUN (use --apply to write)"
    print(f"=== Jotform hidden UTM fields — {mode_s} ===\n")
    for r in registry:
        if not r["active"]:
            print(f"form {r['form_id']}  ({r['clinic_name']} — {r['title']!r})  SKIPPED: inactive\n")
            continue
        print(f"form {r['form_id']}  ->  clinic {r['clinic_id']}  ({r['clinic_name']} — {r['title']!r})")
        qids = ensure_utm_fields(r["form_id"], api_key, args.apply)
        if r["form_id"] in INLINED_FORMS and args.apply:
            print("  ↳ INLINED form — paste these hidden inputs into the site's <form>:")
            for name, qid in qids.items():
                print(f'       <input type="hidden" id="input_{qid}" '
                      f'name="q{qid}_{name}" data-utm="{name}" />')
        print()


if __name__ == "__main__":
    main()
