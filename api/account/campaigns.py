"""
Multi-campaign ID management per clinic — backed by Cloud SQL.

The legacy `clinic_campaigns` BQ table (single table, `campaign_type`
discriminator) has been split into typed tables:
    google_ads_campaigns  → (id, clinic_id, google_ads_campaign_id, active)
    invoca_campaigns      → (id, clinic_id, invoca_campaign_id, active)
    jotform_forms         → (id, clinic_id, jotform_form_id, form_title, active)

`jotform` rows register a clinic on the Jotform → webhook → BigQuery lead
pipeline (see api/webforms.py). Unlike the other two types a form maps to
exactly ONE clinic (UNIQUE on jotform_form_id) — the webhook URL is
clinic-scoped, so a second mapping would double-ingest every submission.

That one clinic is the form's DEFAULT. A form serving a whole group asks the
patient which site they want, and `jotform_form_locations` maps each answer
to a clinic so the submission is attributed to the site the patient chose.
The /locations endpoints below maintain that map.

URL shape:
    GET    /campaigns/{instance_id}                  → both types, all clinics
    GET    /campaigns/{instance_id}/{clinic_id}      → both types, one clinic
    POST   /campaigns/{clinic_id}  body{campaign_type, external_campaign_id, active}
    DELETE /campaigns/{campaign_type}/{id}           → explicit type required
    GET    /campaigns/{instance_id}/jotform/locations → every form + its routing
    GET    /campaigns/jotform/{form_id}/locations    → the form's location map
    PUT    /campaigns/jotform/{form_id}/locations    → replace the location map
"""
import json
import logging
import urllib.parse
import urllib.request

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.deps import bq_client, require_read_access, require_write_access, verify_token
from api.models import ClinicCampaignCreate, JotformLocationMapSet
from api.core.db import get_session
from api.core.orm import (
    Clinic, GoogleAdsCampaign, Instance, InvocaCampaign, JotformForm,
    JotformFormLocation,
)
from api.core.secrets import get_secret

log = logging.getLogger(__name__)

router = APIRouter()

_CAMPAIGN_TYPES = ("google_ads", "invoca", "jotform")


# ── Catalog (BigQuery-backed) ────────────────────────────────────────────────

# Project/dataset that the big-query-ingestion ETL writes the catalogs into.
_CLINIC_DATA_DATASET = "project-demo-2-482101.ClinicData"

_JOTFORM_API = "https://api.jotform.com"


def _jotform_catalog(caller: dict, db: Session) -> list[dict]:
    """Catalog of Jotform forms, fetched live from the Jotform API.

    There is no per-instance upstream ID for Jotform: all forms live in the
    shared agency account (150+ forms spanning many businesses), so the
    listing is gated to super_admins — anyone else gets [] and the admin UI
    falls back to manual-ID entry. Linkage marking spans ALL clinics (form
    IDs are globally unique), so a form linked to another instance's clinic
    shows as taken. Any API failure (missing key, timeout, non-JSON) degrades
    to [] rather than 500 — the catalog is a convenience, not a dependency.
    """
    if caller.get("role") != "super_admin":
        return []

    api_key = (get_secret("jotform-api-key") or "").strip()
    if not api_key:
        return []

    linked_rows = db.execute(
        select(JotformForm.jotform_form_id, Clinic.clinic_name)
        .join(Clinic, Clinic.clinic_id == JotformForm.clinic_id)
        .where(Clinic.deleted_at.is_(None))
    ).all()
    linked: dict[str, list[str]] = {}
    for form_id, clinic_name in linked_rows:
        linked.setdefault(str(form_id), []).append(clinic_name)

    try:
        url = f"{_JOTFORM_API}/user/forms?limit=1000&apiKey={urllib.parse.quote(api_key)}"
        with urllib.request.urlopen(urllib.request.Request(url), timeout=15) as resp:
            forms = json.loads(resp.read().decode()).get("content") or []
    except Exception as exc:  # network/auth/parse — degrade to empty catalog
        log.warning("Jotform catalog fetch failed: %s", exc)
        return []

    out = []
    for f in forms:
        if not isinstance(f, dict) or f.get("status") == "DELETED":
            continue
        form_id = str(f.get("id") or "")
        if not form_id:
            continue
        out.append({
            "external_campaign_id": form_id,
            "name":                 f.get("title") or form_id,
            "status":               f.get("status"),
            "already_linked":       form_id in linked,
            "linked_clinic_names":  linked.get(form_id, []),
        })
    out.sort(key=lambda r: (r["name"] or "").lower())
    return out


@router.get("/campaigns_catalog/{campaign_type}/{instance_id}")
def get_campaigns_catalog(
    campaign_type: str,
    instance_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Return the catalog of campaigns visible to an instance, scoped by its
    Google Ads customer ID or Invoca advertiser ID.

    Each row carries ``already_linked`` and ``linked_clinic_names`` so the
    admin UI can mark campaigns that are already attached to a clinic on this
    instance.

    Source of truth:
      - google_ads → BQ ``ClinicData.google_ads_campaigns_catalog`` filtered by
        the instance's ``google_ads_customer_id``.
      - invoca → BQ ``ClinicData.invoca_campaigns_catalog`` filtered by the
        instance's ``invoca_profile_id``.
      - jotform → live Jotform API listing of the shared agency account
        (super_admin only — see ``_jotform_catalog``).

    Returns an empty list if the instance has no upstream account configured
    or the catalog has no matching rows yet.
    """
    require_read_access(instance_id, caller)
    if campaign_type not in _CAMPAIGN_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"campaign_type must be one of {', '.join(_CAMPAIGN_TYPES)}",
        )

    instance = db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    if campaign_type == "jotform":
        return _jotform_catalog(caller, db)

    # Existing linkages (per-clinic) — used to mark already-linked entries.
    if campaign_type == "google_ads":
        upstream_id = instance.google_ads_customer_id
        linked_rows = db.execute(
            select(
                GoogleAdsCampaign.google_ads_campaign_id,
                Clinic.clinic_name,
            )
            .join(Clinic, Clinic.clinic_id == GoogleAdsCampaign.clinic_id)
            .where(Clinic.instance_id == instance_id, Clinic.deleted_at.is_(None))
        ).all()
    else:
        upstream_id = instance.invoca_profile_id
        linked_rows = db.execute(
            select(
                InvocaCampaign.invoca_campaign_id,
                Clinic.clinic_name,
            )
            .join(Clinic, Clinic.clinic_id == InvocaCampaign.clinic_id)
            .where(Clinic.instance_id == instance_id, Clinic.deleted_at.is_(None))
        ).all()

    if not upstream_id:
        return []

    linked: dict[str, list[str]] = {}
    for ext_id, clinic_name in linked_rows:
        linked.setdefault(str(ext_id), []).append(clinic_name)

    # BQ catalog read.
    if campaign_type == "google_ads":
        sql = f"""
            SELECT
              CAST(campaign_id AS STRING) AS external_campaign_id,
              campaign_name AS name,
              status,
              advertising_channel_type
            FROM `{_CLINIC_DATA_DATASET}.google_ads_campaigns_catalog`
            WHERE CAST(google_ads_customer_id AS STRING) = @customer_id
            ORDER BY status, campaign_name
        """
        param_name, param_val = "customer_id", str(upstream_id)
    else:
        sql = f"""
            SELECT
              CAST(campaign_id AS STRING) AS external_campaign_id,
              campaign_name AS name,
              status,
              CAST(campaign_id_from_network AS STRING) AS campaign_id_from_network
            FROM `{_CLINIC_DATA_DATASET}.invoca_campaigns_catalog`
            WHERE CAST(invoca_profile_id AS STRING) = @profile_id
            ORDER BY status, campaign_name
        """
        param_name, param_val = "profile_id", str(upstream_id)

    from google.cloud import bigquery as _bq
    job = bq_client.query(
        sql,
        job_config=_bq.QueryJobConfig(
            query_parameters=[_bq.ScalarQueryParameter(param_name, "STRING", param_val)],
        ),
    )
    out: list[dict] = []
    for row in job.result():
        ext_id = row["external_campaign_id"]
        out.append({
            "external_campaign_id": ext_id,
            "name":                 row["name"],
            "status":               row["status"],
            "already_linked":       ext_id in linked,
            "linked_clinic_names":  linked.get(ext_id, []),
        })
    return out


def _gads_dict(c: GoogleAdsCampaign) -> dict:
    return {
        "id": c.id,
        "clinic_id": c.clinic_id,
        "campaign_type": "google_ads",
        "external_campaign_id": c.google_ads_campaign_id,
        "active": bool(c.active),
    }


def _invoca_dict(c: InvocaCampaign) -> dict:
    # ``tracking_numbers`` = the campaign's registered Invoca promo numbers
    # (synced from the Invoca API by configure_promo_numbers.py). Read-only
    # here; lets the admin UI show which dialed numbers attribute to this
    # campaign without a second endpoint.
    return {
        "id": c.id,
        "clinic_id": c.clinic_id,
        "campaign_type": "invoca",
        "external_campaign_id": c.invoca_campaign_id,
        "active": bool(c.active),
        "tracking_numbers": [
            {
                "promo_number": p.promo_number,
                "description": p.description,
                "media_type": p.media_type,
                "active": bool(p.active),
            }
            for p in c.promo_numbers
        ],
    }


def _jotform_dict(c: JotformForm) -> dict:
    # ``name`` rides along so the UI can label the opaque form ID even when the
    # live Jotform catalog is unavailable (non-super_admin or API failure).
    return {
        "id": c.id,
        "clinic_id": c.clinic_id,
        "campaign_type": "jotform",
        "external_campaign_id": c.jotform_form_id,
        "name": c.form_title,
        "active": bool(c.active),
    }


@router.get("/campaigns/{instance_id}")
def list_campaigns_for_instance(
    instance_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """List all campaign associations for every clinic in an instance."""
    require_read_access(instance_id, caller)

    gads = db.scalars(
        select(GoogleAdsCampaign)
        .join(Clinic, Clinic.clinic_id == GoogleAdsCampaign.clinic_id)
        .where(Clinic.instance_id == instance_id, Clinic.deleted_at.is_(None))
    ).all()
    invoca = db.scalars(
        select(InvocaCampaign)
        .join(Clinic, Clinic.clinic_id == InvocaCampaign.clinic_id)
        .where(Clinic.instance_id == instance_id, Clinic.deleted_at.is_(None))
    ).all()
    jotform = db.scalars(
        select(JotformForm)
        .join(Clinic, Clinic.clinic_id == JotformForm.clinic_id)
        .where(Clinic.instance_id == instance_id, Clinic.deleted_at.is_(None))
    ).all()

    return ([_gads_dict(c) for c in gads]
            + [_invoca_dict(c) for c in invoca]
            + [_jotform_dict(c) for c in jotform])


@router.get("/campaigns/{instance_id}/{clinic_id}")
def list_campaigns_for_clinic(
    instance_id: str,
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """List campaign associations for a specific clinic."""
    require_read_access(instance_id, caller)

    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.instance_id != instance_id or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")

    gads = db.scalars(
        select(GoogleAdsCampaign).where(GoogleAdsCampaign.clinic_id == clinic_id)
    ).all()
    invoca = db.scalars(
        select(InvocaCampaign).where(InvocaCampaign.clinic_id == clinic_id)
    ).all()
    jotform = db.scalars(
        select(JotformForm).where(JotformForm.clinic_id == clinic_id)
    ).all()

    return ([_gads_dict(c) for c in gads]
            + [_invoca_dict(c) for c in invoca]
            + [_jotform_dict(c) for c in jotform])


@router.post("/campaigns/{clinic_id}")
def add_campaign(
    clinic_id: str,
    body: ClinicCampaignCreate,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Add a campaign ID association to a clinic. Type-specific."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_write_access(clinic.instance_id, caller)

    if body.campaign_type == "google_ads":
        row = GoogleAdsCampaign(
            clinic_id=clinic_id,
            google_ads_campaign_id=body.external_campaign_id,
            active=body.active,
        )
    elif body.campaign_type == "invoca":
        row = InvocaCampaign(
            clinic_id=clinic_id,
            invoca_campaign_id=body.external_campaign_id,
            active=body.active,
        )
    else:  # jotform
        row = JotformForm(
            clinic_id=clinic_id,
            jotform_form_id=body.external_campaign_id,
            active=body.active,
        )

    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # google_ads/invoca: UNIQUE(clinic_id, external_id) — already linked here.
        # jotform: UNIQUE(jotform_form_id) — the form is mapped to SOME clinic
        # (possibly another one); a second mapping would double-ingest.
        detail = (
            "Jotform form already mapped to a clinic (a form can feed only one clinic)"
            if body.campaign_type == "jotform"
            else "Campaign already associated with this clinic"
        )
        raise HTTPException(status_code=409, detail=detail)

    return {"status": "success", "id": row.id, "campaign_type": body.campaign_type}


@router.delete("/campaigns/{campaign_type}/{campaign_id}")
def remove_campaign(
    campaign_type: str,
    campaign_id: int,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Remove a campaign association. Type must be a member of _CAMPAIGN_TYPES."""
    if campaign_type == "google_ads":
        row = db.get(GoogleAdsCampaign, campaign_id)
    elif campaign_type == "invoca":
        row = db.get(InvocaCampaign, campaign_id)
    elif campaign_type == "jotform":
        row = db.get(JotformForm, campaign_id)
    else:
        raise HTTPException(
            status_code=400,
            detail=f"campaign_type must be one of {', '.join(_CAMPAIGN_TYPES)}",
        )

    if row is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    clinic = db.get(Clinic, row.clinic_id)
    require_write_access(clinic.instance_id, caller)

    db.delete(row)
    return {"status": "success"}


# ── Jotform location map ─────────────────────────────────────────────────────
#
# A form's `jotform_forms` row names ONE clinic, because the webhook URL can
# only carry one. A group that runs every site off a single lead form needs the
# patient's own answer to decide where the lead belongs — that map lives here.

def _jotform_location_options(form_id: str) -> list[str] | None:
    """The form's live "choose your location" options, in builder order.

    Read straight off the Jotform API so the admin UI can show which options
    exist but are unmapped — the drift that silently misattributes leads. A form
    can carry SEVERAL location dropdowns revealed by condition (Sense of
    Hearing's has four: adult, 6-17, APD, 10-months-up), so every dropdown's
    options are pooled and de-duplicated; the resolver matches on the answer's
    value and does not care which field produced it.

    Returns None — distinct from [] — when the catalog could not be read at all,
    so the caller can say "unknown" rather than "no options".
    """
    api_key = (get_secret("jotform-api-key") or "").strip()
    if not api_key:
        return None
    try:
        url = (f"{_JOTFORM_API}/form/{urllib.parse.quote(form_id)}/questions"
               f"?apiKey={urllib.parse.quote(api_key)}")
        with urllib.request.urlopen(urllib.request.Request(url), timeout=15) as resp:
            questions = json.loads(resp.read().decode()).get("content") or {}
    except Exception as exc:  # network/auth/parse — the map still stands alone
        log.warning("Jotform question fetch failed for form %s: %s", form_id, exc)
        return None

    seen: dict[str, None] = {}
    for q in (questions or {}).values():
        if not isinstance(q, dict) or q.get("type") != "control_dropdown":
            continue
        # "Choose Your Location", "Choose your location (APD)", … — the location
        # dropdowns are the ones whose label says so. Other dropdowns on the same
        # form (preferred contact method, appointment time) must not be pooled in.
        if "location" not in (q.get("text") or "").lower():
            continue
        for opt in (q.get("options") or "").split("|"):
            opt = opt.strip()
            if opt:
                seen.setdefault(opt, None)
    return list(seen)


def _jotform_form_or_404(db: Session, form_id: str) -> JotformForm:
    form = db.scalars(
        select(JotformForm).where(JotformForm.jotform_form_id == form_id)
    ).first()
    if form is None:
        raise HTTPException(
            status_code=404,
            detail=f"Form {form_id} is not registered. Add it with "
                   f"POST /campaigns/{{clinic_id}} before mapping its locations.",
        )
    return form


def _location_payload(
    db: Session, form: JotformForm, names: dict[str, str], live: list[str] | None,
) -> dict:
    """One form's routing picture: its scope, its map, and what is unrouted.

    ``scope`` is derived, not stored, because it IS the presence of a map:

      "instance"  the form has location rows, so one form serves several clinics
                  and the patient's answer decides which
      "clinic"    no rows, so every submission goes to the one registry clinic

    ``needs_location_map`` is the misconfiguration worth shouting about — a form
    that ASKS for a location but has no map. Every one of its leads is being
    attributed to a single clinic while the patient is telling us otherwise.
    """
    rows = db.scalars(
        select(JotformFormLocation)
        .where(JotformFormLocation.jotform_form_id == form.jotform_form_id)
        .order_by(JotformFormLocation.option_value)
    ).all()
    mapped = {r.option_value for r in rows}
    unmapped = None if live is None else [o for o in live if o not in mapped]
    scope = "instance" if rows else "clinic"
    return {
        "jotform_form_id": form.jotform_form_id,
        "form_title": form.form_title,
        "active": bool(form.active),
        # The clinic in the webhook URL. For an instance-scoped form this is only
        # where an answer that resolves to nothing lands, NOT where its leads go.
        "default_clinic_id": form.clinic_id,
        "default_clinic_name": names.get(form.clinic_id),
        "scope": scope,
        "needs_location_map": scope == "clinic" and bool(live),
        "locations": [
            {
                "option_value": r.option_value,
                "clinic_id": r.clinic_id,
                "clinic_name": names.get(r.clinic_id) if r.clinic_id else None,
                "active": bool(r.active),
                # Mapped, but the form no longer offers it — renamed in the
                # builder, so it can never match a submission again.
                "stale": live is not None and r.option_value not in live,
            }
            for r in rows
        ],
        # Distinct clinics this form can actually deliver to, default included.
        "routes_to": sorted({
            *(r.clinic_id for r in rows if r.active and r.clinic_id),
            form.clinic_id,
        }),
        "unmapped_options": unmapped,
    }


def _instance_clinic_names(db: Session, instance_id: str) -> dict[str, str]:
    return {
        c.clinic_id: c.clinic_name
        for c in db.scalars(
            select(Clinic).where(
                Clinic.instance_id == instance_id, Clinic.deleted_at.is_(None),
            )
        ).all()
    }


@router.get("/campaigns/{instance_id}/jotform/locations")
def get_instance_jotform_locations(
    instance_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Every registered Jotform of one business, with its routing.

    One call rather than one per form: the admin UI needs the whole picture to
    answer "is this form set up for the business or for a single clinic?", and a
    per-clinic panel needs to know which OTHER clinic's form delivers to it.

    A form whose options cannot be read from Jotform still appears, with
    ``unmapped_options: null`` — an outage must not read as "fully mapped".
    """
    require_read_access(instance_id, caller)

    names = _instance_clinic_names(db, instance_id)
    forms = db.scalars(
        select(JotformForm)
        .join(Clinic, Clinic.clinic_id == JotformForm.clinic_id)
        .where(Clinic.instance_id == instance_id, Clinic.deleted_at.is_(None))
        .order_by(JotformForm.jotform_form_id)
    ).all()

    return {
        "instance_id": instance_id,
        "clinics": [{"clinic_id": cid, "clinic_name": n} for cid, n in sorted(
            names.items(), key=lambda kv: kv[1])],
        "forms": [
            _location_payload(db, f, names, _jotform_location_options(f.jotform_form_id))
            for f in forms
        ],
    }


@router.get("/campaigns/jotform/{form_id}/locations")
def get_jotform_locations(
    form_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """One form's location map, merged with the options the form actually offers.

    ``unmapped_options`` is the useful half: an option the form presents that no
    row routes is a lead that will be attributed to the form's default clinic.
    It is null (not empty) when the Jotform API could not be reached, so an
    outage is never reported as "everything is mapped".
    """
    form = _jotform_form_or_404(db, form_id)
    default_clinic = db.get(Clinic, form.clinic_id)
    require_read_access(default_clinic.instance_id, caller)

    names = _instance_clinic_names(db, default_clinic.instance_id)
    return _location_payload(db, form, names, _jotform_location_options(form_id))


@router.put("/campaigns/jotform/{form_id}/locations")
def set_jotform_locations(
    form_id: str,
    body: JotformLocationMapSet,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Replace the form's location map. Options not listed are deleted.

    Replace rather than patch for the same reason the PMS location map is a
    replace: the set is small, a partial write leaves a half-mapped form that
    routes some leads and misattributes the rest, and the caller always holds the
    whole list anyway.
    """
    form = _jotform_form_or_404(db, form_id)
    default_clinic = db.get(Clinic, form.clinic_id)
    require_write_access(default_clinic.instance_id, caller)

    clinics = {
        c.clinic_id: c
        for c in db.scalars(
            select(Clinic).where(
                Clinic.instance_id == default_clinic.instance_id,
                Clinic.deleted_at.is_(None),
            )
        ).all()
    }

    seen: set[str] = set()
    for entry in body.locations:
        option = entry.option_value.strip()
        if not option:
            raise HTTPException(status_code=400, detail="option_value cannot be blank")
        if option in seen:
            # The UNIQUE index would catch this, but as a 500-shaped IntegrityError.
            raise HTTPException(
                status_code=400,
                detail=f"Option {option!r} appears twice — an option routes to "
                       f"exactly one clinic, or routing depends on row order.",
            )
        seen.add(option)

        if entry.clinic_id and entry.clinic_id not in clinics:
            raise HTTPException(
                status_code=400,
                detail=f"Option {option!r} maps to clinic {entry.clinic_id}, which is "
                       f"not a clinic of this instance.",
            )
        # Unlike the PMS map, active-with-no-clinic is legal: it records an option
        # whose clinic has not been created yet, which is a rollout state worth
        # keeping visible. Retired-with-a-clinic is not — a later reader would
        # act on the clinic named there.
        if not entry.active and entry.clinic_id:
            raise HTTPException(
                status_code=400,
                detail=f"Option {option!r} is retired, so it must not name a clinic.",
            )

    existing = {
        r.option_value: r
        for r in db.scalars(
            select(JotformFormLocation)
            .where(JotformFormLocation.jotform_form_id == form_id)
        ).all()
    }
    for entry in body.locations:
        option = entry.option_value.strip()
        row = existing.pop(option, None)
        if row is None:
            db.add(JotformFormLocation(
                jotform_form_id=form_id,
                option_value=option,
                clinic_id=entry.clinic_id,
                active=entry.active,
            ))
        else:
            row.clinic_id = entry.clinic_id
            row.active = entry.active
    for row in existing.values():
        db.delete(row)

    return {"status": "success", "mapped": len(body.locations),
            "removed": len(existing)}
