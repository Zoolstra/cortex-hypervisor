"""One-off: build the corrected Virsono x Clementine call-data workbook.

Non-destructive — writes a NEW file next to the original. Matthew-handled calls,
Dec 4 2025 – Jul 8 2026, corrected categorization (fixes #1 + #2), a Claimed
column, and a Summary sheet (universe accounting, methods, delta, findings, root
cause).
"""
import json
from collections import Counter
from sqlalchemy import select
from google.cloud import storage

from api.core.db import get_session
from api.core.orm import Clinic, ClinicLocationDetails, InvocaCampaign, GoogleAdsCampaign
from intelligence_report import queries as q

INST = "9e59f7fa-dcf5-4308-bdff-7c1b56ae7a1a"
OUT = "/mnt/c/Users/willi/OneDrive/Documents/Virsono x Clementine Call Data (corrected).xlsx"
w = q.Window("2025-12-04", "2026-07-08")

db = next(get_session())
clinics = db.execute(
    select(Clinic.clinic_id, Clinic.clinic_name, ClinicLocationDetails.time_zone)
    .outerjoin(ClinicLocationDetails, ClinicLocationDetails.clinic_id == Clinic.clinic_id)
    .where(Clinic.instance_id == INST, Clinic.deleted_at.is_(None))).all()

allr, roi_rows = [], []
for cid, name, tz in clinics:
    iv = [str(r[0]) for r in db.execute(select(InvocaCampaign.invoca_campaign_id)
          .where(InvocaCampaign.clinic_id == cid, InvocaCampaign.active.is_(True))).all()]
    ga = [str(r[0]) for r in db.execute(select(GoogleAdsCampaign.google_ads_campaign_id)
          .where(GoogleAdsCampaign.clinic_id == cid, GoogleAdsCampaign.active.is_(True))).all()]
    if iv:
        rws = q.line_item_calls(cid, iv, w, limit=50000, clinic_tz=tz)
        for rr in rws:
            rr["clinic_name"] = name
        allr += rws
        if ga:
            roi_rows += q.google_ads_roi(cid, ga, iv, window=w)

m = [r for r in allr if r["handled_by_matthew"]]
CONNECTED = ("booked", "existing_patient", "qualified_no_conversion", "other")

# ── transcripts (connected rows only) from GCS ───────────────────────────────
sc = storage.Client(project="project-demo-2-482101")
bkt = sc.bucket("transcripts-json")
tx = {}
for r in m:
    if r["outcome"] in CONNECTED and r["has_transcript"]:
        try:
            raw = json.loads(bkt.blob(f"{r['call_id']}.json").download_as_text())
            turns = [f"{k}: {v}" for seg in raw if isinstance(seg, dict) for k, v in seg.items() if v]
            tx[r["call_id"]] = "\n".join(turns)
        except Exception:
            tx[r["call_id"]] = ""

def appt(r, i):
    a = r["appointments"][i] if i < len(r["appointments"]) else None
    if not a:
        return ""
    return " · ".join(str(x) for x in [a.get("start_time"), a.get("event_type"), a.get("status"),
                                       a.get("practitioner"), a.get("location_name")] if x)

# ── numbers ──────────────────────────────────────────────────────────────────
c = Counter(r["outcome"] for r in m)
connected = sum(c[k] for k in CONNECTED)
no_utm_conn = sum(1 for r in m if r["outcome"] in CONNECTED and
                  ((not r["channel"]) or r["channel"] == "No UTM parameter data"))
paid_leaked = sum(1 for r in m if r["channel"] == "Paid Search" and r["outcome"] == "qualified_no_conversion")
paid_spend = sum(x["spend"] for x in roi_rows)
paid_rev = sum(x["revenue"] for x in roi_rows)
paid_booked = sum(x["booked"] for x in roi_rows)
cap = (c["booked"] / connected * 100) if connected else 0

print("Matthew=%d connected=%d booked=%d existing=%d qualified=%d other=%d" % (
    len(m), connected, c["booked"], c["existing_patient"], c["qualified_no_conversion"], c["other"]))
print("no_utm_conn=%d paid_leaked=%d spend=%.0f rev=%.0f booked=%d" % (
    no_utm_conn, paid_leaked, paid_spend, paid_rev, paid_booked))

# ── write workbook ───────────────────────────────────────────────────────────
import openpyxl
from openpyxl.styles import Font, Alignment
wb = openpyxl.Workbook()
BOLD = Font(bold=True)
H = Font(bold=True, color="FFFFFF")
from openpyxl.styles import PatternFill
HFILL = PatternFill("solid", fgColor="0A1628")

sm = wb.active
sm.title = "Summary"
def put(row, a, b="", bold=False):
    sm.cell(row, 1, a).font = BOLD if bold else Font()
    if b != "":
        sm.cell(row, 2, b)
r = 1
put(r, "Virsono × Clementine — Call Data (corrected)", bold=True); r += 2
put(r, "Window", "Dec 4 2025 – Jul 8 2026"); r += 1
put(r, "Scope", "All Virsono clinics · Matthew-handled calls"); r += 2
put(r, "UNIVERSE ACCOUNTING", bold=True); r += 1
put(r, "Matthew-handled calls", len(m)); r += 1
put(r, "  − spam / solicitor", -c["spam"]); r += 1
put(r, "  − no transcript", -c["no_transcript"]); r += 1
put(r, "  − no conversation", -c["no_conversation"]); r += 1
put(r, "  − wrong number", -c["wrong_number"]); r += 1
put(r, "= Analyzed (connected)", connected, bold=True); r += 1
put(r, "     Booked (new-patient)", c["booked"]); r += 1
put(r, "     Existing-patient activity", c["existing_patient"]); r += 1
put(r, "     Qualified – no conversion", c["qualified_no_conversion"]); r += 1
put(r, "     Other", c["other"]); r += 2
put(r, "New-patient booking capture", f"{cap:.1f}% ({c['booked']}/{connected})"); r += 2

put(r, "DELTA vs v1", bold=True); r += 1
put(r, "1) Booked redefined to NEW-patient acquisition only; existing-patient reschedules/"
       "pickups/service split into a new 'Existing patient' category. Booked fell (was inflated "
       "by existing-patient activity). Why: existing-patient linkage counted as new bookings (root cause #1)."); r += 1
put(r, "2) Four coincidental wrong-number/no-conversation phone matches removed from Booked; "
       "analyzed 405 → %d. Matches on wrong-number/no-conversation calls were reviewed and reclassified." % connected); r += 2

put(r, "METHODS", bold=True); r += 1
put(r, "(a) %d of %d analyzed calls have no UTM data — channel attribution has a stated blind spot." % (no_utm_conn, connected)); r += 1
put(r, "(b) The 3-day same-phone matching rule is conservative; bookings made under a different number read as misses."); r += 1
put(r, "(c) Booking = a genuine, connected, NEW-patient call reconciled to a PMS appointment created within 3 days (closest call owns it)."); r += 2

put(r, "ROOT CAUSE #1 — existing-patient linkage", bold=True); r += 1
put(r, "Reconciliation matched any appointment created in the window, incl. existing-patient reschedules/"
       "pickups/service. The eleven former led_to_booking rows (e.g. Timothy Diehl) said 'existing patient, "
       "no new appointment' in the reasoning while flagged as booking-linked. Now labeled Existing patient; "
       "led_to_booking no longer fires on existing patients (flag and reasoning agree)."); r += 2

put(r, "FINDINGS", bold=True); r += 1
roas = (paid_rev / paid_spend) if paid_spend else 0
put(r, "(a) Paid search:", "spend $%.0f · attributed revenue $%.0f · ROAS %.1fx · %d new-patient bookings · %d motivated paid callers leaked (qualified, no booking)." % (paid_spend, paid_rev, roas, paid_booked, paid_leaked)); r += 1
put(r, "(b) RingCentral:", "clinic-side lines currently have no recording or transcription active, so the transfer seam is unmeasured; activation is configuration on existing infrastructure."); r += 1
sm.column_dimensions["A"].width = 48
sm.column_dimensions["B"].width = 90

# ── Definitions ──────────────────────────────────────────────────────────────
dfn = wb.create_sheet("Call Categorization Definitions")
defs = [
    ("Booked", "A genuine, connected, NEW-patient call reconciled to a PMS appointment created within 3 days of the call (closest call owns the booking)."),
    ("Existing patient", "A connected call from an existing patient — reschedules, device pickups, service. NOT a new booking."),
    ("Qualified – no conversion", "A connected NEW-patient caller looking to book who ended without an appointment (recoverable leak)."),
    ("Other", "A connected NEW-patient call with no booking intent and no booking."),
    ("Excluded from analyzed:", ""),
    ("  Spam / solicitor", "Robocall, autodial, B2B sales — no patient-care intent."),
    ("  Wrong number", "Caller dialed in error."),
    ("  No conversation", "Line connected but no real dialogue (recording, silence, immediate drop)."),
    ("  No transcript", "No usable transcript captured (never analyzed)."),
]
dfn.cell(1, 1, "Call Categorization").font = BOLD
for i, (k, v) in enumerate(defs, start=3):
    dfn.cell(i, 1, k).font = BOLD
    dfn.cell(i, 2, v)
dfn.column_dimensions["A"].width = 28
dfn.column_dimensions["B"].width = 100

# ── category sheets ──────────────────────────────────────────────────────────
HEADERS = ["clinic", "datetime", "caller_name", "caller_phone", "location", "channel",
           "campaign", "duration_sec", "outcome", "claimed", "reasoning", "has_transcript",
           "handled_by_matthew", "touchpoints", "led_to_booking", "patient_matched",
           "appointment_count", "appointment_1", "transcript",
           "appointment_2", "appointment_3", "appointment_4", "appointment_5"]
SHEETS = [("Booked", ("booked", "led_to_booking")),
          ("Existing Patient", ("existing_patient",)),
          ("Other", ("other",)),
          ("Qualifed - No Conversion", ("qualified_no_conversion",))]
yn = lambda b: "yes" if b else "no"
for title, outs in SHEETS:
    ws = wb.create_sheet(title)
    for j, h in enumerate(HEADERS, 1):
        cell = ws.cell(1, j, h); cell.font = H; cell.fill = HFILL
    ri = 2
    for r_ in sorted([x for x in m if x["outcome"] in outs], key=lambda x: (x["clinic_name"] or "", x["datetime"] or "")):
        vals = [r_["clinic_name"], r_["datetime"], r_["caller_name"], r_["caller_phone"], r_["location"],
                r_["channel"], r_["campaign"], r_["duration_sec"], r_["outcome"], r_["matthew_outcome"],
                r_["reasoning"], yn(r_["has_transcript"]), yn(r_["handled_by_matthew"]), r_["touchpoints"],
                yn(r_["led_to_booking"]), yn(r_["patient_matched"]), r_["appointment_count"],
                appt(r_, 0), tx.get(r_["call_id"], ""), appt(r_, 1), appt(r_, 2), appt(r_, 3), appt(r_, 4)]
        for j, v in enumerate(vals, 1):
            ws.cell(ri, j, v)
        ri += 1
    ws.freeze_panes = "A2"
    print(f"  sheet {title}: {ri-2} rows")

wb.save(OUT)
print("WROTE", OUT)
