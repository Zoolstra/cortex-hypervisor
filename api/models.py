"""
Pydantic request/response shapes for the hypervisor API.

Persistent data shapes live in services/models.py (SQLAlchemy ORM). This
module is request-side: validation rules and the JSON body shapes the routers
accept.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


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
    # Renaming is safe: `instance_name` is a display label everywhere it is
    # read (reports, pickers, mart row labels) and is never a join key — scope
    # and every FK resolve through `instance_id`.
    instance_name: Optional[str] = None
    primary_contact_name: Optional[str] = None
    primary_contact_email: Optional[str] = None
    google_ads_customer_id: Optional[str] = None
    invoca_profile_id: Optional[str] = None
    ga4_account_id: Optional[str] = None
    # multi_location_group was here. It is now derived from the clinic count
    # (api/core/grouping.py) rather than stored, because a flag restating what the
    # data already says can disagree with it — and did: an instance that grew to
    # four locations kept 404ing its rollup until someone remembered the switch.

    @field_validator(
        "instance_name", "primary_contact_name", "primary_contact_email",
        "google_ads_customer_id", "invoca_profile_id",
    )
    @classmethod
    def _v(cls, v):
        return _reject_empty_string(v)

    @field_validator("ga4_account_id")
    @classmethod
    def _v_ga4(cls, v):
        # The SPA form sends "" when the field is left blank. Blank normalises
        # to None (= "not provided", so the PATCH leaves the column alone)
        # rather than 422ing the whole update, mirroring the blank→NULL rule
        # provisioning applies to the other two upstream ids.
        if v is None:
            return None
        v = v.strip()
        return v or None


class ClinicUpdate(BaseModel):
    # Renaming is safe for the same reason as `instance_name`: every consumer
    # of `clinic_name` treats it as a label (reports, voice-agent prompt copy,
    # the marts' clinic dimension, the `webforms` stamp), and every key is
    # `clinic_id`. NOTE the two places a rename is not retroactive — the live
    # VAPI assistant keeps the old name in its prompt until republished, and
    # `ClinicData.webforms` rows already written keep the name they were
    # stamped with, which is a point-in-time record rather than a stale copy.
    clinic_name: Optional[str] = None
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
    tier: Optional[Literal["none", "bridge", "growth"]] = None

    @field_validator(
        "clinic_name", "address", "phone", "email", "place_id", "about_us",
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
    campaign_type: Literal["google_ads", "invoca", "jotform", "google_analytics"]
    external_campaign_id: str
    active: bool = True

    @field_validator("external_campaign_id")
    @classmethod
    def _v_ext_id(cls, v):
        return _require_non_empty(v, "external_campaign_id")


# ── PMS Config ────────────────────────────────────────────────────────────────

class PmsLocationEntry(BaseModel):
    """One vendor location of a shared PMS account, mapped to a clinic.

    ``vendor_location_key`` is the vendor's own id for the site as a string —
    Blueprint uses a numeric ``location_id``, CounselEar a string clinic id, and
    neither space is ours to renumber.

    The two states, which the ETL treats very differently:

      active=True,  clinic_id set    route this location's rows to that clinic
      active=False, clinic_id null   known site, deliberately not ingested

    The second is how a closed location is recorded. Its rows keep arriving in
    the account's feed indefinitely, and the ETL has to tell them apart from a
    site nobody mapped — the latter is a wiring gap that must be reported, the
    former a decision already taken. Retiring rather than deleting the row is
    what preserves that distinction.
    """
    vendor_location_key: str = Field(min_length=1, max_length=64)
    clinic_id: str | None = None
    location_name: str | None = Field(default=None, max_length=255)
    active: bool = True
    # The only genuinely per-clinic PMS settings, which is why they ride on the
    # mapping rather than beside the account's credentials.
    prompt_for_location: bool = False
    booking_user_id: int | None = None


class PmsLocationImportEntry(BaseModel):
    """One PMS location to import as a clinic.

    Either names an existing clinic (`clinic_id`) or asks for one to be created
    from `clinic_name` — falling back to `location_name`, since that is what the
    PMS calls the site. Blueprint leaves some locations unnamed, so those need an
    explicit `clinic_name`.
    """
    vendor_location_key: str = Field(min_length=1, max_length=64)
    location_name: str | None = Field(default=None, max_length=255)
    clinic_id: str | None = None
    clinic_name: str | None = Field(default=None, max_length=255)
    address: str | None = None
    country: str | None = Field(default=None, max_length=2)
    time_zone: str | None = None


class PmsLocationImport(BaseModel):
    """Create clinics from what the PMS reports and map them.

    The step that lets onboarding start from the PMS: an instance is provisioned
    with its account config and no clinics, then the clinics come from the sites
    the PMS says exist.
    """
    pms_type: Literal["blueprint", "counselear"]
    locations: list[PmsLocationImportEntry]


class JotformLocationEntry(BaseModel):
    """One "choose your location" answer on a Jotform, mapped to a clinic.

    ``option_value`` is the dropdown answer **verbatim** as it arrives in the
    webhook — ``"Burlington: 11 - 1960 Appleby Line"``, address and all. It is
    not a location name we parse out: these strings are maintained in the Jotform
    builder and routinely disagree with our clinic names, so the literal answer
    is the only stable key.

    The three states:

      active=True,  clinic_id set    route this option's submissions there
      active=True,  clinic_id null   known option, clinic not created yet
      active=False, clinic_id null   retired option, ignore

    The middle state is the one that differs from :class:`PmsLocationEntry`,
    where an active row must route somewhere. A group's form lists every site
    from day one while the clinics are created over days or weeks, and recording
    the option with no clinic is what makes that gap visible — the submission
    falls back to the form's own clinic and the resolver logs it.
    """
    option_value: str = Field(min_length=1, max_length=255)
    clinic_id: str | None = None
    active: bool = True


class JotformLocationMapSet(BaseModel):
    """Replace a form's whole location map. Omitted options are deleted."""
    locations: list[JotformLocationEntry]


class InstancePmsConfigSet(BaseModel):
    """
    Sets the PMS configuration for an *account* — one PMS login serving several
    physical sites, each its own clinic.

    `config` is per-vendor and validated against that vendor's field list:

        blueprint    clinic_code, api_url, aws_url
        counselear   counselear_location_code, counselear_sftp_username

    `secrets` are stored under `instance_{instance_id}_{pms_type}_{key}`.
    CounselEar accepts none — its secrets are named after the SFTP login, which
    is itself account config here, so no rename of live credentials is needed.

    `primary_clinic_id` receives feed rows that carry no location at all. Most of
    Blueprint's tables are like this — only appointments and invoices identify a
    site, while the patient-level tables carry an account-wide branch id. Those
    rows must land on exactly one clinic: copying them to all of them makes every
    patient look dormant at the locations they don't attend, inflating the
    reactivation worklist by the number of sites.

    `locations` REPLACES the whole map when present, and is left untouched when
    omitted — so the editor can save credentials without having to resend the
    map, and clearing the map is explicit (send `[]`) rather than a side effect
    of a partial save.
    """
    pms_type: Literal["blueprint", "counselear"]
    config: dict | None = None
    secrets: dict | None = None
    primary_clinic_id: str | None = None
    locations: list[PmsLocationEntry] | None = None


class CustomerIOConfigSet(BaseModel):
    """Sets a clinic's Customer.io workspace credentials (one workspace per
    clinic). All fields optional so site ID / API key / region can be rotated
    independently — blank keeps the existing value. Stored in Secret Manager
    as ``customerio-site-id-<clinic_id>`` etc.; never in the DB, never
    returned by any endpoint."""
    site_id: Optional[str] = None
    track_api_key: Optional[str] = None
    region: Optional[Literal["us", "eu"]] = None


# ── Webforms ──────────────────────────────────────────────────────────────────

class WebformSubmission(BaseModel):
    """A single web-form submission relayed from one of our clinic sites.

    Posted server-to-server to ``POST /webforms`` with the global
    ``X-Webform-Secret`` header. ``clinic_id`` routes the row to a clinic
    (validated against Cloud SQL before any write); every other field is
    optional so partial captures still land. Blank strings are normalised to
    None so BigQuery stores NULL rather than "".
    """
    clinic_id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    utm_term: Optional[str] = None
    utm_content: Optional[str] = None
    gclid: Optional[str] = None          # Google Ads click id
    fbclid: Optional[str] = None         # Meta click id
    # Google sends gbraid (cross-device) or wbraid (iOS post-ATT) INSTEAD of a
    # gclid on many clicks; gad_campaignid is the campaign id itself. All three
    # are optional here because the relay may not capture them — the ingest
    # falls back to parsing them out of `landing_page` (webforms._attribution).
    gbraid: Optional[str] = None
    wbraid: Optional[str] = None
    gad_campaignid: Optional[str] = None
    # Referring site host. Sites currently send this INSIDE utm_source when the
    # visit had no UTM tags; the ingest splits it back out (webforms._utm), so
    # relays may either send it here explicitly or keep doing what they do.
    referrer_host: Optional[str] = None
    landing_page: Optional[str] = None
    customer_type: Optional[str] = None  # e.g. "New Customer" / "Returning Customer"
    message: Optional[str] = None        # free-text "How can we help?" intent

    @field_validator("clinic_id")
    @classmethod
    def _v_clinic_id(cls, v):
        return _require_non_empty(v, "clinic_id")

    @field_validator(
        "first_name", "last_name", "phone_number", "email",
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid", "gbraid", "wbraid", "gad_campaignid",
        "referrer_host", "landing_page",
        "customer_type", "message",
    )
    @classmethod
    def _v_optional(cls, v):
        """Trim whitespace; collapse blank strings to None."""
        if v is None:
            return None
        v = v.strip()
        return v or None
