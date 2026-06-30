"""
Scored "Active Leads" recovery inbox.

Unifies every still-open lead a clinic could win back — qualified callers who
didn't book, non-spam missed calls during business hours, and web-form
submissions with no booking since — into one list ranked by **expected
recoverable revenue** = P(recover) × expected value.

Sourcing/enrichment SQL lives in ``queries.py``; this module orchestrates,
de-dupes across channels, auto-resolves anyone who has since booked/purchased,
and scores. Value is tiered by PMS match + lifecycle (returning patients use
their own history; warranty/upgrade/tested-not-sold callers are valued at the
device level); no LLM call. Everything is fail-safe — a failing source/section
degrades to empty rather than raising.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
from typing import Any

from intelligence_report import clinic_hours
from intelligence_report import queries as q
from intelligence_report.queries import Window

log = logging.getLogger(__name__)

# ── Scoring constants (transparent + tunable) ────────────────────────────────
_RECENCY_TAU_DAYS = 14.0          # exponential decay constant for speed-to-lead
_RECENCY_FLOOR = 0.05
_LIFECYCLE_VALUE_MULT = 2.0       # device-level value for warranty/upgrade/tested
_ENGAGEMENT = {
    "qualified_call": 1.0,
    "form_new": 0.95,
    "form_returning": 0.80,
    "form": 0.85,
    "missed_call": 0.60,
}
_REACH = {"both": 1.0, "phone": 0.9, "email": 0.75, "none": 0.4}


def _today() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def _norm_phone(raw: str | None) -> str:
    digits = "".join(c for c in str(raw or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""


def _norm_email(raw: str | None) -> str:
    e = str(raw or "").strip().lower()
    return e if "@" in e else ""


def _parse_local(ts: str | None) -> tuple[_dt.date | None, float | None]:
    """(date, hour-of-day float) from a loose local timestamp string."""
    if not ts:
        return None, None
    s = str(ts).strip().replace("T", " ").split("+")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = _dt.datetime.strptime(s[: len(fmt) + 2], fmt)
            return d.date(), d.hour + d.minute / 60.0
        except ValueError:
            continue
    try:
        return _dt.date.fromisoformat(s[:10]), None
    except ValueError:
        return None, None


def _lead_key(phone_norm: str, email_norm: str, fallback: str) -> str:
    return f"p:{phone_norm}" if phone_norm else (f"e:{email_norm}" if email_norm else f"x:{fallback}")


def build_active_leads(
    *,
    clinic_id: str,
    clinic_name: str,
    invoca_campaign_ids: list[str],
    window: Window,
    location_hours: dict | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    w = window
    today = _today()

    # ── 1. Gather raw leads from each source (fail-safe) ─────────────────────
    raw: list[dict] = []

    def _safe(fn, label):
        try:
            return fn()
        except Exception as exc:
            log.warning("active_leads source %s failed clinic=%s: %s", label, clinic_id, exc)
            return []

    for c in _safe(lambda: q.qualified_lead_no_conv_detail(clinic_id, invoca_campaign_ids, window=w),
                   "qualified"):
        d, _h = _parse_local(c.get("start_time_local"))
        raw.append({
            "source": "call", "subtype": "qualified_call",
            "engagement": "qualified_call",
            "name": "", "phone_norm": _norm_phone(c.get("calling_phone_number")),
            "email_norm": "", "phone_raw": c.get("calling_phone_number"), "email_raw": None,
            "date": d, "utm_source": None, "utm_medium": c.get("utm_medium"),
            "landing_page": None, "customer_type": None,
            "detail": c.get("reasoning") or "",
        })

    # Missed (non-spam) calls that landed during business hours — only when we
    # can prove they were in-hours (needs parsed clinic hours), else skipped to
    # avoid flooding the inbox with after-hours / unknowable misses.
    if location_hours:
        for c in _safe(lambda: q._stage2_outcome_detail(
                clinic_id, invoca_campaign_ids, w.span_days, "No Conversation", window=w),
                "missed"):
            d, hod = _parse_local(c.get("start_time_local"))
            if d is None or hod is None:
                continue
            if not clinic_hours.is_open_at(location_hours, d.weekday(), hod):
                continue
            raw.append({
                "source": "call", "subtype": "missed_call", "engagement": "missed_call",
                "name": "", "phone_norm": _norm_phone(c.get("calling_phone_number")),
                "email_norm": "", "phone_raw": c.get("calling_phone_number"), "email_raw": None,
                "date": d, "utm_source": None, "utm_medium": c.get("utm_medium"),
                "landing_page": None, "customer_type": None,
                "detail": "Missed call during business hours",
            })

    for f in _safe(lambda: q.open_form_leads(clinic_id, window=w), "forms"):
        d, _h = _parse_local(f.get("submitted_at"))
        ctype = (f.get("customer_type") or "").strip().lower()
        is_new = "new" in ctype or ctype == ""
        name = f"{f.get('first_name','')} {f.get('last_name','')}".strip()
        raw.append({
            "source": "form", "subtype": "form",
            "engagement": "form_new" if is_new else "form_returning",
            "name": name, "phone_norm": _norm_phone(f.get("phone_number")),
            "email_norm": _norm_email(f.get("email")),
            "phone_raw": f.get("phone_number"), "email_raw": f.get("email"),
            "date": d, "utm_source": f.get("utm_source"), "utm_medium": f.get("utm_medium"),
            "landing_page": f.get("landing_page"), "customer_type": f.get("customer_type"),
            "detail": f.get("message") or "",
        })

    if not raw:
        return _empty_payload(clinic_id, clinic_name, w)

    # ── 2. De-dupe across channels (a caller who also submitted a form, etc.) ─
    merged: dict[str, dict] = {}
    for i, lead in enumerate(raw):
        key = _lead_key(lead["phone_norm"], lead["email_norm"], str(i))
        cur = merged.get(key)
        if cur is None:
            merged[key] = {**lead, "key": key, "touches": 1, "sources": {lead["subtype"]}}
            continue
        cur["touches"] += 1
        cur["sources"].add(lead["subtype"])
        # Keep the strongest engagement and the most recent date; fill blanks.
        if _ENGAGEMENT.get(lead["engagement"], 0) > _ENGAGEMENT.get(cur["engagement"], 0):
            cur["engagement"] = lead["engagement"]
        if lead["date"] and (cur["date"] is None or lead["date"] > cur["date"]):
            cur["date"] = lead["date"]
        cur["name"] = cur["name"] or lead["name"]
        cur["email_norm"] = cur["email_norm"] or lead["email_norm"]
        cur["phone_norm"] = cur["phone_norm"] or lead["phone_norm"]
        cur["phone_raw"] = cur["phone_raw"] or lead["phone_raw"]
        cur["email_raw"] = cur["email_raw"] or lead["email_raw"]
        cur["detail"] = cur["detail"] or lead["detail"]

    leads = list(merged.values())

    # ── 3. Enrichment: PMS match, value inputs, opt-out, lifecycle ───────────
    phones = sorted({l["phone_norm"] for l in leads if l["phone_norm"]})
    emails = sorted({l["email_norm"] for l in leads if l["email_norm"]})
    enrich = q.lead_pms_enrichment(clinic_id, phones, emails)
    lifecycle = q.lifecycle_client_ids(clinic_id)
    rev = q.invoice_revenue(clinic_id, window=w)
    base = (rev["revenue"] / rev["invoice_count"]) if rev.get("invoice_count") else 0.0
    value_known = base > 0

    by_phone, by_email, clients = enrich["by_phone"], enrich["by_email"], enrich["clients"]
    lifecycle_ids = lifecycle["warranty"] | lifecycle["upgrade"] | lifecycle["tested_not_sold"]

    scored: list[dict] = []
    for l in leads:
        cid = by_phone.get(l["phone_norm"]) or by_email.get(l["email_norm"])
        c = clients.get(cid) if cid else None

        # Auto-resolve: matched patient who has booked / purchased since the touch.
        if c and l["date"]:
            ld = l["date"].isoformat()
            if (c["max_appt_date"] and c["max_appt_date"] >= ld) or \
               (c["max_invoice_date"] and c["max_invoice_date"] >= ld):
                continue

        returning = bool(c and c["invoice_count"] > 0)
        is_lifecycle = bool(cid and cid in lifecycle_ids)
        opt_both = bool(c and c["do_not_contact"] and c["do_not_text"])
        opt_some = bool(c and (c["do_not_contact"] or c["do_not_text"]))

        # Value (tiered).
        if is_lifecycle:
            value = base * _LIFECYCLE_VALUE_MULT
        elif returning:
            value = max(base, c["avg_invoice"])
        else:
            value = base

        # Recoverability.
        age = (today - l["date"]).days if l["date"] else 60
        recency = max(_RECENCY_FLOOR, math.exp(-max(age, 0) / _RECENCY_TAU_DAYS))
        has_p, has_e = bool(l["phone_norm"]), bool(l["email_norm"])
        reach = _REACH["both"] if (has_p and has_e) else _REACH["phone"] if has_p \
            else _REACH["email"] if has_e else _REACH["none"]
        eng = _ENGAGEMENT.get(l["engagement"], 0.8)
        repeat = min(1.3, 1.0 + 0.12 * (l["touches"] - 1))
        optf = 0.25 if opt_both else 0.7 if opt_some else 1.0
        p = max(0.0, min(1.0, recency * reach * eng * optf * repeat))

        exp_rev = (p * value) if value_known else None
        band = "hot" if p >= 0.5 else "warm" if p >= 0.25 else "cooling"

        name = l["name"]
        if not name and c:
            name = f"{c['given_name']} {c['surname']}".strip()

        scored.append({
            "key": l["key"],
            "source": l["source"],
            "subtype": l["subtype"],
            "sources": sorted(l["sources"]),
            "touches": l["touches"],
            "name": name or "Unknown caller",
            "phone": l["phone_raw"],
            "email": l["email_raw"],
            "date": l["date"].isoformat() if l["date"] else None,
            "age_days": age,
            "utm_source": l["utm_source"],
            "utm_medium": l["utm_medium"],
            "landing_page": l["landing_page"],
            "customer_type": l["customer_type"],
            "detail": (l["detail"] or "")[:400],
            "matched": bool(cid),
            "client_id": cid,
            "returning": returning,
            "lifecycle": is_lifecycle,
            "opt_out": opt_both,
            "opt_out_partial": opt_some and not opt_both,
            "value": round(value, 2) if value_known else None,
            "recoverability": {
                "p": round(p, 3), "recency": round(recency, 3), "reach": reach,
                "engagement": round(eng, 2), "repeat": round(repeat, 2),
            },
            "expected_recoverable_revenue": round(exp_rev, 2) if exp_rev is not None else None,
            "band": band,
            "suggested_action": _suggest(l["subtype"], l["engagement"], returning, opt_both),
        })

    # ── 4. Sort by expected recoverable revenue (value-known first), then P ───
    scored.sort(key=lambda x: (
        x["expected_recoverable_revenue"] is not None,
        x["expected_recoverable_revenue"] or 0.0,
        x["recoverability"]["p"],
    ), reverse=True)

    total = sum(x["expected_recoverable_revenue"] or 0.0 for x in scored)
    source_counts = {"qualified_call": 0, "missed_call": 0, "form": 0}
    for x in scored:
        source_counts[x["subtype"]] = source_counts.get(x["subtype"], 0) + 1

    return {
        "clinic_id": clinic_id,
        "clinic_name": clinic_name,
        "window": {"start": w.start_date, "end": (w.end_excl - _dt.timedelta(days=1)).isoformat()},
        "value_known": value_known,
        "base_value": round(base, 2),
        "total_recoverable": round(total, 2),
        "lead_count": len(scored),
        "source_counts": source_counts,
        "leads": scored[: max(1, int(limit))],
    }


def _suggest(subtype: str, engagement: str, returning: bool, opt_both: bool) -> str:
    if opt_both:
        return "Outreach restricted — caller has opted out of contact."
    if subtype == "qualified_call":
        return "Call back — was qualified on the call but didn't book."
    if subtype == "missed_call":
        return "Return the missed call — landed during business hours."
    if engagement == "form_new":
        return "Follow up on the new-patient web enquiry."
    return "Follow up on the web enquiry." + (" Existing patient." if returning else "")


def _empty_payload(clinic_id, clinic_name, w) -> dict[str, Any]:
    return {
        "clinic_id": clinic_id, "clinic_name": clinic_name,
        "window": {"start": w.start_date, "end": (w.end_excl - _dt.timedelta(days=1)).isoformat()},
        "value_known": False, "base_value": 0.0, "total_recoverable": 0.0,
        "lead_count": 0, "source_counts": {"qualified_call": 0, "missed_call": 0, "form": 0},
        "leads": [],
    }
