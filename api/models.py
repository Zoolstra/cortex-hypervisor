"""
Pydantic request/response shapes for the hypervisor API.

Persistent data shapes live in services/models.py (SQLAlchemy ORM). This
module is request-side: validation rules and the JSON body shapes the routers
accept.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, field_validator


def _require_non_empty(v: str, field_name: str) -> str:
    if not v or not v.strip():
        raise ValueError(f"{field_name} is required and cannot be empty")
    return v.strip()


def _reject_empty_string(v: Optional[str]) -> Optional[str]:
    if v is not None and not v.strip():
        raise ValueError("Field cannot be an empty string")
    return v.strip() if v is not None else v


# ── Create models ─────────────────────────────────────────────────────────────

class InstanceCreate(BaseModel):
    instance_name: str
    primary_contact_name: str
    primary_contact_email: str

    @field_validator("instance_name")
    @classmethod
    def _v_name(cls, v):
        return _require_non_empty(v, "instance_name")

    @field_validator("primary_contact_name")
    @classmethod
    def _v_pcn(cls, v):
        return _require_non_empty(v, "primary_contact_name")

    @field_validator("primary_contact_email")
    @classmethod
    def _v_pce(cls, v):
        return _require_non_empty(v, "primary_contact_email")


class ClinicCreate(BaseModel):
    ref_id: Optional[str] = None  # client-provided handle for caller-side bookkeeping
    clinic_name: str
    address: str
    place_id: str
    about_us: str
    hours_monday: str
    hours_tuesday: str
    hours_wednesday: str
    hours_thursday: str
    hours_friday: str
    hours_saturday: str
    hours_sunday: str
    phone: str
    time_zone: str
    country: str

    @field_validator("clinic_name")
    @classmethod
    def _v_name(cls, v):
        return _require_non_empty(v, "clinic_name")

    @field_validator("address")
    @classmethod
    def _v_addr(cls, v):
        return _require_non_empty(v, "address")

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v):
        return _require_non_empty(v, "phone")


# ── Update models ─────────────────────────────────────────────────────────────

class InstanceUpdate(BaseModel):
    primary_contact_name: Optional[str] = None
    primary_contact_email: Optional[str] = None
    google_ads_customer_id: Optional[str] = None
    invoca_profile_id: Optional[str] = None

    @field_validator(
        "primary_contact_name", "primary_contact_email",
        "google_ads_customer_id", "invoca_profile_id",
    )
    @classmethod
    def _v(cls, v):
        return _reject_empty_string(v)


class ClinicUpdate(BaseModel):
    address: Optional[str] = None
    place_id: Optional[str] = None
    about_us: Optional[str] = None
    hours_monday: Optional[str] = None
    hours_tuesday: Optional[str] = None
    hours_wednesday: Optional[str] = None
    hours_thursday: Optional[str] = None
    hours_friday: Optional[str] = None
    hours_saturday: Optional[str] = None
    hours_sunday: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    time_zone: Optional[str] = None
    country: Optional[str] = None
    gbp_location_id: Optional[str] = None
    etl_enabled: Optional[bool] = None

    @field_validator(
        "address", "phone", "email", "place_id", "about_us",
        "time_zone", "country", "gbp_location_id",
        "hours_monday", "hours_tuesday", "hours_wednesday", "hours_thursday",
        "hours_friday", "hours_saturday", "hours_sunday",
    )
    @classmethod
    def _v(cls, v):
        return _reject_empty_string(v)


# ── Composite shapes used by /provision_account/ ──────────────────────────────

class ProvisionRequest(BaseModel):
    uid: str
    instance: InstanceCreate
    clinics: List[ClinicCreate]


# ── Campaigns ─────────────────────────────────────────────────────────────────

class ClinicCampaignCreate(BaseModel):
    campaign_type: Literal["google_ads", "invoca"]
    external_campaign_id: str
    active: bool = True

    @field_validator("external_campaign_id")
    @classmethod
    def _v_ext_id(cls, v):
        return _require_non_empty(v, "external_campaign_id")


# ── PMS Config ────────────────────────────────────────────────────────────────

class PmsConfigSet(BaseModel):
    """
    Sets the PMS configuration for a clinic.

    Non-secret config goes in the `config` field. Shape depends on pms_type:
      blueprint  → {"clinic_code": str, "api_url": str, "aws_url": str}
      audit_data → reserved (table not yet created)
      none       → ignored

    Secrets are passed in `secrets` and stored in Secret Manager under
    `clinic_{clinic_id}_blueprint_{key}` for Blueprint clinics. Never in the DB.
    """
    pms_type: Literal["none", "blueprint", "audit_data"]
    config: Optional[dict] = None
    secrets: Optional[dict] = None
