"""
Is this business actually set up, and if not, what is stopping data flowing?

Every settings field can be individually valid while the business as a whole
ingests nothing, and until now that only surfaced days later as an empty
dashboard. Onboarding one four-location client produced five separate failures of
exactly this shape — locations left unmapped, ETL left off, a rollup gated on a
flag nobody flipped, a sync never re-run after the mapping changed, and an
account's config wiped by a blank save. Each was discoverable from data the
system already held; none was on screen at the moment it mattered.

So this endpoint composes the checks that must pass, in the order they must pass,
and says what breaks when each one doesn't. It is deliberately a *reader*: every
rule here already exists somewhere that enforces it (``core.grouping`` for the
rollup, the PMS validators for the map, the sync's unassigned counting for
attribution). Restating a rule would create a second definition of healthy that
could drift from the one the pipeline applies.

Statuses:
    ok       nothing to do
    warn     works, but something downstream will be wrong or invisible
    blocked  nothing flows until this is fixed
    unknown  could not be determined (BigQuery unreachable) — never a silent pass
    skipped  does not apply to this business
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import PROJECT, bq_client, require_read_access, verify_token
from api.core.db import get_session
from api.core.grouping import clinic_count, is_multi_location
from api.core.orm import (
    CATCH_ALL_LOCATION_KEY, Clinic, GoogleAdsCampaign, Instance,
    InstancePmsConfig, InvocaCampaign, PmsClinicLocation,
)
from api.core.secrets import get_secret

log = logging.getLogger(__name__)

router = APIRouter()

# The feed credentials. `api_key` is the live API (voice agent, booking); the
# other three are the S3 data feed, and without them nothing is ingested at all.
_FEED_SECRETS = ("aws_access_key_id", "aws_secret_access_key", "zip_password")

# Required for the account to be configured at all. `aws_url` is deliberately not
# here: Blueprint exposes a live API and an S3 data feed, and a client can be on
# the API alone (Prairie is). Reporting that as broken would send someone hunting
# for a feed URL that was never meant to exist.
_VENDOR_REQUIRED_CONFIG = {
    "blueprint": ("api_url",),
    "counselear": ("counselear_location_code", "counselear_sftp_username"),
}


def _check(key, title, status, detail, *, consequence=None, fix=None, items=None,
           clinic_ids=None):
    return {
        "key": key, "title": title, "status": status, "detail": detail,
        # What goes wrong if this is left — the part an operator needs in order to
        # judge whether to act now, and the part an empty dashboard never says.
        "consequence": consequence,
        "fix": fix,
        "items": items or [],
        # Which clinics this check is about, by id. `items` carries names for
        # people; a clinic-scoped consumer — the dashboard deciding whether to
        # explain a zero — needs to match on something that survives a rename.
        # None means "the whole business", which every clinic inherits.
        "clinic_ids": clinic_ids,
    }


def _secret_present(instance_id: str, clinic_ids: list[str],
                    pms_type: str, key: str) -> bool:
    """Mirror the readers' resolution order: instance scope, then a mapped clinic.

    The per-clinic fallback exists because migration 0030 moved config to the
    account but could not move Secret Manager entries. Reporting "missing" for a
    credential the ETL will happily find would send someone re-keying a working
    account.
    """
    for name in ([f"instance_{instance_id}_{pms_type}_{key}"]
                 + [f"clinic_{cid}_{pms_type}_{key}" for cid in clinic_ids]):
        try:
            if get_secret(name):
                return True
        except Exception:
            continue
    return False


def _feed_state(clinic_ids: list[str]) -> dict:
    """Latest snapshot date and the locations the feed carries, from BigQuery.

    Best-effort by design: BigQuery being unreachable must not take down a
    settings page. Callers turn a None into `unknown` rather than a pass.
    """
    if not clinic_ids:
        return {"snapshot": None, "locations": None}
    ids = ",".join(f"'{c}'" for c in clinic_ids)
    out: dict = {"snapshot": None, "locations": None}
    try:
        rows = list(bq_client.query(
            f"SELECT MAX(_snapshot_date) AS snap "
            f"FROM `{PROJECT}.Blueprint_PHI.Appointments` "
            f"WHERE _clinic_id IN ({ids})").result())
        out["snapshot"] = rows[0].snap if rows else None
    except Exception:
        log.warning("readiness: snapshot lookup failed", exc_info=True)
    try:
        rows = list(bq_client.query(
            f"SELECT DISTINCT location_id, location_name "
            f"FROM `{PROJECT}.Blueprint_PHI.Location` "
            f"WHERE _clinic_id IN ({ids}) AND location_id IS NOT NULL").result())
        out["locations"] = {str(r.location_id): r.location_name for r in rows}
    except Exception:
        log.warning("readiness: location dimension lookup failed", exc_info=True)
    return out


@router.get("/instances/{instance_id}/readiness")
def get_instance_readiness(
    instance_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Ordered setup checks for a business, each with its consequence and a fix."""
    instance = db.get(Instance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    require_read_access(instance_id, caller)

    clinics = list(db.scalars(select(Clinic).where(
        Clinic.instance_id == instance_id, Clinic.deleted_at.is_(None))))
    by_id = {c.clinic_id: c for c in clinics}
    settings_base = f"/settings/instance/{instance_id}"

    # Which vendor this business is on. Clinics carry it; the config row is the
    # fallback for a business configured but with no clinic pointed at it yet.
    pms_types = {c.pms_type for c in clinics if c.pms_type in _VENDOR_REQUIRED_CONFIG}
    configs = {c.pms_type: c for c in db.scalars(select(InstancePmsConfig).where(
        InstancePmsConfig.instance_id == instance_id))}
    pms_types |= {t for t in configs if t in _VENDOR_REQUIRED_CONFIG}
    pms_type = sorted(pms_types)[0] if pms_types else None

    checks: list[dict] = []

    # ── 1. Clinics exist ──────────────────────────────────────────────────────
    if not clinics:
        checks.append(_check(
            "clinics", "Locations", "blocked",
            "This business has no clinics.",
            consequence="Nothing can be configured or ingested until it has at "
                        "least one location.",
            fix=f"{settings_base}/pms"))
    else:
        checks.append(_check(
            "clinics", "Locations", "ok",
            f"{len(clinics)} location(s).",
            fix=f"{settings_base}"))

    # ── 2. PMS account configured ─────────────────────────────────────────────
    if pms_type is None:
        checks.append(_check(
            "pms_account", "PMS account", "skipped",
            "No practice-management system connected.",
            consequence="Revenue and bookings cannot be measured — there is no "
                        "second source for either."))
    else:
        cfg = configs.get(pms_type)
        required = _VENDOR_REQUIRED_CONFIG[pms_type]
        missing = [f for f in required if not (cfg and getattr(cfg, f, None))]
        if missing:
            checks.append(_check(
                "pms_account", "PMS account", "blocked",
                f"Missing {', '.join(missing)}.",
                consequence="Nothing is ingested from the PMS.",
                fix=f"{settings_base}/pms", items=missing))
        else:
            checks.append(_check(
                "pms_account", "PMS account", "ok",
                f"{pms_type} account configured.", fix=f"{settings_base}/pms"))

    locations = list(db.scalars(select(PmsClinicLocation).where(
        PmsClinicLocation.instance_id == instance_id,
        PmsClinicLocation.pms_type == pms_type)) ) if pms_type else []
    active_locs = [l for l in locations if l.active]
    mapped_clinic_ids = {l.clinic_id for l in active_locs if l.clinic_id}
    # Does this account actually have a data feed? Everything below about
    # locations, credentials and sync freshness is about the feed, and none of it
    # applies to an API-only account.
    has_feed = bool(pms_type == "blueprint" and configs.get("blueprint")
                    and configs["blueprint"].aws_url)
    feed = _feed_state([c.clinic_id for c in clinics]) if has_feed \
        else {"snapshot": None, "locations": None}

    # ── 3. Credentials ────────────────────────────────────────────────────────
    if pms_type == "blueprint" and not has_feed:
        checks.append(_check(
            "pms_secrets", "Data feed", "skipped",
            "API only — no S3 feed configured for this account.",
            consequence="Appointment and revenue history come from the feed, so "
                        "this account has none. Booking through the live API is "
                        "unaffected.",
            fix=f"{settings_base}/pms"))
    elif pms_type == "blueprint":
        absent = [k for k in _FEED_SECRETS
                  if not _secret_present(instance_id, list(by_id), "blueprint", k)]
        if absent:
            checks.append(_check(
                "pms_secrets", "Feed credentials", "blocked",
                f"Not found: {', '.join(absent)}.",
                consequence="The data feed cannot be downloaded, so no "
                            "appointments or revenue arrive.",
                fix=f"{settings_base}/pms", items=absent))
        else:
            checks.append(_check(
                "pms_secrets", "Feed credentials", "ok",
                "All feed credentials present.", fix=f"{settings_base}/pms"))

    # ── 4. Every location mapped ──────────────────────────────────────────────
    if pms_type:
        unmapped_clinics = [c.clinic_name for c in clinics
                            if c.clinic_id not in mapped_clinic_ids]
        has_catch_all = any(
            l.vendor_location_key == CATCH_ALL_LOCATION_KEY for l in active_locs)

        feed_locs = feed["locations"]
        # The catch-all means "every row of this feed belongs to this clinic", so
        # nothing the feed reports can be unmapped. Comparing ids against it would
        # report every location of a correctly-configured single-clinic account.
        unmapped_feed = ([
            f"{k} ({v or 'unnamed'})" for k, v in sorted(feed_locs.items())
            if k not in {l.vendor_location_key for l in locations}
        ] if feed_locs and not has_catch_all else [])

        if not locations:
            checks.append(_check(
                "locations_mapped", "Location map", "blocked",
                "No locations mapped.",
                consequence="Nothing from this account is attributed to any clinic.",
                fix=f"{settings_base}/pms"))
        elif unmapped_feed:
            # Two legitimate resolutions, and the system cannot tell them apart
            # until one is recorded — which is exactly what a retired row is for.
            # Saying only "map it" would push someone to invent a clinic for a
            # site that closed years ago.
            checks.append(_check(
                "locations_mapped", "Location map", "blocked",
                f"{len(unmapped_feed)} location(s) in the feed are not mapped.",
                consequence="Their appointments and revenue are held back on every "
                            "sync and belong to no clinic. Map each to a clinic, or "
                            "mark it retired if the site has closed.",
                fix=f"{settings_base}/pms", items=unmapped_feed))
        elif has_catch_all and len(clinics) > 1:
            checks.append(_check(
                "locations_mapped", "Location map", "warn",
                "Mapped as a single location while the business has "
                f"{len(clinics)} clinics.",
                consequence="Every location's rows land on one clinic, so its "
                            "figures include the others'.",
                fix=f"{settings_base}/pms"))
        elif unmapped_clinics:
            checks.append(_check(
                "locations_mapped", "Location map", "warn",
                f"{len(unmapped_clinics)} clinic(s) receive nothing.",
                consequence="Those clinics show no PMS data at all.",
                fix=f"{settings_base}/pms", items=unmapped_clinics,
                clinic_ids=[c.clinic_id for c in clinics
                            if c.clinic_id not in mapped_clinic_ids]))
        elif feed_locs is None and has_feed:
            checks.append(_check(
                "locations_mapped", "Location map", "unknown",
                f"{len(active_locs)} mapped; the feed has not been read yet, so "
                "completeness can't be confirmed.",
                fix=f"{settings_base}/pms"))
        else:
            checks.append(_check(
                "locations_mapped", "Location map", "ok",
                f"{len(active_locs)} location(s) mapped.",
                fix=f"{settings_base}/pms"))

    # ── 5. Mapped clinics are ETL-enabled ─────────────────────────────────────
    off = [by_id[cid].clinic_name for cid in mapped_clinic_ids
           if cid in by_id and not by_id[cid].etl_enabled]
    if pms_type:
        if off:
            checks.append(_check(
                "etl_enabled", "ETL enabled", "blocked",
                f"Off for {len(off)} mapped clinic(s).",
                consequence="Their rows are ingested but the dashboard and marts "
                            "never surface them — the page looks empty, not broken.",
                fix=f"/settings/{next(iter(mapped_clinic_ids))}/details" if mapped_clinic_ids else None,
                items=off,
                clinic_ids=[cid for cid in mapped_clinic_ids
                            if cid in by_id and not by_id[cid].etl_enabled]))
        elif mapped_clinic_ids:
            checks.append(_check(
                "etl_enabled", "ETL enabled", "ok",
                f"On for all {len(mapped_clinic_ids)} mapped clinic(s)."))

    # ── 6. The feed has been read since the map last changed ──────────────────
    if has_feed and locations:
        # A catch-all account has exactly one destination, so no edit to its map
        # can re-attribute a row — comparing its timestamp against the snapshot
        # would flag every account migration 0030 back-filled, on the day it ran,
        # for a change that by construction altered nothing.
        attribution_can_change = any(
            l.vendor_location_key != CATCH_ALL_LOCATION_KEY for l in locations)
        last_map_change = max(
            (l.updated_at for l in locations if l.updated_at), default=None
        ) if attribution_can_change else None
        snap = feed["snapshot"]
        if snap is None:
            checks.append(_check(
                "sync_fresh", "Feed sync", "blocked",
                "No feed has been ingested for this business yet.",
                consequence="Every PMS figure is empty until the sync runs."))
        elif last_map_change and str(snap) < last_map_change.date().isoformat():
            checks.append(_check(
                "sync_fresh", "Feed sync", "blocked",
                f"Last sync {snap}; the map changed "
                f"{last_map_change.date().isoformat()}.",
                consequence="Rows are still attributed the way they were before "
                            "the map changed, so per-location figures are wrong.",
                items=[f"Run blueprint-sync, then marts-build"]))
        else:
            checks.append(_check(
                "sync_fresh", "Feed sync", "ok", f"Last sync {snap}."))

    # ── 7. Campaigns, per clinic ──────────────────────────────────────────────
    ga = {c for c in db.scalars(select(GoogleAdsCampaign.clinic_id).where(
        GoogleAdsCampaign.clinic_id.in_(by_id or [""]),
        GoogleAdsCampaign.active.is_(True)))}
    inv = {c for c in db.scalars(select(InvocaCampaign.clinic_id).where(
        InvocaCampaign.clinic_id.in_(by_id or [""]),
        InvocaCampaign.active.is_(True)))}
    no_campaigns = [c.clinic_name for c in clinics
                    if c.clinic_id not in ga and c.clinic_id not in inv]
    if clinics:
        if no_campaigns:
            checks.append(_check(
                "campaigns", "Campaigns", "warn",
                f"{len(no_campaigns)} clinic(s) have no active campaign.",
                consequence="No calls or ad spend are attributed to them, so their "
                            "acquisition figures read as zero rather than unmeasured.",
                fix=f"/settings/{clinics[0].clinic_id}/campaigns",
                items=no_campaigns,
                clinic_ids=[c.clinic_id for c in clinics
                            if c.clinic_id not in ga and c.clinic_id not in inv]))
        else:
            checks.append(_check(
                "campaigns", "Campaigns", "ok",
                "Every clinic has at least one active campaign."))

    # ── 8. Group rollup (informational — it is derived, never configured) ─────
    if clinic_count(db, instance_id) >= 2:
        checks.append(_check(
            "group", "Group Intelligence", "ok",
            "Available — this business has multiple locations.",
            fix=f"/intelligence/group/{instance_id}"))

    blocked = [c for c in checks if c["status"] == "blocked"]
    warn = [c for c in checks if c["status"] == "warn"]
    overall = "blocked" if blocked else "warn" if warn else "ok"

    return {
        "instance_id": instance_id,
        "instance_name": instance.instance_name,
        "pms_type": pms_type,
        "overall": overall,
        "blocked_count": len(blocked),
        "warn_count": len(warn),
        "multi_location": is_multi_location(db, instance_id),
        "checks": checks,
    }
