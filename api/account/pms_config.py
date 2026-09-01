"""
PMS configuration. Credentials are ALWAYS account-level; the only per-clinic
part is which vendor location feeds the clinic (alembic 0030).

    instance_pms_config     one row per (instance, pms_type) — the account's
                            credentials, URLs and feed identifiers, plus the
                            clinic that receives rows carrying no location
    pms_clinic_locations    vendor location -> clinic, plus the two settings that
                            really are per-clinic: prompt_for_location and
                            booking_user_id

Before 0030 the same thing could be configured in two places with the clinic
winning, which made the losing one invisible: a clinic wired directly kept
claiming its whole account's feed while the account config sat there looking
correct. There is now one place.

Per-vendor fields; the unused ones stay NULL:

    blueprint    clinic_code, api_url, aws_url
    counselear   counselear_location_code, counselear_sftp_username

A single-location account still gets a map row — the catch-all key ``"*"``,
meaning "every row of this feed belongs to this clinic". Without it, "configured"
and "ingesting" come apart again. It must be the only row for its account.

Secrets live in Secret Manager and never touch the DB:

    instance_{instance_id}_blueprint_{api_key,aws_access_key_id,
                                      aws_secret_access_key,zip_password}

CounselEar is the exception and is deliberately left alone: its secrets are named
after the SFTP login (``{Username}_COUNSELEAR_SFTP_password``, set up by
``provision_sftp.sh``), and that username is itself account-level config here. So
the account row is enough to locate them and no rename — hence no migration of
live credentials — is needed. This endpoint therefore accepts no CounselEar
secrets.

Readers (the ETL, the voice agent) try the instance scope first and fall back to
a mapped clinic's own ``clinic_{clinic_id}_…`` secret with a warning, so 0030 did
not have to be sequenced against a secret copy.
``scripts/copy_pms_secrets_to_instance.py`` retires the fallback.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import require_read_access, require_write_access, verify_token
from api.models import InstancePmsConfigSet, PmsLocationImport
from api.core.db import get_session
from api.account.provisioning import provision_clinic
from api.core.orm import (
    CATCH_ALL_LOCATION_KEY, Clinic, Instance, InstancePmsConfig, PmsClinicLocation,
)
from api.core.secrets import get_secret


log = logging.getLogger(__name__)

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_clinic_or_404(db: Session, clinic_id: str) -> Clinic:
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return clinic


def _sm_secret_name(scope: str, scope_id: str, pms_type: str, key: str) -> str:
    """``{scope}_{id}_{pms}_{key}`` — scope is ``clinic`` or ``instance``.

    The scope is part of the name because the two mean different things to the
    ETL: a clinic-scoped secret belongs to one clinic's feed, an instance-scoped
    one to an account whose feed is split across several clinics.
    """
    return f"{scope}_{scope_id}_{pms_type}_{key}"


def _write_pms_secret(scope: str, scope_id: str, pms_type: str,
                      key: str, value: str) -> None:
    """Write a PMS secret to SM (create the secret, or add a new version)."""
    from google.cloud import secretmanager

    sm = secretmanager.SecretManagerServiceClient()
    secret_id = _sm_secret_name(scope, scope_id, pms_type, key)
    project = "project-demo-2-482101"  # mirrors services/secrets.py
    parent = f"projects/{project}"
    secret_path = f"{parent}/secrets/{secret_id}"

    try:
        sm.get_secret(request={"name": secret_path})
        sm.add_secret_version(
            request={"parent": secret_path, "payload": {"data": value.encode("utf-8")}}
        )
    except Exception:
        sm.create_secret(
            request={
                "parent": parent,
                "secret_id": secret_id,
                "secret": {"replication": {"automatic": {}}},
            }
        )
        sm.add_secret_version(
            request={"parent": secret_path, "payload": {"data": value.encode("utf-8")}}
        )
    # Force any cached read of the previous version to refresh.
    get_secret.cache_clear()


# ── Per-vendor field maps ─────────────────────────────────────────────────────

# Non-secret account config, by vendor. Everything here is a property of the PMS
# login, not of any clinic it feeds.
_VENDOR_CONFIG_FIELDS: dict[str, tuple[str, ...]] = {
    "blueprint": ("clinic_code", "api_url", "aws_url"),
    "counselear": ("counselear_location_code", "counselear_sftp_username"),
}

# Secrets this endpoint will write. CounselEar's are named after the SFTP login
# rather than an id (see the module docstring), so they are located from the
# account config and left where provision_sftp.sh put them.
_VENDOR_SECRET_KEYS: dict[str, tuple[str, ...]] = {
    "blueprint": ("api_key", "aws_access_key_id", "aws_secret_access_key",
                  "zip_password"),
    "counselear": (),
}

_SUPPORTED = tuple(_VENDOR_CONFIG_FIELDS)


def _vendor_or_400(pms_type: str) -> str:
    if pms_type not in _VENDOR_CONFIG_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"pms_type must be one of {sorted(_SUPPORTED)}; "
                   f"{pms_type!r} has no account-level config.",
        )
    return pms_type


# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_instance_or_404(db: Session, instance_id: str) -> Instance:
    instance = db.get(Instance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    return instance


def _instance_clinics(db: Session, instance_id: str) -> dict[str, Clinic]:
    return {
        c.clinic_id: c
        for c in db.scalars(
            select(Clinic).where(
                Clinic.instance_id == instance_id,
                Clinic.deleted_at.is_(None),
            )
        )
    }


def _location_rows(db: Session, instance_id: str,
                   pms_type: str) -> list[PmsClinicLocation]:
    return list(db.scalars(
        select(PmsClinicLocation)
        .where(
            PmsClinicLocation.instance_id == instance_id,
            PmsClinicLocation.pms_type == pms_type,
        )
        .order_by(PmsClinicLocation.vendor_location_key)
    ))


def _serialise_location(l: PmsClinicLocation, clinics: dict[str, Clinic]) -> dict:
    return {
        "vendor_location_key": l.vendor_location_key,
        "clinic_id": l.clinic_id,
        "clinic_name": clinics[l.clinic_id].clinic_name if l.clinic_id in clinics else None,
        "location_name": l.location_name,
        "active": bool(l.active),
        "catch_all": l.vendor_location_key == CATCH_ALL_LOCATION_KEY,
        "prompt_for_location": bool(l.prompt_for_location),
        "booking_user_id": l.booking_user_id,
    }


# ── Per-clinic: read-only ─────────────────────────────────────────────────────

@router.get("/clinics/{clinic_id}/pms")
def get_clinic_pms_view(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    How this clinic is fed, resolved from its instance's account config.

    Read-only by design. There is no per-clinic PMS configuration to set any more
    — credentials belong to the account and the mapping is edited on the
    instance — so this exists to answer "where does this clinic's PMS data come
    from", not to be an editor.
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_read_access(clinic.instance_id, caller)

    pms_type = clinic.pms_type or "none"
    if pms_type not in _VENDOR_CONFIG_FIELDS:
        return {"clinic_id": clinic_id, "pms_type": pms_type, "configured": False,
                "account": None, "locations": []}

    cfg = db.get(InstancePmsConfig, (clinic.instance_id, pms_type))
    clinics = _instance_clinics(db, clinic.instance_id)
    mine = [l for l in _location_rows(db, clinic.instance_id, pms_type)
            if l.clinic_id == clinic_id]

    return {
        "clinic_id": clinic_id,
        "pms_type": pms_type,
        "configured": cfg is not None and any(
            getattr(cfg, f) for f in _VENDOR_CONFIG_FIELDS[pms_type]),
        "account": {
            "instance_id": clinic.instance_id,
            "config": {f: getattr(cfg, f) for f in _VENDOR_CONFIG_FIELDS[pms_type]}
            if cfg else {},
            "is_primary": bool(cfg and cfg.primary_clinic_id == clinic_id),
        } if cfg else None,
        "locations": [_serialise_location(l, clinics) for l in mine],
    }


# ── Account-scoped ────────────────────────────────────────────────────────────

@router.get("/instances/{instance_id}/pms")
def get_instance_pms_config(
    instance_id: str,
    pms_type: str = "blueprint",
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """The account's PMS config and its location map. Secrets are never returned."""
    instance = _get_instance_or_404(db, instance_id)
    require_read_access(instance_id, caller)
    _vendor_or_400(pms_type)

    cfg = db.get(InstancePmsConfig, (instance_id, pms_type))
    locations = _location_rows(db, instance_id, pms_type)
    clinics = _instance_clinics(db, instance_id)

    # "Configured" has to mean usable, not merely present. A row with every field
    # NULL satisfies `is not None` while ingesting nothing and listing no
    # locations, which is a worse lie than reporting nothing at all.
    configured = cfg is not None and any(
        getattr(cfg, f) for f in _VENDOR_CONFIG_FIELDS[pms_type])

    return {
        "instance_id": instance_id,
        "instance_name": instance.instance_name,
        "pms_type": pms_type,
        "configured": configured,
        "config": {f: getattr(cfg, f) if cfg else None
                   for f in _VENDOR_CONFIG_FIELDS[pms_type]},
        "secret_keys": list(_VENDOR_SECRET_KEYS[pms_type]),
        "primary_clinic_id": cfg.primary_clinic_id if cfg else None,
        "locations": [_serialise_location(l, clinics) for l in locations],
        "clinics": [
            {"clinic_id": c.clinic_id, "clinic_name": c.clinic_name,
             "pms_type": c.pms_type, "etl_enabled": bool(c.etl_enabled)}
            for c in sorted(clinics.values(), key=lambda c: c.clinic_name)
        ],
        # Mapped but not ETL-enabled. PMS ingest is gated on pms_type rather than
        # this flag, so such a clinic can hold rows the dashboard never surfaces.
        "mapped_not_etl_enabled": [
            {"clinic_id": l.clinic_id,
             "clinic_name": clinics[l.clinic_id].clinic_name}
            for l in locations
            if l.active and l.clinic_id in clinics
            and not clinics[l.clinic_id].etl_enabled
        ],
    }


def _validate_locations(entries: list, clinics: dict[str, Clinic]) -> None:
    """Reject a map that would mis-attribute or double-ingest rows.

    Every rule corresponds to a way the ETL would go quietly wrong rather than
    fail, which is why they are enforced at write time.
    """
    keys = [e.vendor_location_key.strip() for e in entries]
    catch_alls = [k for k in keys if k == CATCH_ALL_LOCATION_KEY]
    if catch_alls and len(keys) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"The catch-all key {CATCH_ALL_LOCATION_KEY!r} means every row of "
                   f"the feed belongs to one clinic, so it cannot sit beside "
                   f"specific locations. Use one or the other.",
        )

    seen: set[str] = set()
    for entry in entries:
        key = entry.vendor_location_key.strip()
        if not key:
            raise HTTPException(status_code=400,
                                detail="vendor_location_key cannot be blank")
        if key in seen:
            # The UNIQUE index would catch this, but as a 500-shaped IntegrityError.
            raise HTTPException(
                status_code=400,
                detail=f"Location {key!r} appears twice — a location maps to exactly "
                       f"one clinic, or its rows would be ingested twice.",
            )
        seen.add(key)

        if entry.active:
            if not entry.clinic_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"Location {key!r} is active but has no clinic. An active "
                           f"location must route somewhere.",
                )
            if entry.clinic_id not in clinics:
                raise HTTPException(
                    status_code=400,
                    detail=f"Location {key!r} maps to clinic {entry.clinic_id}, which "
                           f"is not a clinic of this instance.",
                )
        elif entry.clinic_id:
            raise HTTPException(
                status_code=400,
                detail=f"Location {key!r} is retired, so it must not name a clinic — "
                       f"a retired mapping is one a later reader would act on.",
            )
        if key == CATCH_ALL_LOCATION_KEY and not entry.active:
            raise HTTPException(
                status_code=400,
                detail="The catch-all cannot be retired — retiring the only mapping "
                       "stops the account being ingested at all. Delete it instead.",
            )


def _apply_locations(db: Session, instance_id: str, pms_type: str,
                     entries: list) -> None:
    existing = {
        l.vendor_location_key: l
        for l in _location_rows(db, instance_id, pms_type)
    }
    for entry in entries:
        key = entry.vendor_location_key.strip()
        row = existing.pop(key, None)
        if row is None:
            row = PmsClinicLocation(instance_id=instance_id, pms_type=pms_type,
                                    vendor_location_key=key)
            db.add(row)
        row.clinic_id = entry.clinic_id if entry.active else None
        row.location_name = entry.location_name
        row.active = entry.active
        row.prompt_for_location = bool(entry.prompt_for_location)
        row.booking_user_id = entry.booking_user_id
    # Anything left is absent from the payload: the caller removed it.
    for row in existing.values():
        db.delete(row)


def _adopt_mapped_clinics(clinics: dict[str, Clinic], entries: list,
                          pms_type: str) -> list[dict]:
    """Point every mapped clinic at this PMS.

    Without this the mapping stores fine and ingests nothing: the ETL scopes an
    account to clinics of the matching pms_type, so a location mapped to a clinic
    still set to 'none' is invisible to it.

    ``etl_enabled`` is deliberately untouched — it also gates the Google Ads and
    Invoca pipelines, and flipping it here would start ingestion nobody asked
    for. The GET reports ``mapped_not_etl_enabled`` instead.
    """
    updated = []
    for entry in entries:
        if not (entry.active and entry.clinic_id):
            continue
        clinic = clinics[entry.clinic_id]
        if clinic.pms_type != pms_type:
            updated.append({"clinic_id": clinic.clinic_id,
                            "clinic_name": clinic.clinic_name,
                            "pms_type_from": clinic.pms_type,
                            "pms_type_to": pms_type})
            clinic.pms_type = pms_type
    return updated


@router.post("/instances/{instance_id}/pms")
def set_instance_pms_config(
    instance_id: str,
    body: InstancePmsConfigSet,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Set or replace the account's PMS configuration and its location map."""
    _get_instance_or_404(db, instance_id)
    require_write_access(instance_id, caller)
    pms_type = _vendor_or_400(body.pms_type)

    allowed = _VENDOR_CONFIG_FIELDS[pms_type]
    config = body.config or {}
    unknown = set(config) - set(allowed)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown {pms_type} config fields: {sorted(unknown)}. "
                   f"Allowed: {sorted(allowed)}",
        )

    clinics = _instance_clinics(db, instance_id)
    if body.primary_clinic_id and body.primary_clinic_id not in clinics:
        raise HTTPException(status_code=400,
                            detail="primary_clinic_id is not a clinic of this instance")
    if body.locations is not None:
        _validate_locations(body.locations, clinics)

    secret_keys = _VENDOR_SECRET_KEYS[pms_type]
    if body.secrets:
        unknown_secrets = set(body.secrets) - set(secret_keys)
        if unknown_secrets:
            detail = (f"{pms_type} secrets are named after the SFTP login and are "
                      f"not set here — see the account config fields instead."
                      if not secret_keys else
                      f"Unknown {pms_type} secret keys: {sorted(unknown_secrets)}")
            raise HTTPException(status_code=400, detail=detail)

    cfg = db.get(InstancePmsConfig, (instance_id, pms_type))
    creating = cfg is None
    if creating:
        has_anything = (
            any(str(config.get(f) or "").strip() for f in allowed)
            or bool(body.secrets)
            or bool(body.locations)
            or bool(body.primary_clinic_id)
        )
        if not has_anything:
            raise HTTPException(
                status_code=400,
                detail=f"Nothing to save. Enter the account's {pms_type} details "
                       f"first — an empty config row would read as configured "
                       f"while ingesting nothing.",
            )
        cfg = InstancePmsConfig(instance_id=instance_id, pms_type=pms_type)
        db.add(cfg)
    # A blank means "leave unchanged", never "clear". Clearing an account's
    # api_url or feed URL has no legitimate use, and treating blank as a clear
    # made an accidental Save on an unfilled form silently destroy the only copy
    # of a client's PMS config — which is exactly what happened to one account
    # before this guard existed. Same convention as InstanceUpdate's
    # _reject_empty_string: a value can be corrected but not blanked.
    for field in allowed:
        value = config.get(field)
        if isinstance(value, str):
            value = value.strip()
        if value:
            setattr(cfg, field, value)
    if "primary_clinic_id" in body.model_fields_set:
        cfg.primary_clinic_id = body.primary_clinic_id
    db.flush()  # the map's composite FK needs the config row to exist

    clinics_updated: list[dict] = []
    if body.locations is not None:
        _apply_locations(db, instance_id, pms_type, body.locations)
        clinics_updated = _adopt_mapped_clinics(clinics, body.locations, pms_type)

    for key, value in (body.secrets or {}).items():
        if value:
            _write_pms_secret("instance", instance_id, pms_type, key, str(value))

    return {"status": "success", "instance_id": instance_id,
            "pms_type": pms_type, "clinics_updated": clinics_updated}


@router.delete("/instances/{instance_id}/pms")
def clear_instance_pms_config(
    instance_id: str,
    pms_type: str = "blueprint",
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    Remove the account's config and its whole location map.

    Clinics keep their ``pms_type`` — deleting the account config must not quietly
    unwire them from PMS ingest as a side effect. SM secrets are left in place.
    """
    _get_instance_or_404(db, instance_id)
    require_write_access(instance_id, caller)
    _vendor_or_400(pms_type)

    cfg = db.get(InstancePmsConfig, (instance_id, pms_type))
    if cfg is None:
        raise HTTPException(status_code=404,
                            detail="No account-level PMS config for this instance")
    removed = 0
    for row in _location_rows(db, instance_id, pms_type):
        db.delete(row)
        removed += 1
    db.delete(cfg)

    return {"status": "success", "instance_id": instance_id,
            "pms_type": pms_type, "locations_removed": removed}


# ── Discovery: ask the PMS what locations exist ───────────────────────────────

def _blueprint_api_credentials(db: Session, instance_id: str) -> tuple[str, str]:
    """(base_url, api_key) for an account, from its config + Secret Manager.

    Falls back to a mapped clinic's own api_key secret, because 0030 moved the
    config without moving credentials — see the module docstring. The fallback is
    logged so it is visible rather than permanent.
    """
    cfg = db.get(InstancePmsConfig, (instance_id, "blueprint"))
    if cfg is None or not cfg.api_url:
        raise HTTPException(
            status_code=400,
            detail="This account has no Blueprint api_url configured, so its "
                   "locations cannot be listed. Save the account config first.",
        )

    from api.voice_agent.pms.blueprint import _blueprint_base

    api_key = None
    try:
        api_key = get_secret(_sm_secret_name("instance", instance_id, "blueprint",
                                             "api_key"))
    except Exception:
        pass
    if not api_key:
        for row in _location_rows(db, instance_id, "blueprint"):
            if not row.clinic_id:
                continue
            try:
                api_key = get_secret(
                    _sm_secret_name("clinic", row.clinic_id, "blueprint", "api_key"))
            except Exception:
                continue
            if api_key:
                log.warning(
                    "Blueprint api_key for instance %s read from clinic %s's secret "
                    "— copy it to instance_%s_blueprint_api_key to retire the "
                    "fallback", instance_id, row.clinic_id, instance_id)
                break
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="No Blueprint API key found for this account. Save it under "
                   "the account secrets and try again.",
        )

    return _blueprint_base({"api_url": cfg.api_url}), api_key


@router.get("/instances/{instance_id}/pms/discover")
def discover_pms_locations(
    instance_id: str,
    pms_type: str = "blueprint",
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    List the locations the PMS itself reports, with what each is already mapped to.

    This is what makes onboarding start from the PMS rather than from a typed-out
    list of clinics: provision the instance with its account config, ask the PMS
    what sites exist, then create a clinic per site.

    **The Blueprint list is not guaranteed complete.** ``clinicConfiguration``
    returns only locations enabled for online booking, and drops any whose name is
    unset — so a site that exists, bills, and books by phone can be missing. The
    complete list is the ``Location`` table in the S3 data feed
    (``pms.blueprint.sync --discover``), which needs one sync to have run. The
    response says which source it used and flags the limitation rather than
    presenting a partial list as the whole truth.

    CounselEar has no locations endpoint: its feed tags every row with a clinic
    id, and those ids are only visible once a feed has landed
    (``pms.counselear.api_backfill --verify-clinics``).
    """
    _get_instance_or_404(db, instance_id)
    require_read_access(instance_id, caller)
    _vendor_or_400(pms_type)

    if pms_type != "blueprint":
        raise HTTPException(
            status_code=501,
            detail="CounselEar exposes no locations endpoint. Its per-row clinic "
                   "ids come from a landed feed — run "
                   "`pms.counselear.api_backfill --verify-clinics` and enter them "
                   "as location keys.",
        )

    base, api_key = _blueprint_api_credentials(db, instance_id)

    import httpx

    try:
        resp = httpx.get(f"{base}/clinicConfiguration/",
                         params={"apiKey": api_key}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Blueprint returned {exc.response.status_code} for "
                   f"clinicConfiguration. Check the api_url and API key.",
        )
    except Exception as exc:
        raise HTTPException(status_code=502,
                            detail=f"Could not reach Blueprint: {type(exc).__name__}")

    existing = {l.vendor_location_key: l
                for l in _location_rows(db, instance_id, pms_type)}
    clinics = _instance_clinics(db, instance_id)

    found, unnamed = [], 0
    for loc in data.get("locations", []):
        key = str(loc.get("id") or "").strip()
        if not key:
            continue
        name = (loc.get("name") or "").strip()
        if not name:
            # Kept, unlike the voice agent's list_locations which drops these —
            # an unnamed site still has appointments and revenue to attribute.
            unnamed += 1
        row = existing.get(key)
        found.append({
            "vendor_location_key": key,
            "location_name": name or None,
            "address": loc.get("formattedAddress") or loc.get("street"),
            "timezone": loc.get("timezone") or loc.get("timeZone"),
            "mapped": row is not None,
            "clinic_id": row.clinic_id if row else None,
            "clinic_name": (clinics[row.clinic_id].clinic_name
                            if row and row.clinic_id in clinics else None),
            "active": bool(row.active) if row else None,
        })

    found.sort(key=lambda f: (len(f["vendor_location_key"]),
                              f["vendor_location_key"]))
    stale = sorted(k for k in existing
                   if k != CATCH_ALL_LOCATION_KEY
                   and k not in {f["vendor_location_key"] for f in found})

    return {
        "instance_id": instance_id,
        "pms_type": pms_type,
        "source": "blueprint clinicConfiguration",
        # Say plainly what this source cannot see, so a short list is not read as
        # a complete one.
        "caveat": ("Only locations enabled for online booking are returned. The "
                   "complete list is the Location table in the S3 data feed — run "
                   "`pms.blueprint.sync --discover` once a feed has landed."),
        "unnamed_count": unnamed,
        "locations": found,
        # Mapped keys the PMS no longer reports. Usually a closed site, which
        # should be retired rather than deleted so its historical rows stay
        # attributable and the sync stops reporting them as unmapped.
        "mapped_but_not_reported": stale,
        "has_catch_all": CATCH_ALL_LOCATION_KEY in existing,
    }


@router.post("/instances/{instance_id}/pms/locations/import")
def import_pms_locations(
    instance_id: str,
    body: PmsLocationImport,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    Create a clinic per PMS location and map it — onboarding in one step.

    The point of the new onboarding flow: an instance is provisioned with its
    account config and no clinics, and the clinics come from what the PMS says
    exists. Each entry either names an existing clinic or asks for one to be
    created, and clinics are created through ``provision_clinic`` so they get
    their 1:1 sub-tables and seeded voice-agent defaults — a bare row would leave
    the voice agent and the revenue-per-clinic-hour KPI reading NULLs.

    Importing real locations removes any catch-all: the two cannot coexist, and a
    catch-all left behind would keep sending the whole feed to one clinic.

    Idempotent — re-importing the same locations adopts the existing clinics and
    rewrites their mapping rather than creating duplicates.
    """
    _get_instance_or_404(db, instance_id)
    require_write_access(instance_id, caller)
    pms_type = _vendor_or_400(body.pms_type)

    if not body.locations:
        raise HTTPException(status_code=400, detail="No locations to import")

    keys = [e.vendor_location_key.strip() for e in body.locations]
    if CATCH_ALL_LOCATION_KEY in keys:
        raise HTTPException(
            status_code=400,
            detail="Import is for real vendor locations. Set a catch-all through "
                   "the account config instead.",
        )
    if len(set(keys)) != len(keys):
        raise HTTPException(status_code=400,
                            detail="The same location appears twice in the import")

    clinics = _instance_clinics(db, instance_id)
    by_name = {c.clinic_name.strip().casefold(): c for c in clinics.values()}
    existing = {l.vendor_location_key: l
                for l in _location_rows(db, instance_id, pms_type)}

    created, mapped, warnings = [], [], []

    for entry in body.locations:
        key = entry.vendor_location_key.strip()
        if not key:
            raise HTTPException(status_code=400,
                                detail="vendor_location_key cannot be blank")

        clinic = None
        if entry.clinic_id:
            clinic = clinics.get(entry.clinic_id)
            if clinic is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Location {key!r} names clinic {entry.clinic_id}, which "
                           f"is not a clinic of this instance.",
                )
        else:
            name = (entry.clinic_name or entry.location_name or "").strip()
            if not name:
                raise HTTPException(
                    status_code=400,
                    detail=f"Location {key!r} has no clinic and no name to create "
                           f"one from. Blueprint leaves some locations unnamed — "
                           f"supply clinic_name for those.",
                )
            clinic = by_name.get(name.casefold())
            if clinic is None:
                clinic_id, _ = provision_clinic(db, {
                    "clinic_name": name,
                    "address": entry.address,
                    "country": entry.country,
                    "time_zone": entry.time_zone,
                }, instance_id)
                db.flush()
                clinic = db.get(Clinic, clinic_id)
                clinics[clinic_id] = clinic
                by_name[name.casefold()] = clinic
                created.append({"clinic_id": clinic_id, "clinic_name": name})
                if not entry.time_zone:
                    warnings.append(
                        f"{name}: no time zone, and no opening hours — the "
                        f"revenue-per-clinic-hour KPI and the 'open now' gate on "
                        f"active leads both read those. Set them under "
                        f"Settings → Details.")

        row = existing.pop(key, None)
        if row is None:
            row = PmsClinicLocation(instance_id=instance_id, pms_type=pms_type,
                                    vendor_location_key=key)
            db.add(row)
        row.clinic_id = clinic.clinic_id
        row.location_name = entry.location_name
        row.active = True
        if clinic.pms_type != pms_type:
            clinic.pms_type = pms_type
        mapped.append({"vendor_location_key": key, "clinic_id": clinic.clinic_id,
                       "clinic_name": clinic.clinic_name})

    catch_all = existing.pop(CATCH_ALL_LOCATION_KEY, None)
    if catch_all is not None:
        db.delete(catch_all)
        warnings.append(
            "Removed the catch-all mapping — real locations replace it. Rows are "
            "now split by location instead of all landing on one clinic.")

    return {"status": "success", "instance_id": instance_id, "pms_type": pms_type,
            "clinics_created": created, "locations_mapped": mapped,
            "warnings": warnings}
