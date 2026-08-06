#!/usr/bin/env python3
"""
Sync + audit Invoca promo (call-tracking) numbers against Google Ads call assets.

Campaign attribution for calls rests on one invariant: *a tracking number
belongs to exactly one campaign*. Neither platform enforces it — the retired
Earlens Google Ads account had Freehold ads carrying Princeton's number — so
this script makes it an audited fact instead of an assumption:

1. SYNC   Invoca is the source of truth for number → campaign. For every
          campaign registered in Cloud SQL ``invoca_campaigns``, pull its
          promo numbers from the Invoca API and upsert them into
          ``invoca_promo_numbers`` (alembic 0021). Numbers Invoca no longer
          returns are deactivated, never deleted.
2. AUDIT  Google Ads is the source of truth for the number *shown on each ad*.
          Pull every CALL asset (campaign-, ad-group- and account-level) from
          each instance's Google Ads account and diff against the registry:

          UNREGISTERED  ad displays a number the registry doesn't know →
                        those calls can't be attributed with certainty.
          CROSS-WIRED   one number on Google Ads campaigns that map to
                        different clinics, or on two Invoca campaigns —
                        the Freehold failure mode.
          ACCOUNT-DRIFT the registry says the number serves Google Ads
                        account X (Invoca's ``adwords_account_id``) but it is
                        found on account Y.
          STALE         active registry number with media_type
                        "Google Call Extension" that no ENABLED campaign
                        displays any more.

Dry-run by default: without ``--apply`` nothing is written to Cloud SQL.

Credentials (all ADC / Secret Manager — same as running the hypervisor locally)
-------------------------------------------------------------------------------
- Invoca:     SM ``invoca-token``.
- Google Ads: SM ``google-ads-config`` (developer token, login_customer_id,
              impersonated_email) + SM ``wills_service_account_json`` for
              domain-wide delegation — the same pair the ETL uses.
- Cloud SQL:  standard ADC.

Usage
-----
    cd cortex-hypervisor && source venv/bin/activate

    python configure_promo_numbers.py                 # sync preview + audit
    python configure_promo_numbers.py --apply         # write registry changes
    python configure_promo_numbers.py --clinic <uuid> # restrict to one clinic
    python configure_promo_numbers.py --customer 9552827444 \\
        # ALSO audit a Google Ads account no instance references (e.g. the
        # retired Earlens account) — audit-only, never written to the registry.
"""
import argparse
import json
import re
import sys
import tempfile

import requests
from sqlalchemy import select

from api.core.db import session_scope
from api.core.orm import Clinic, Instance, InvocaCampaign, InvocaPromoNumber
from api.core.secrets import get_secret

INVOCA_API = "https://zoolstraltd.invoca.net/api/2022-08-01"

# Google Call Extension is the media type Invoca assigns to numbers that serve
# as Google Ads call assets; only those participate in the STALE check (a GMB
# or website-pool number is legitimately never shown on an ad).
GA_MEDIA_TYPE = "google call extension"

_CAMPAIGN_STATUS = {0: "UNSPECIFIED", 1: "UNKNOWN", 2: "ENABLED", 3: "PAUSED", 4: "REMOVED"}


def _norm(number: str | None) -> str | None:
    """Digits-only 10-digit NANP form: '(252) 576-1487' / '+12525761487' →
    '2525761487'. Returns None if it doesn't reduce to 10 digits."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


# ── Invoca side ───────────────────────────────────────────────────────────────

def _invoca_get(path: str, token: str):
    r = requests.get(f"{INVOCA_API}{path}", headers={"Authorization": token}, timeout=30)
    r.raise_for_status()
    return r.json()


def _campaign_advertiser_index(token: str) -> dict[str, str]:
    """invoca_campaign_id → advertiser_id, discovered from the network's
    advertiser list. The promo-numbers endpoint is advertiser-scoped, and
    ``instances.invoca_profile_id`` is not guaranteed to be the advertiser id,
    so discovery from the API is the reliable route."""
    index: dict[str, str] = {}
    for adv in _invoca_get("/advertisers.json", token):
        aid = str(adv.get("id") or "")
        if not aid:
            continue
        try:
            camps = _invoca_get(f"/advertisers/{aid}/advertiser_campaigns.json", token)
        except requests.HTTPError as exc:
            print(f"  ! advertiser {aid} campaigns fetch failed: {exc}", file=sys.stderr)
            continue
        for c in camps:
            index[str(c.get("id"))] = aid
    return index


def _promo_numbers(advertiser_id: str, campaign_id: str, token: str) -> list[dict]:
    rows = _invoca_get(
        f"/advertisers/{advertiser_id}/advertiser_campaigns/{campaign_id}/promo_numbers.json",
        token,
    )
    out = []
    for p in rows:
        num = _norm(p.get("promo_number"))
        if not num:
            continue
        out.append({
            "invoca_promo_id": str(p.get("id")),
            "promo_number": num,
            "description": p.get("description") or None,
            "media_type": p.get("media_type") or None,
            "promo_type": p.get("promo_type") or None,
            "adwords_account_id": p.get("adwords_account_id") or None,
        })
    return out


# ── Google Ads side ───────────────────────────────────────────────────────────

def _google_ads_client():
    """Same auth recipe as big-query-ingestion/app/google_ads/auth.py: config +
    developer token from SM, SA key for domain-wide delegation via temp file."""
    from google.ads.googleads.client import GoogleAdsClient

    config = json.loads(get_secret("google-ads-config"))
    sa_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    sa_file.write(get_secret("wills_service_account_json"))
    sa_file.close()
    config["json_key_file_path"] = sa_file.name
    config.pop("path_to_private_key_file", None)
    return GoogleAdsClient.load_from_dict(config)


def _call_assets(client, customer_id: str) -> list[dict]:
    """Every CALL asset visible on the account: campaign-, ad-group- and
    account-level links, with the campaign it serves (None for account-level)."""
    svc = client.get_service("GoogleAdsService")
    out: list[dict] = []

    queries = {
        "campaign": """
            SELECT campaign.id, campaign.name, campaign.status,
                   asset.call_asset.phone_number
            FROM campaign_asset WHERE asset.type = 'CALL'
        """,
        "ad_group": """
            SELECT campaign.id, campaign.name, campaign.status,
                   asset.call_asset.phone_number
            FROM ad_group_asset WHERE asset.type = 'CALL'
        """,
        "customer": """
            SELECT asset.call_asset.phone_number
            FROM customer_asset WHERE asset.type = 'CALL'
        """,
    }
    for level, gaql in queries.items():
        try:
            for batch in svc.search_stream(customer_id=customer_id, query=gaql):
                for row in batch.results:
                    num = _norm(row.asset.call_asset.phone_number)
                    if not num:
                        continue
                    if level == "customer":
                        # Account-level assets serve across every campaign, so
                        # they count as actively displayed (status ENABLED).
                        out.append({"level": level, "number": num, "campaign_id": None,
                                    "campaign_name": None, "status": "ENABLED"})
                    else:
                        out.append({
                            "level": level, "number": num,
                            "campaign_id": str(row.campaign.id),
                            "campaign_name": row.campaign.name,
                            "status": _CAMPAIGN_STATUS.get(int(row.campaign.status), "?"),
                        })
        except Exception as exc:  # per-level: an unreadable account shouldn't kill the audit
            print(f"  ! GA {customer_id} {level}_asset query failed: {exc}", file=sys.stderr)

    # The same (number, campaign) pair can surface at several levels (campaign
    # + ad_group links to one asset); collapse so each placement reports once.
    seen: set[tuple] = set()
    deduped = []
    for a in out:
        key = (a["number"], a["campaign_id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)
    return deduped


# ── Registry ──────────────────────────────────────────────────────────────────

def _load_registry(clinic_filter: str | None):
    """Registered Invoca campaigns (+ their stored promo numbers) joined to
    clinic/instance context."""
    with session_scope() as db:
        rows = db.execute(
            select(InvocaCampaign, Clinic.clinic_name, Clinic.clinic_id,
                   Instance.google_ads_customer_id)
            .join(Clinic, Clinic.clinic_id == InvocaCampaign.clinic_id)
            .join(Instance, Instance.instance_id == Clinic.instance_id)
            .where(Clinic.deleted_at.is_(None))
            .order_by(Clinic.clinic_name)
        ).all()
        out = []
        for camp, clinic_name, clinic_id, ga_customer in rows:
            if clinic_filter and clinic_id != clinic_filter:
                continue
            out.append({
                "row_id": camp.id,
                "invoca_campaign_id": camp.invoca_campaign_id,
                "campaign_active": bool(camp.active),
                "clinic_id": clinic_id,
                "clinic_name": clinic_name,
                "ga_customer_id": (ga_customer or "").replace("-", "") or None,
                "numbers": [{
                    "id": p.id,
                    "invoca_promo_id": p.invoca_promo_id,
                    "promo_number": p.promo_number,
                    "description": p.description,
                    "media_type": p.media_type,
                    "promo_type": p.promo_type,
                    "adwords_account_id": p.adwords_account_id,
                    "active": bool(p.active),
                } for p in camp.promo_numbers],
            })
        return out


def _sync_campaign(reg: dict, live: list[dict], apply: bool) -> None:
    """Upsert one campaign's promo numbers; deactivate rows Invoca dropped."""
    stored = {n["invoca_promo_id"]: n for n in reg["numbers"]}
    live_ids = {n["invoca_promo_id"] for n in live}

    with session_scope() as db:
        for n in live:
            cur = stored.get(n["invoca_promo_id"])
            if cur is None:
                print(f"    + {'ADD' if apply else 'WOULD ADD'} {n['promo_number']}"
                      f"  [{n['media_type']}] {n['description'] or ''}")
                if apply:
                    db.add(InvocaPromoNumber(
                        invoca_campaign_row_id=reg["row_id"], active=True,
                        **{k: n[k] for k in ("invoca_promo_id", "promo_number",
                                             "description", "media_type",
                                             "promo_type", "adwords_account_id")}))
                continue
            changed = {k: n[k] for k in ("promo_number", "description", "media_type",
                                         "promo_type", "adwords_account_id")
                       if n[k] != cur[k]} | ({} if cur["active"] else {"active": True})
            if changed:
                print(f"    ~ {'UPDATE' if apply else 'WOULD UPDATE'} {n['promo_number']}: "
                      + ", ".join(f"{k}→{v}" for k, v in changed.items()))
                if apply:
                    row = db.get(InvocaPromoNumber, cur["id"])
                    for k, v in changed.items():
                        setattr(row, k, v)
            else:
                print(f"    ✓ {n['promo_number']}  [{n['media_type']}] {n['description'] or ''}")
        for pid, cur in stored.items():
            if pid not in live_ids and cur["active"]:
                print(f"    - {'DEACTIVATE' if apply else 'WOULD DEACTIVATE'} "
                      f"{cur['promo_number']} (gone from Invoca)")
                if apply:
                    db.get(InvocaPromoNumber, cur["id"]).active = False


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--apply", action="store_true",
                    help="write registry changes (default: dry-run preview)")
    ap.add_argument("--clinic", metavar="UUID", help="restrict to one clinic_id")
    ap.add_argument("--customer", action="append", default=[], metavar="CID",
                    help="extra Google Ads customer id to audit (repeatable)")
    ap.add_argument("--skip-google-ads", action="store_true",
                    help="sync Invoca → registry only, skip the GA audit")
    args = ap.parse_args()

    registry = _load_registry(args.clinic)
    if not registry:
        sys.exit("No registered invoca_campaigns rows match (is the registry empty?).")

    invoca_token = get_secret("invoca-token")

    # ── 1. sync: Invoca → registry ────────────────────────────────────────────
    print(f"── Sync Invoca promo numbers → registry "
          f"({'apply' if args.apply else 'dry-run'}) ──")
    adv_index = _campaign_advertiser_index(invoca_token)
    live_by_campaign: dict[str, list[dict]] = {}
    for reg in registry:
        cid = reg["invoca_campaign_id"]
        print(f"  {reg['clinic_name']}  (invoca campaign {cid})")
        aid = adv_index.get(cid)
        if aid is None:
            print("    ! campaign not found on any Invoca advertiser (archived or wrong id)")
            continue
        live = _promo_numbers(aid, cid, invoca_token)
        live_by_campaign[cid] = live
        _sync_campaign(reg, live, args.apply)

    # Effective registry view for the audit = stored rows overlaid with what the
    # sync just saw (so the audit is accurate even on a dry run).
    number_owner: dict[str, dict] = {}
    cross_wired: list[str] = []
    for reg in registry:
        for n in live_by_campaign.get(reg["invoca_campaign_id"], reg["numbers"]):
            prev = number_owner.get(n["promo_number"])
            if prev and prev["invoca_campaign_id"] != reg["invoca_campaign_id"]:
                cross_wired.append(
                    f"{n['promo_number']} on Invoca campaigns "
                    f"{prev['invoca_campaign_id']} ({prev['clinic_name']}) AND "
                    f"{reg['invoca_campaign_id']} ({reg['clinic_name']})")
                continue
            number_owner[n["promo_number"]] = {**n,
                                               "invoca_campaign_id": reg["invoca_campaign_id"],
                                               "clinic_name": reg["clinic_name"],
                                               "ga_customer_id": reg["ga_customer_id"]}

    if args.skip_google_ads:
        _summary(cross_wired, [], [], [])
        return

    # ── 2. audit: Google Ads call assets ↔ registry ───────────────────────────
    print("\n── Audit Google Ads call assets ↔ registry ──")
    customers = sorted({r["ga_customer_id"] for r in registry if r["ga_customer_id"]}
                       | {c.replace("-", "") for c in args.customer})
    client = _google_ads_client()

    unregistered: list[str] = []
    account_drift: list[str] = []
    seen_on_enabled: set[str] = set()
    for cust in customers:
        print(f"  account {cust}")
        assets = _call_assets(client, cust)
        if not assets:
            print("    (no CALL assets)")
        ga_campaigns_by_number: dict[str, set[str]] = {}
        for a in assets:
            owner = number_owner.get(a["number"])
            where = (f"campaign {a['campaign_id']} {a['campaign_name']!r} [{a['status']}]"
                     if a["campaign_id"] else "account-level")
            if owner is None:
                unregistered.append(f"{a['number']} shown on {cust} {where}")
                print(f"    ✗ {a['number']} {where} — UNREGISTERED")
                continue
            print(f"    ✓ {a['number']} {where} → {owner['clinic_name']} "
                  f"(invoca {owner['invoca_campaign_id']})")
            if a["status"] == "ENABLED":
                seen_on_enabled.add(a["number"])
            if a["campaign_id"]:
                ga_campaigns_by_number.setdefault(a["number"], set()).add(a["campaign_id"])
            adw = (owner["adwords_account_id"] or "").replace("-", "")
            if adw and adw != cust:
                account_drift.append(
                    f"{a['number']}: Invoca says account {adw}, found on {cust}")
        for num, camps in ga_campaigns_by_number.items():
            if len(camps) > 1:
                cross_wired.append(
                    f"{num} on {len(camps)} Google Ads campaigns in {cust}: "
                    + ", ".join(sorted(camps)))

    stale = [
        f"{num} ({o['clinic_name']}, {o['description'] or 'no description'})"
        for num, o in number_owner.items()
        if (o["media_type"] or "").lower() == GA_MEDIA_TYPE
        and num not in seen_on_enabled
    ]
    _summary(cross_wired, unregistered, account_drift, stale)


def _summary(cross_wired, unregistered, account_drift, stale) -> None:
    print("\n── Summary ──")
    findings = [("CROSS-WIRED", cross_wired), ("UNREGISTERED", unregistered),
                ("ACCOUNT-DRIFT", account_drift), ("STALE", stale)]
    clean = True
    for label, items in findings:
        for item in items:
            clean = False
            print(f"  {label:14} {item}")
    if clean:
        print("  ✓ no drift — every ad-displayed number attributes to exactly one campaign")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
