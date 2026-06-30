"""
Clinic CRUD endpoints — backed by Cloud SQL.

A clinic spans three tables:
  - `clinics`                          — IDs / operational toggles / soft delete
  - `clinic_location_details`          — hours, about_us, phone, email, time_zone
  - `clinic_voice_agent_configuration` — managed by /clinics/{id}/voice_agent/* (untouched here)

GETs assemble a flat dict from clinics + clinic_location_details. PATCH
dispatches each field to the appropriate table based on a hard-coded mapping.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import bq_client, require_read_access, require_write_access, verify_token
from api.models import ClinicCreate, ClinicUpdate
from api.core.db import get_session
from api.core.orm import Clinic, ClinicLocationDetails, GoogleAdsCampaign, InvocaCampaign
from api.account.provisioning import provision_clinic


router = APIRouter()


# Field → owning table for PATCH dispatch.
_CLINIC_FIELDS = {"address", "place_id", "country", "gbp_location_id", "etl_enabled", "tier"}
_LOCATION_FIELDS = {
    "hours_monday", "hours_tuesday", "hours_wednesday", "hours_thursday",
    "hours_friday", "hours_saturday", "hours_sunday",
    "about_us", "email", "phone", "time_zone",
}


def _merged_dict(clinic: Clinic, loc: ClinicLocationDetails | None) -> dict:
    out = {
        "clinic_id": clinic.clinic_id,
        "instance_id": clinic.instance_id,
        "clinic_name": clinic.clinic_name,
        "address": clinic.address,
        "place_id": clinic.place_id,
        "gbp_location_id": clinic.gbp_location_id,
        "pms_type": clinic.pms_type,
        "etl_enabled": bool(clinic.etl_enabled),
        "tier": getattr(clinic, "tier", "none"),
        "country": clinic.country,
    }
    if loc:
        out.update({
            "hours_monday": loc.hours_monday,
            "hours_tuesday": loc.hours_tuesday,
            "hours_wednesday": loc.hours_wednesday,
            "hours_thursday": loc.hours_thursday,
            "hours_friday": loc.hours_friday,
            "hours_saturday": loc.hours_saturday,
            "hours_sunday": loc.hours_sunday,
            "about_us": loc.about_us,
            "email": loc.email,
            "phone": loc.phone,
            "time_zone": loc.time_zone,
        })
    return out


def _get_clinic_or_404(db: Session, clinic_id: str) -> Clinic:
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return clinic


@router.get("/clinics/{instance_id}")
def get_clinics(
    instance_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    require_read_access(instance_id, caller)

    clinics = list(db.scalars(
        select(Clinic).where(
            Clinic.instance_id == instance_id,
            Clinic.deleted_at.is_(None),
        )
    ))
    return [_merged_dict(c, c.location) for c in clinics]


# ── ETL status (per clinic) ──────────────────────────────────────────────────
# IMPORTANT: This route must be declared BEFORE the wildcard
# /clinics/{instance_id}/{clinic_id} route below — FastAPI matches routes in
# declaration order, and both have the same shape /clinics/X/Y for GET. If
# this one is declared after, the wildcard catches the request and a 404 is
# raised because "etl_status" is treated as a clinic_id lookup.

@router.get("/clinics/{clinic_id}/etl_status")
def get_etl_status(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Snapshot of what the ETL has produced for this clinic.

    Returns:
        {
          "etl_enabled": bool,
          "google_ads_campaign_ids": [str],
          "invoca_campaign_ids":     [str],
          "ad_clicks":    {"last_24h": int, "last_7d": int, "max_timestamp": iso|None},
          "transactions": {"last_24h": int, "last_7d": int, "max_timestamp": iso|None},
          "transcripts": {
              "total_calls": int,         # transactions for this clinic's campaigns w/ a complete_call_id
              "unavailable": int,         # marked .skip — no transcript from Invoca
              "callscoring_done": int,
              "faq_done": int,
              "non_conversion_done": int,
          },
        }

    Counts are scoped via the clinic's currently-active Invoca / Google Ads
    campaign IDs (from Cloud SQL). Clinics with no campaigns return zeros.
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_read_access(clinic.instance_id, caller)

    invoca_ids = [str(c) for c in db.scalars(
        select(InvocaCampaign.invoca_campaign_id).where(
            InvocaCampaign.clinic_id == clinic_id,
            InvocaCampaign.active.is_(True),
        )
    )]
    ga_ids = [str(c) for c in db.scalars(
        select(GoogleAdsCampaign.google_ads_campaign_id).where(
            GoogleAdsCampaign.clinic_id == clinic_id,
            GoogleAdsCampaign.active.is_(True),
        )
    )]

    out: dict = {
        "etl_enabled":            bool(clinic.etl_enabled),
        "google_ads_campaign_ids": ga_ids,
        "invoca_campaign_ids":     invoca_ids,
        "ad_clicks":    {"last_24h": 0, "last_7d": 0, "max_timestamp": None},
        "transactions": {"last_24h": 0, "last_7d": 0, "max_timestamp": None},
        "transcripts": {
            "total_calls":          0,
            "unavailable":          0,
            "callscoring_done":     0,
            "faq_done":             0,
            "non_conversion_done":  0,
        },
    }

    project = bq_client.project
    clinic_data = f"`{project}.ClinicData`"

    def _to_iso(ts) -> str | None:
        return ts.isoformat() if ts is not None else None

    # Google Ads clicks — scoped by campaign id.
    if ga_ids:
        in_ga = "(" + ", ".join(f"'{c}'" for c in ga_ids) + ")"
        sql = f"""
            SELECT
              COUNTIF(timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)) AS last_24h,
              COUNTIF(timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)) AS last_7d,
              MAX(timestamp) AS max_ts
            FROM {clinic_data}.ad_clicks_v2
            WHERE google_ads_campaign_id IN {in_ga}
        """
        try:
            r = next(iter(bq_client.query(sql).result()), None)
            if r:
                out["ad_clicks"] = {
                    "last_24h": int(r.last_24h or 0),
                    "last_7d":  int(r.last_7d or 0),
                    "max_timestamp": _to_iso(r.max_ts),
                }
        except Exception as e:  # noqa: BLE001
            out["ad_clicks"]["error"] = f"{type(e).__name__}: {e}"

    # Invoca transactions — scoped by invoca_campaign_id.
    if invoca_ids:
        in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_ids) + ")"
        sql = f"""
            SELECT
              COUNTIF(timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)) AS last_24h,
              COUNTIF(timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)) AS last_7d,
              COUNTIF(complete_call_id IS NOT NULL AND complete_call_id != '') AS total_calls,
              MAX(timestamp) AS max_ts
            FROM {clinic_data}.transactions
            WHERE CAST(invoca_campaign_id AS STRING) IN {in_iv}
        """
        try:
            r = next(iter(bq_client.query(sql).result()), None)
            if r:
                out["transactions"] = {
                    "last_24h": int(r.last_24h or 0),
                    "last_7d":  int(r.last_7d or 0),
                    "max_timestamp": _to_iso(r.max_ts),
                }
                out["transcripts"]["total_calls"] = int(r.total_calls or 0)
        except Exception as e:  # noqa: BLE001
            out["transactions"]["error"] = f"{type(e).__name__}: {e}"

    # Transcript analysis coverage. Scoped by complete_call_id ∈ this clinic's
    # transactions. transcript_analysis_log is keyed only by complete_call_id +
    # analysis_name (no clinic_id column), so we have to scope via the IN list.
    if invoca_ids:
        in_iv = "(" + ", ".join(f"'{c}'" for c in invoca_ids) + ")"
        sql = f"""
            WITH clinic_ccids AS (
              SELECT DISTINCT complete_call_id
              FROM {clinic_data}.transactions
              WHERE CAST(invoca_campaign_id AS STRING) IN {in_iv}
                AND complete_call_id IS NOT NULL AND complete_call_id != ''
            )
            SELECT
              (SELECT COUNT(*) FROM {clinic_data}.transcripts_unavailable u
                 WHERE u.complete_call_id IN (SELECT complete_call_id FROM clinic_ccids))
                AS unavailable,
              (SELECT COUNT(DISTINCT l.complete_call_id) FROM {clinic_data}.transcript_analysis_log l
                 WHERE l.analysis_name = 'transcript_analysis.callscoring'
                   AND l.complete_call_id IN (SELECT complete_call_id FROM clinic_ccids))
                AS callscoring_done,
              (SELECT COUNT(DISTINCT l.complete_call_id) FROM {clinic_data}.transcript_analysis_log l
                 WHERE l.analysis_name = 'transcript_analysis.faq_analysis'
                   AND l.complete_call_id IN (SELECT complete_call_id FROM clinic_ccids))
                AS faq_done,
              (SELECT COUNT(DISTINCT l.complete_call_id) FROM {clinic_data}.transcript_analysis_log l
                 WHERE l.analysis_name = 'transcript_analysis.non_conversion_analysis'
                   AND l.complete_call_id IN (SELECT complete_call_id FROM clinic_ccids))
                AS non_conversion_done
        """
        try:
            r = next(iter(bq_client.query(sql).result()), None)
            if r:
                out["transcripts"].update({
                    "unavailable":          int(r.unavailable or 0),
                    "callscoring_done":     int(r.callscoring_done or 0),
                    "faq_done":             int(r.faq_done or 0),
                    "non_conversion_done":  int(r.non_conversion_done or 0),
                })
        except Exception as e:  # noqa: BLE001
            out["transcripts"]["error"] = f"{type(e).__name__}: {e}"

    return out


@router.get("/clinics/{instance_id}/{clinic_id}")
def get_clinic(
    instance_id: str,
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    require_read_access(instance_id, caller)

    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None or clinic.instance_id != instance_id:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return _merged_dict(clinic, clinic.location)


@router.post("/clinics/{instance_id}")
def add_clinic(
    instance_id: str,
    clinic: ClinicCreate,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    require_write_access(instance_id, caller)

    clinic_id, _ = provision_clinic(
        db,
        clinic_data=clinic.model_dump(),
        instance_id=instance_id,
    )
    return {"status": "success", "clinic_id": clinic_id}


@router.patch("/clinics/{clinic_id}")
def update_clinic(
    clinic_id: str,
    body: ClinicUpdate,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided")

    # Dispatch each field to its owning table.
    location: ClinicLocationDetails | None = clinic.location
    for field, value in updates.items():
        if field in _CLINIC_FIELDS:
            setattr(clinic, field, value)
        elif field in _LOCATION_FIELDS:
            if location is None:
                # Defensive: provisioning always creates the row, but treat
                # missing as something to lazily create rather than 500.
                location = ClinicLocationDetails(clinic_id=clinic.clinic_id)
                db.add(location)
                clinic.location = location
            setattr(location, field, value)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown field: {field}")

    return {"status": "success", "updated": updates}


@router.delete("/clinics/{clinic_id}")
def delete_clinic(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    # Hard delete — child config tables CASCADE. If we want to preserve history
    # later, switch to setting deleted_at instead.
    db.delete(clinic)
    return {"status": "success"}
