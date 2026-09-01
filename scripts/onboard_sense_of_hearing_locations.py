"""
One-off onboarding: Sense of Hearing's 14 Ontario clinics + the location map for
their shared appointment-request form.

Sense of Hearing runs every location off ONE Jotform ("Sense of Hearing -
Appointment Request Form", 262174010008038) and asks the patient which site they
want. A webhook URL carries a single clinic_id, so without a location map the
whole group's leads are attributed to whichever clinic that URL names — of the
form's first 78 submissions only 11 chose Burlington, the group's one existing
clinic, so 86% would have been wrong.

What it does (all idempotent, dry-run by default)
-------------------------------------------------
1. Creates any of the 14 clinics missing from the Sense of Hearing instance,
   from the site's own data (``websites/sense-of-hearing/src/data/site.ts``),
   via ``provision_clinic`` — same path the admin UI uses, so each gets its
   location-details and voice-agent rows too. Matching is by clinic_name within
   the instance, so a re-run creates nothing.
2. Ensures the ``jotform_forms`` registry row for the form, defaulting to
   Burlington. That default is the fallback for an answer that resolves to
   nothing; it is not where a mapped submission lands.
3. Seeds all 14 ``jotform_form_locations`` rows, each linking one dropdown
   answer VERBATIM to its clinic.

Why the map is spelled out here rather than derived: 3 of the 14 option strings
do not match the clinic name ("Limestone Hearing Care Centre (Kingston)" is
clinic *Kingston*, "Mississauga (Eglinton)" is *Mississauga Central*, "St
Catharines West" is *St. Catharines West*), so
``configure_jotform_webhooks.py --locations --link-by-name`` would leave exactly
those three unmapped. Writing all 14 keeps the group's map in one reviewable
place.

Run this BEFORE adding the form's webhook — leads arriving in between all land
on the default clinic.

Usage
-----
    cd cortex-hypervisor && source venv/bin/activate
    PYTHONPATH=. python scripts/onboard_sense_of_hearing_locations.py
    PYTHONPATH=. python scripts/onboard_sense_of_hearing_locations.py --apply

    # then, once this reports clean:
    python configure_jotform_webhooks.py --form 262174010008038 --apply
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from api.account.provisioning import provision_clinic
from api.core.db import session_scope
from api.core.orm import Clinic, Instance, JotformForm, JotformFormLocation

INSTANCE_NAME = "Sense of Hearing"
FORM_ID = "262174010008038"
FORM_TITLE = "Sense of Hearing - Appointment Request Form"
# The form's webhook URL names this clinic; it is the fallback for an answer
# that resolves to nothing, not the destination for mapped submissions.
DEFAULT_CLINIC_NAME = "Burlington"

# (clinic_name, address, phone, email) — from the live site's own location data.
# clinic_name is what CORTEX calls the site; it is NOT expected to equal the
# Jotform option label (see LOCATION_MAP).
CLINICS = [
    ("Burlington",                "11 – 1960 Appleby Line, Burlington, ON L7L 0B7",
     "905-681-8977", "burlington@senseofhearing.ca"),
    ("Etobicoke",                 "265 Wincott Drive, Suite #5A, Etobicoke, ON M9R 2R7",
     "416-241-4327", "toronto@senseofhearing.ca"),
    ("Etobicoke (Long Branch)",   "3609 Lake Shore Blvd W, #104, Etobicoke, ON M8W 1P5",
     "416-477-7744", "longbranch@senseofhearing.ca"),
    ("Guelph",                    "83 Dawson Road, #104, Guelph, ON N1H 1B1",
     "519-763-1517", "guelph@senseofhearing.ca"),
    ("Hamilton",                  "304 Victoria Avenue North, Unit #104, Hamilton, ON L8L 5G4",
     "905-308-8441", "hamilton@senseofhearing.ca"),
    ("Kingston",                  "817 Bayridge Drive, Kingston, ON K7P 1T5",
     "613-384-4400", "kingston@senseofhearing.ca"),
    ("Mississauga (Port Credit)", "272 Lakeshore Road East, Mississauga, ON L5G 1H8",
     "905-274-3032", "portcredit@senseofhearing.ca"),
    ("Oakville",                  "240 North Service Road West, Oakville, ON L6M 2R7",
     "905-339-1397", "oakville@senseofhearing.ca"),
    ("St. Catharines West",       "300 Fourth Avenue, St. Catharines, ON L2S 0E6",
     "905-684-0440", "stcatharines@senseofhearing.ca"),
    ("Waterdown",                 "245 Dundas Street East, Unit #9, Waterdown, ON L8B 0E9",
     "905-690-1633", "waterdown@senseofhearing.ca"),
    ("Welland",                   "555 Prince Charles Drive North, Suite 109, Welland, ON L3B 5X8",
     "905-788-9449", "welland@senseofhearing.ca"),
    ("Brampton",                  "17 Ray Lawson Blvd, Unit #9, Brampton, ON L6Y 5L7",
     "905-450-6018", "brampton@senseofhearing.ca"),
    ("Stoney Creek",              "68 Centennial Parkway South, Suite #102, Stoney Creek, ON L8G 2C5",
     "905-662-3130", "stoneycreek@senseofhearing.ca"),
    ("Mississauga Central",       "660 Eglinton Avenue West, Suite #5A, Mississauga, ON L5R 0B2",
     "289-652-2311", "mississauga@senseofhearing.ca"),
]

# Jotform dropdown answer (VERBATIM, exactly as it arrives in rawRequest) ->
# clinic_name. Taken from the form's four conditional location dropdowns
# (qids 19/20/21/22 — adult, 6-17, APD, 10-months-up), whose option lists
# overlap; the resolver matches on value, so one pooled map covers all four.
# Marked ⚠ where the option text and the clinic name differ — the reason this
# map is data and not a string-matching rule.
LOCATION_MAP = {
    "Burlington: 11 - 1960 Appleby Line": "Burlington",
    "Etobicoke: 5A - 265 Wincott Drive": "Etobicoke",
    "Etobicoke (Long Branch): 104 - 3609 Lakeshore Boulevard West": "Etobicoke (Long Branch)",
    "Guelph: 104: 83 Dawson Road": "Guelph",
    "Hamilton: 104-304 Victoria Avenue North, ON L8L 5G4": "Hamilton",
    # ⚠ option says the clinic's trading name, CORTEX calls it Kingston.
    "Limestone Hearing Care Centre (Kingston): 102 - 817 Bayridge Drive": "Kingston",
    "Mississauga (Port Credit): 272 Lakeshore Road East": "Mississauga (Port Credit)",
    "Oakville: 240 North Service Road West Oakville": "Oakville",
    # ⚠ no period after "St" on the form.
    "St Catharines West: 300 Fourth Avenue": "St. Catharines West",
    "Waterdown: 9 - 245 Dundas Street East": "Waterdown",
    "Welland: 109 - 555 Prince Charles Drive North": "Welland",
    "Brampton: 17 Ray Lawson Blvd, Brampton, ON L6Y 5L7": "Brampton",
    "Stoney Creek: 102 - 68 Centennial Parkway South": "Stoney Creek",
    # ⚠ option names the cross-street, CORTEX calls it Mississauga Central.
    "Mississauga (Eglinton): 5A - 660 Eglinton Avenue West": "Mississauga Central",
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    # Every option must name a clinic we are about to guarantee exists, or the
    # map would silently under-cover the form.
    known = {name for name, *_ in CLINICS}
    unknown = sorted(set(LOCATION_MAP.values()) - known)
    if unknown:
        sys.exit(f"LOCATION_MAP names clinics absent from CLINICS: {unknown}")

    print(f"=== Sense of Hearing onboarding — {'APPLY' if args.apply else 'DRY-RUN (use --apply to write)'} ===\n")

    with session_scope() as db:
        instance = db.execute(
            select(Instance).where(Instance.instance_name == INSTANCE_NAME)
        ).scalar_one_or_none()
        if instance is None:
            sys.exit(f"No instance named {INSTANCE_NAME!r}.")
        print(f"instance {instance.instance_id}  ({INSTANCE_NAME})\n")

        # ── 1. clinics ───────────────────────────────────────────────────────
        existing = {
            c.clinic_name: c
            for c in db.execute(
                select(Clinic).where(
                    Clinic.instance_id == instance.instance_id,
                    Clinic.deleted_at.is_(None),
                )
            ).scalars()
        }
        print("clinics:")
        created: dict[str, str] = {}
        for name, address, phone, email in CLINICS:
            if name in existing:
                print(f"  ✓ {name}")
                continue
            if not args.apply:
                print(f"  + WOULD CREATE {name}  ({address})")
                continue
            clinic_id, _ = provision_clinic(
                db,
                clinic_data={
                    "clinic_name": name,
                    "address": address,
                    "country": "CA",
                    "phone": phone,
                    "email": email,
                    "time_zone": "America/Toronto",
                },
                instance_id=instance.instance_id,
            )
            created[name] = clinic_id
            print(f"  + CREATED {name}  {clinic_id}")
        # provision_clinic only stages the rows; make them visible to the
        # lookups below within this same transaction.
        db.flush()

        by_name = {
            c.clinic_name: c
            for c in db.execute(
                select(Clinic).where(
                    Clinic.instance_id == instance.instance_id,
                    Clinic.deleted_at.is_(None),
                )
            ).scalars()
        }

        # ── 2. registry row ──────────────────────────────────────────────────
        print("\nregistry:")
        default_clinic = by_name.get(DEFAULT_CLINIC_NAME)
        if default_clinic is None:
            print(f"  ! {DEFAULT_CLINIC_NAME} does not exist yet — re-run with --apply")
            return
        form = db.execute(
            select(JotformForm).where(JotformForm.jotform_form_id == FORM_ID)
        ).scalar_one_or_none()
        if form is not None:
            print(f"  ✓ form {FORM_ID} -> {by_name and form.clinic_id}")
        elif not args.apply:
            print(f"  + WOULD REGISTER form {FORM_ID} -> {DEFAULT_CLINIC_NAME} (default)")
        else:
            form = JotformForm(clinic_id=default_clinic.clinic_id,
                               jotform_form_id=FORM_ID, form_title=FORM_TITLE)
            db.add(form)
            db.flush()
            print(f"  + REGISTERED form {FORM_ID} -> {DEFAULT_CLINIC_NAME} (default)")

        # ── 3. location map ──────────────────────────────────────────────────
        print("\nlocation map:")
        if form is None:
            print("  (skipped — the registry row must exist first; re-run with --apply)")
            return
        rows = {
            l.option_value: l
            for l in db.execute(
                select(JotformFormLocation)
                .where(JotformFormLocation.jotform_form_id == FORM_ID)
            ).scalars()
        }
        for option, clinic_name in LOCATION_MAP.items():
            clinic = by_name.get(clinic_name)
            target = clinic.clinic_id if clinic else None
            row = rows.get(option)
            if row is not None and row.clinic_id == target and row.active:
                print(f"  ✓ {clinic_name:<26} <- {option}")
                continue
            if not args.apply:
                verb = "WOULD MAP" if row is None else "WOULD REPOINT"
                print(f"  + {verb} {clinic_name:<20} <- {option}")
                continue
            if row is None:
                db.add(JotformFormLocation(jotform_form_id=FORM_ID, option_value=option,
                                           clinic_id=target, active=True))
                print(f"  + MAPPED {clinic_name:<24} <- {option}")
            else:
                row.clinic_id, row.active = target, True
                print(f"  + REPOINTED {clinic_name:<21} <- {option}")

        stale = sorted(set(rows) - set(LOCATION_MAP))
        for option in stale:
            print(f"  ! existing row not in this map, left alone: {option}")

    print("\nNext: python configure_jotform_webhooks.py --form "
          f"{FORM_ID} --apply    # adds the webhook")


if __name__ == "__main__":
    main()
