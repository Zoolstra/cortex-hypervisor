"""
Multi-campaign ID management per clinic — backed by Cloud SQL.

The legacy `clinic_campaigns` BQ table (single table, `campaign_type`
discriminator) has been split into two typed tables:
    google_ads_campaigns  → (id, clinic_id, google_ads_campaign_id, active)
    invoca_campaigns      → (id, clinic_id, invoca_campaign_id, active)

URL shape:
    GET    /campaigns/{instance_id}                  → both types, all clinics
    GET    /campaigns/{instance_id}/{clinic_id}      → both types, one clinic
    POST   /campaigns/{clinic_id}  body{campaign_type, external_campaign_id, active}
    DELETE /campaigns/{campaign_type}/{id}           → explicit type required
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.deps import bq_client, require_read_access, require_write_access, verify_token
from api.models import ClinicCampaignCreate
from api.core.db import get_session
from api.core.orm import Clinic, GoogleAdsCampaign, Instance, InvocaCampaign


router = APIRouter()


# ── Catalog (BigQuery-backed) ────────────────────────────────────────────────

# Project/dataset that the big-query-ingestion ETL writes the catalogs into.
_CLINIC_DATA_DATASET = "project-demo-2-482101.ClinicData"


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

    Returns an empty list if the instance has no upstream account configured
    or the catalog has no matching rows yet.
    """
    require_read_access(instance_id, caller)
    if campaign_type not in ("google_ads", "invoca"):
        raise HTTPException(
            status_code=400,
            detail="campaign_type must be 'google_ads' or 'invoca'",
        )

    instance = db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

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

    return [_gads_dict(c) for c in gads] + [_invoca_dict(c) for c in invoca]


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

    return [_gads_dict(c) for c in gads] + [_invoca_dict(c) for c in invoca]


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
    else:  # invoca
        row = InvocaCampaign(
            clinic_id=clinic_id,
            invoca_campaign_id=body.external_campaign_id,
            active=body.active,
        )

    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # UNIQUE(clinic_id, external_id) — already linked.
        raise HTTPException(status_code=409, detail="Campaign already associated with this clinic")

    return {"status": "success", "id": row.id, "campaign_type": body.campaign_type}


@router.delete("/campaigns/{campaign_type}/{campaign_id}")
def remove_campaign(
    campaign_type: str,
    campaign_id: int,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Remove a campaign association. Type must be 'google_ads' or 'invoca'."""
    if campaign_type == "google_ads":
        row = db.get(GoogleAdsCampaign, campaign_id)
    elif campaign_type == "invoca":
        row = db.get(InvocaCampaign, campaign_id)
    else:
        raise HTTPException(
            status_code=400,
            detail="campaign_type must be 'google_ads' or 'invoca'",
        )

    if row is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    clinic = db.get(Clinic, row.clinic_id)
    require_write_access(clinic.instance_id, caller)

    db.delete(row)
    return {"status": "success"}
