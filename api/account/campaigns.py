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

URL shape:
    GET    /campaigns/{instance_id}                  → both types, all clinics
    GET    /campaigns/{instance_id}/{clinic_id}      → both types, one clinic
    POST   /campaigns/{clinic_id}  body{campaign_type, external_campaign_id, active}
    DELETE /campaigns/{campaign_type}/{id}           → explicit type required
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
from api.models import ClinicCampaignCreate
from api.core.db import get_session
from api.core.orm import Clinic, GoogleAdsCampaign, Instance, InvocaCampaign, JotformForm
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
    return {
        "id": c.id,
        "clinic_id": c.clinic_id,
        "campaign_type": "invoca",
        "external_campaign_id": c.invoca_campaign_id,
        "active": bool(c.active),
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
