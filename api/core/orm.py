"""
SQLAlchemy declarative models for the Cloud SQL (MySQL 8) config store.

Schema mirrors the migration plan at /root/.claude/plans/i-want-to-get-partitioned-coral.md.
All primary IDs are CHAR(36) UUIDs, all tables get created_at/updated_at audit
columns, snake_case naming, InnoDB / utf8mb4. Cascade behavior:
  - clinics ON DELETE RESTRICT against instances (clinics don't auto-die with instance)
  - all child config tables ON DELETE CASCADE against clinics
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CHAR, Column, DateTime, Enum, ForeignKey,
    ForeignKeyConstraint, Index, Integer, SmallInteger, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# "Every row of this feed belongs to one clinic" — a single-location account.
# See :class:`PmsClinicLocation`; it must never appear beside specific keys.
CATCH_ALL_LOCATION_KEY = "*"


def _audit_columns():
    """created_at + updated_at columns. MySQL maintains them via DEFAULT/ON UPDATE."""
    return (
        Column("created_at", DateTime, nullable=False, server_default=func.current_timestamp()),
        Column(
            "updated_at", DateTime, nullable=False,
            server_default=func.current_timestamp(),
            server_onupdate=func.current_timestamp(),
        ),
    )


# ─────────────────────────── instances ───────────────────────────

class Instance(Base):
    __tablename__ = "instances"

    instance_id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    instance_name: Mapped[str] = mapped_column(String(255), nullable=False)
    primary_contact_name: Mapped[str | None] = mapped_column(String(255))
    primary_contact_email: Mapped[str | None] = mapped_column(String(255))
    primary_contact_uid: Mapped[str | None] = mapped_column(String(128))
    google_ads_customer_id: Mapped[str | None] = mapped_column(String(32))
    invoca_profile_id: Mapped[str | None] = mapped_column(String(32))
    # GA account handle (alembic 0033). Filters the GA4 property picker to this
    # client's account; NULL means the admin UI falls back to manual entry.
    ga4_account_id: Mapped[str | None] = mapped_column(String(32))
    # DEPRECATED by alembic 0031 — read by nothing. Group Intelligence is derived
    # from the clinic count (api/core/grouping.py, >= 2), because a stored flag
    # restating what the data already says can disagree with it: an instance that
    # grew to four locations kept 404ing its rollup until someone remembered the
    # switch. Retained as the record of which instances had it explicitly off,
    # which is the seed for an override column if that distinction is ever wanted
    # back. Do not add readers.
    multi_location_group: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinics: Mapped[list["Clinic"]] = relationship(back_populates="instance")
    admins: Mapped[list["ClinicAdmin"]] = relationship(back_populates="instance",
                                                       cascade="all, delete-orphan")
    pms_configs: Mapped[list["InstancePmsConfig"]] = relationship(
        back_populates="instance", cascade="all, delete-orphan")


# ─────────────────────────── clinics ───────────────────────────

class Clinic(Base):
    __tablename__ = "clinics"

    clinic_id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("instances.instance_id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    clinic_name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str | None] = mapped_column(String(512))
    place_id: Mapped[str | None] = mapped_column(String(255))
    gbp_location_id: Mapped[str | None] = mapped_column(String(64))
    pms_type: Mapped[str] = mapped_column(
        Enum("blueprint", "counselear", "audit_data", "none", name="pms_type_enum"),
        nullable=False, server_default="none",
    )
    etl_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    # Service tier — drives which System-Performance KPI the Intelligence
    # Overview emphasises (bridge → revenue/clinic-hour, growth → cost/contact).
    tier: Mapped[str] = mapped_column(
        Enum("none", "bridge", "growth", name="tier_enum"),
        nullable=False, server_default="none",
    )
    country: Mapped[str | None] = mapped_column(CHAR(2))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    instance: Mapped["Instance"] = relationship(back_populates="clinics")
    location: Mapped["ClinicLocationDetails"] = relationship(
        back_populates="clinic", uselist=False, cascade="all, delete-orphan"
    )
    voice_agent: Mapped["ClinicVoiceAgentConfiguration"] = relationship(
        back_populates="clinic", uselist=False, cascade="all, delete-orphan"
    )
    blueprint_config: Mapped["ClinicBlueprintConfig"] = relationship(
        back_populates="clinic", uselist=False, cascade="all, delete-orphan"
    )
    counselear_config: Mapped["ClinicCounselEarConfig"] = relationship(
        back_populates="clinic", uselist=False, cascade="all, delete-orphan"
    )
    voice_agent_script: Mapped["ClinicVoiceAgentScript"] = relationship(
        back_populates="clinic", uselist=False, cascade="all, delete-orphan"
    )
    voice_agent_persona: Mapped["ClinicVoiceAgentPersona"] = relationship(
        back_populates="clinic", uselist=False, cascade="all, delete-orphan"
    )
    voice_agent_caller_buckets: Mapped[list["ClinicVoiceAgentCallerBucket"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    voice_agent_qualifying_questions: Mapped[list["ClinicVoiceAgentQualifyingQuestion"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    voice_agent_faqs: Mapped[list["ClinicVoiceAgentFaq"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    capabilities: Mapped[list["VoiceAgentCapability"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    protocols: Mapped[list["ClinicProtocol"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    worklist_taxonomy: Mapped["ClinicWorklistTaxonomy"] = relationship(
        back_populates="clinic", uselist=False, cascade="all, delete-orphan"
    )
    google_ads_campaigns: Mapped[list["GoogleAdsCampaign"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    invoca_campaigns: Mapped[list["InvocaCampaign"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    jotform_forms: Mapped[list["JotformForm"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    google_analytics_properties: Mapped[list["GoogleAnalyticsProperty"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    # Locations of a shared PMS account that resolve to this clinic. Distinct
    # from InstancePmsConfig.primary_clinic, hence the explicit foreign_keys.
    pms_locations: Mapped[list["PmsClinicLocation"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan",
        foreign_keys="PmsClinicLocation.clinic_id",
    )
    # Jotform location options that route to this clinic. A group's single lead
    # form fans out to every clinic in the group through these rows.
    jotform_form_locations: Mapped[list["JotformFormLocation"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan",
    )


# ──────────────────── clinic_location_details (1:1) ────────────────────

class ClinicLocationDetails(Base):
    __tablename__ = "clinic_location_details"

    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        primary_key=True,
    )
    hours_monday: Mapped[str | None] = mapped_column(String(64))
    hours_tuesday: Mapped[str | None] = mapped_column(String(64))
    hours_wednesday: Mapped[str | None] = mapped_column(String(64))
    hours_thursday: Mapped[str | None] = mapped_column(String(64))
    hours_friday: Mapped[str | None] = mapped_column(String(64))
    hours_saturday: Mapped[str | None] = mapped_column(String(64))
    hours_sunday: Mapped[str | None] = mapped_column(String(64))
    about_us: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    time_zone: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="location")


# ──────────────── clinic_voice_agent_configuration (1:1) ────────────────

class ClinicVoiceAgentConfiguration(Base):
    __tablename__ = "clinic_voice_agent_configuration"

    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        primary_key=True,
    )
    voice_agent_status: Mapped[str] = mapped_column(
        Enum("inactive", "provisioning", "active", "error", name="voice_agent_status_enum"),
        nullable=False, server_default="inactive",
    )
    twilio_phone_number: Mapped[str | None] = mapped_column(String(32))
    twilio_phone_sid: Mapped[str | None] = mapped_column(String(64))
    twilio_verified_caller_id: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0"
    )
    vapi_assistant_id: Mapped[str | None] = mapped_column(String(64))
    vapi_phone_number_id: Mapped[str | None] = mapped_column(String(64))

    # Where after-hours "take a message" tickets are pushed so a lead is never
    # lost. Nullable — an alert is best-effort and skipped when unset. SMS is the
    # V1 channel (Twilio creds already in SM); email is a forward-compat hook.
    alert_sms_to: Mapped[str | None] = mapped_column(String(32))
    alert_email_to: Mapped[str | None] = mapped_column(String(255))

    # Which agent compiler builds this clinic's assistant. 'general' = the
    # legacy stage-flow factory; other values select a single-purpose role
    # compiler in api/voice_agent/roles.py (e.g. 'annual_booking' for ACNA's
    # after-hours booking specialist).
    agent_role: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="general"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="voice_agent")


# ──────────────────── clinic_blueprint_config (1:1) ────────────────────

class ClinicBlueprintConfig(Base):
    """DEPRECATED by alembic 0030 — read by nothing.

    Blueprint config is now :class:`InstancePmsConfig` plus a
    :class:`PmsClinicLocation` row; ``prompt_for_location`` and ``user_id`` moved
    onto the mapping as ``prompt_for_location`` / ``booking_user_id``. The table
    and its rows are kept as the rollback path for 0030 and are dropped in a
    follow-up once the cutover is proven. Do not add readers.
    """
    __tablename__ = "clinic_blueprint_config"

    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        primary_key=True,
    )
    clinic_code: Mapped[str | None] = mapped_column(String(64))
    api_url: Mapped[str | None] = mapped_column(String(512))
    aws_url: Mapped[str | None] = mapped_column(String(512))
    # When TRUE the voice agent asks the caller which location to book into
    # before searching availability. Most clinics are single-location → FALSE.
    prompt_for_location: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0"
    )
    # Blueprint "user creating the appointment" for create/cancel/reschedule.
    # When NULL the adapter falls back to the booking's providerId.
    user_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="blueprint_config")


# ──────────────────── clinic_counselear_config (1:1) ────────────────────

class ClinicCounselEarConfig(Base):
    """DEPRECATED by alembic 0030 — read by nothing.

    ``counselear_location_code`` and ``counselear_sftp_username`` were
    per-practice facts duplicated onto every clinic of a practice and are now on
    :class:`InstancePmsConfig`; ``counselear_clinic_id`` is the vendor location
    key and is now :class:`PmsClinicLocation.vendor_location_key`. Kept as the
    rollback path for 0030. Do not add readers.

    Original description follows.

    Maps a CORTEX clinic to its identifiers in the CounselEar SFTP feed.

    CounselEar delivers one combined feed per *practice* (SFTP location folder),
    with each row tagged by CounselEar's own per-clinic id. Ingest needs both:
      - ``counselear_location_code`` — the ``upload/<code>/`` folder this clinic's
        files arrive under (the per-practice export id, e.g. "105333").
      - ``counselear_clinic_id`` — CounselEar's per-row "Clinic ID" (e.g. "10797"),
        the value the ETL matches against to resolve a feed row to this clinic.

    Multiple clinics of one practice share a location code but have distinct
    clinic ids, so the ETL can split a single feed across CORTEX clinics.
    """
    __tablename__ = "clinic_counselear_config"

    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        primary_key=True,
    )
    counselear_location_code: Mapped[str | None] = mapped_column(String(64))
    counselear_clinic_id: Mapped[str | None] = mapped_column(String(64))
    # SFTP login the practice's feed is delivered under (one account per
    # practice, chrooted to its upload/ root). The SFTP password lives in Secret
    # Manager under "<Username>_COUNSELEAR_SFTP_password" per provision_sftp.sh.
    counselear_sftp_username: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="counselear_config")


# ──────────────────── instance_pms_config (1 per instance+pms) ────────────────

class InstancePmsConfig(Base):
    """A PMS *account* — the only place PMS credentials live (alembic 0030).

    One PMS login can serve several physical locations, each of which is its own
    CORTEX clinic, so the credentials and feed identifiers belong to the account
    rather than to any one clinic. :class:`PmsClinicLocation` maps the vendor's
    locations to clinics and is the only per-clinic PMS config there is.

    This mirrors what the ads pipeline has always done — ``instances`` holds
    ``google_ads_customer_id`` / ``invoca_profile_id`` and a campaign table maps
    ids to clinics.

    Fields are per-vendor and the unused ones stay NULL:

        blueprint   clinic_code, api_url, aws_url
        counselear  counselear_location_code, counselear_sftp_username

    Both CounselEar fields describe the *practice* — the ``upload/<code>/`` SFTP
    folder its combined feed lands in, and the login it is delivered under. Before
    0030 they were copied identically onto every clinic row of a practice, which
    is what made the account level obviously missing.

    Secrets live in Secret Manager under
    ``instance_{instance_id}_{pms_type}_{key}``. Readers fall back to a mapped
    clinic's own ``clinic_{clinic_id}_…`` secret with a warning, so 0030 did not
    need to be sequenced against a secret copy; see
    ``scripts/copy_pms_secrets_to_instance.py``.

    ``primary_clinic_id`` receives feed rows that carry no location. Blueprint's
    patient-level tables carry only the account-wide ``branch_id``, and those
    rows have to land on exactly one clinic: replicating them across the
    account's clinics would make every patient look dormant at the locations
    they don't attend, inflating the reactivation worklist by the number of
    locations.
    """
    __tablename__ = "instance_pms_config"

    instance_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("instances.instance_id", ondelete="CASCADE"),
        primary_key=True,
    )
    pms_type: Mapped[str] = mapped_column(
        Enum("blueprint", "counselear", "audit_data", "none", name="pms_type_enum"),
        primary_key=True,
    )
    # Blueprint
    clinic_code: Mapped[str | None] = mapped_column(String(64))
    api_url: Mapped[str | None] = mapped_column(String(512))
    aws_url: Mapped[str | None] = mapped_column(String(512))
    # CounselEar — per-practice, not per-clinic (see the class docstring).
    counselear_location_code: Mapped[str | None] = mapped_column(String(64))
    counselear_sftp_username: Mapped[str | None] = mapped_column(String(64))

    primary_clinic_id: Mapped[str | None] = mapped_column(
        CHAR(36), ForeignKey("clinics.clinic_id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    instance: Mapped["Instance"] = relationship(back_populates="pms_configs")
    locations: Mapped[list["PmsClinicLocation"]] = relationship(
        back_populates="config", cascade="all, delete-orphan"
    )
    primary_clinic: Mapped["Clinic | None"] = relationship(
        foreign_keys=[primary_clinic_id]
    )


# ──────────────────── pms_clinic_locations (N) ────────────────────

class PmsClinicLocation(Base):
    """Maps one vendor location id within a PMS account to a CORTEX clinic.

    This is the ONLY per-clinic PMS configuration (alembic 0030): everything else
    is a property of the account. Which is why the two genuinely per-clinic
    settings — ``prompt_for_location`` and ``booking_user_id`` — live here rather
    than beside the credentials.

    ``vendor_location_key`` is UNIQUE per (instance, pms_type) — the same
    constraint ``jotform_forms`` puts on ``jotform_form_id``, and for the same
    reason: a location mapped to two clinics double-ingests its rows.

    Stored as a string because the id space is the vendor's, not ours —
    Blueprint uses a numeric ``location_id``, CounselEar a string clinic id —
    and the ids are neither contiguous nor dense.

    :data:`CATCH_ALL_LOCATION_KEY` (``"*"``) means "every row of this feed belongs
    to this clinic", which is what a single-location account is. It must be the
    only row for its account — a catch-all beside specific keys has no defined
    meaning, and the API rejects the combination rather than picking one.

    ``active=False`` retires a location without deleting the row, so the ETL can
    tell a site we deliberately stopped ingesting from one nobody ever mapped —
    the first is a decision, the second a wiring gap that must be reported. A
    closed site's rows keep arriving in the account's feed indefinitely, so
    conflating the two would keep every sync permanently ``partial``.

    ``clinic_id`` is therefore nullable (migration 0029), and the two fields pair:

        active=True,  clinic_id set    route this location's rows to that clinic
        active=False, clinic_id NULL   known site, deliberately not ingested

    MySQL can't portably express "NULL only when inactive" as a CHECK against
    another column, so that invariant is the application's to keep. Readers
    should treat a NULL ``clinic_id`` as retired regardless of the flag — it
    cannot be routed either way.
    """
    __tablename__ = "pms_clinic_locations"
    __table_args__ = (
        # The parent is the account config, not the instance: a map row with no
        # account credentials behind it is meaningless.
        ForeignKeyConstraint(
            ["instance_id", "pms_type"],
            ["instance_pms_config.instance_id", "instance_pms_config.pms_type"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("instance_id", "pms_type", "vendor_location_key",
                         name="uq_pms_location"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    pms_type: Mapped[str] = mapped_column(
        Enum("blueprint", "counselear", "audit_data", "none", name="pms_type_enum"),
        nullable=False,
    )
    vendor_location_key: Mapped[str] = mapped_column(String(64), nullable=False)
    clinic_id: Mapped[str | None] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    location_name: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    # Voice agent: ask the caller which site to book into. Only meaningful for a
    # clinic that fronts more than one vendor location.
    prompt_for_location: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0")
    # Blueprint "user creating the appointment" for create/cancel/reschedule.
    # NULL falls back to the booking's providerId.
    booking_user_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    config: Mapped["InstancePmsConfig"] = relationship(back_populates="locations")
    clinic: Mapped["Clinic | None"] = relationship(back_populates="pms_locations")


# ──────────────────── clinic_voice_agent_script (1:1) ────────────────────

class ClinicVoiceAgentScript(Base):
    """Editable scope-of-practice content used by the voice agent.

    All columns are free-form text — they're injected into the agent's system
    prompt at provision time. The dashboard's Voice Agent Script section
    surfaces them as labelled textareas so clinic admins can tune what the
    agent will and won't engage with.
    """
    __tablename__ = "clinic_voice_agent_script"

    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        primary_key=True,
    )
    scope_of_practice:      Mapped[str | None] = mapped_column(Text)
    services_not_offered:   Mapped[str | None] = mapped_column(Text)
    additional_notes:       Mapped[str | None] = mapped_column(Text)
    # 0007 — dropped services_offered (now derived from live Blueprint
    # appointment types), caller_needs (agent is scoped to its enabled
    # protocols; ticket intent_category is free-text), opening_overrides
    # (greeting style folded into the hardcoded Stage 1), and
    # new_patient_intake_prompt (Qualifying Questions governs new-patient
    # inquiries).
    existing_patient_intro: Mapped[str | None] = mapped_column(Text)

    # Lean-intake mode: when true, Stage 3a drops the new-patient motivation /
    # caller-bucket "discovery" machinery (price-shopper handling etc.) and the
    # agent just identifies the need and routes to booking / troubleshooting /
    # take-a-message. Used for the after-hours voicemail-replacement flow where
    # the sales-oriented daytime discovery is inappropriate. Default off so
    # existing clinics keep the full flow.
    lean_intake: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="voice_agent_script")


# ──────────────────── clinic_voice_agent_persona (1:1) ────────────────────

class ClinicVoiceAgentPersona(Base):
    """Customisable presentation layer for the voice agent.

    Defaults to ``Emma`` / ``virtual hearing assistant`` so clinics that
    never touch this row get the same behaviour they had before the model
    was introduced. ``first_message`` is null by default — the factory
    falls back to a templated greeting using ``agent_name`` + clinic name.
    """
    __tablename__ = "clinic_voice_agent_persona"

    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        primary_key=True,
    )
    agent_name:    Mapped[str] = mapped_column(String(64),  nullable=False, server_default="Emma")
    agent_title:   Mapped[str] = mapped_column(
        String(128), nullable=False, server_default="virtual hearing assistant",
    )
    voice_id:      Mapped[str] = mapped_column(String(64),  nullable=False, server_default="Emma")
    first_message: Mapped[str | None] = mapped_column(Text)
    ai_model:      Mapped[str] = mapped_column(String(64),  nullable=False, server_default="gpt-4o")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="voice_agent_persona")


# ──────────────────── clinic_voice_agent_caller_bucket (N) ────────────────────

class ClinicVoiceAgentCallerBucket(Base):
    """Per-clinic caller-intent categories with example phrases and canned
    responses, replacing the hardcoded Motivated / Price Shopper / Test-Only
    buckets. Ordered by ``ordinal`` ASC in the prompt; inactive rows hidden.
    Unseeded clinics fall back to a hardcoded default set in factory.py.
    """
    __tablename__ = "clinic_voice_agent_caller_bucket"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    ordinal:         Mapped[int]  = mapped_column(SmallInteger, nullable=False, server_default="0")
    label:           Mapped[str]  = mapped_column(String(128), nullable=False)
    example_phrases: Mapped[str | None] = mapped_column(Text)
    canned_response: Mapped[str | None] = mapped_column(Text)
    active:          Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="voice_agent_caller_buckets")


# ──────────────────── clinic_voice_agent_qualifying_question (N) ────────────────────

class ClinicVoiceAgentQualifyingQuestion(Base):
    """Per-clinic new-patient screening questions, asked during Stage 3a
    (New Patient Discovery). Each row is one question the agent asks, plus
    optional ``expected_responses`` guidance describing the answers to listen
    for. Ordered by ``ordinal`` ASC in the prompt; inactive rows hidden.

    The agent records each answer and serializes the question→answer pairs
    into the booking ``notes`` (when the patient books) and the ticket
    ``details.screening_answers`` (when they don't). Unlike caller buckets
    there is no hardcoded default set — screening is clinic-specific, so a
    clinic with no rows simply gets no screening block.
    """
    __tablename__ = "clinic_voice_agent_qualifying_question"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    ordinal:            Mapped[int]  = mapped_column(SmallInteger, nullable=False, server_default="0")
    question_text:      Mapped[str]  = mapped_column(String(512), nullable=False)
    expected_responses: Mapped[str | None] = mapped_column(Text)
    active:             Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="voice_agent_qualifying_questions")


# ──────────────────── voice_agent FAQ (N) ────────────────────

class ClinicVoiceAgentFaq(Base):
    """Per-clinic curated FAQ the agent retrieves at call time.

    Deliberately NOT prompt content. The agent reaches these through the
    ``faq_lookup`` protocol's ``answer_clinic_question`` tool, which runs a
    semantic search over ``ClinicData.faq_embeddings``. Keeping the corpus out
    of the system prompt is what lets it grow without diluting the booking
    spine (see ``api/voice_agent/roles.py`` on salience inversion).

    Approval is config, so it lives here rather than on
    ``ClinicData.faq.voice_assistant`` — see alembic 0023 for why. ``source``
    marks whether a row is a transcript-derived LLM extraction ('etl',
    imported unapproved) or human-authored ('manual').

    ``embedding_synced_at`` is the seam to BigQuery: NULL on an approved row
    means the embedding hasn't landed, so the agent cannot retrieve it yet.
    That is the drift signal — reconcile on it, not on ``approved`` alone.
    """
    __tablename__ = "clinic_voice_agent_faq"
    # Mirrors alembic 0023. Declared here too so metadata.create_all (tests)
    # enforces the same idempotency the import path relies on.
    __table_args__ = (
        UniqueConstraint("clinic_id", "question", name="uq_va_faq_clinic_question"),
        Index("ix_va_faq_clinic_approved", "clinic_id", "approved"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    question: Mapped[str] = mapped_column(String(512), nullable=False)
    answer:   Mapped[str] = mapped_column(Text, nullable=False)
    source:   Mapped[str] = mapped_column(
        Enum("etl", "manual", name="va_faq_source_enum"),
        nullable=False, server_default="manual",
    )
    source_call_id: Mapped[str | None] = mapped_column(String(128))
    approved:    Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    approved_by: Mapped[str | None] = mapped_column(String(255))
    embedding_synced_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="voice_agent_faqs")


# ──────────────────── voice_agent_capabilities (N) ────────────────────
#
# Legacy table — replaced by ``clinic_protocols`` (below) as part of the
# Protocol migration. The hypervisor dual-writes both tables for the
# transition window so a rollback to old code sees fresh data. Reads
# come from ``clinic_protocols`` only. Step 6 drops this table.

class VoiceAgentCapability(Base):
    __tablename__ = "voice_agent_capabilities"

    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        primary_key=True,
    )
    capability_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    config: Mapped[dict | None] = mapped_column(JSON)
    updated_by: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="capabilities")


# ──────────────────── clinic_protocols (N) ────────────────────
#
# Source of truth for per-clinic protocol toggles. Identical shape to
# ``voice_agent_capabilities`` (which it replaces); ``protocol_id``
# corresponds to ``Protocol.id``. The ``config`` JSON is validated against
# the protocol's ``config_model`` at write time once the first non-empty
# config_model lands (step 5 of the Protocol migration).

class ClinicProtocol(Base):
    __tablename__ = "clinic_protocols"

    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        primary_key=True,
    )
    protocol_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    config: Mapped[dict | None] = mapped_column(JSON)
    updated_by: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="protocols")


# ──────────────────── google_ads_campaigns (N) ────────────────────

class GoogleAdsCampaign(Base):
    __tablename__ = "google_ads_campaigns"
    __table_args__ = (
        UniqueConstraint("clinic_id", "google_ads_campaign_id", name="uq_clinic_gads_campaign"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    google_ads_campaign_id: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="google_ads_campaigns")


# ──────────────────── invoca_campaigns (N) ────────────────────

class InvocaCampaign(Base):
    __tablename__ = "invoca_campaigns"
    __table_args__ = (
        UniqueConstraint("clinic_id", "invoca_campaign_id", name="uq_clinic_invoca_campaign"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    invoca_campaign_id: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="invoca_campaigns")

    promo_numbers: Mapped[list["InvocaPromoNumber"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


# ──────────────────── invoca_promo_numbers (N) ────────────────────

class InvocaPromoNumber(Base):
    """Registry of Invoca promo (call-tracking) numbers per campaign.

    Campaign attribution for calls rests on "this number belongs to that
    campaign" — an invariant neither Google Ads nor Invoca enforces (numbers
    have historically been cross-wired across campaigns). This table models it
    so it can be audited: ``promo_number`` is UNIQUE globally, so a number
    routing to two campaigns is a constraint violation rather than silent
    misattribution. Synced from the Invoca API (the source of truth) and
    audited against Google Ads call assets by ``configure_promo_numbers.py``;
    not edited by hand or via the admin UI.

    ``media_type`` separates ad-extension numbers ("Google Call Extension")
    from GMB-listing / website-pool numbers; ``adwords_account_id`` is Invoca's
    record of the Google Ads account the number serves (cross-checked against
    the instance's ``google_ads_customer_id`` by the audit).
    """
    __tablename__ = "invoca_promo_numbers"
    __table_args__ = (
        UniqueConstraint("promo_number", name="uq_promo_number"),
        UniqueConstraint("invoca_promo_id", name="uq_invoca_promo_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    invoca_campaign_row_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("invoca_campaigns.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    invoca_promo_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # Digits-only NANP number as Invoca returns it (e.g. "2525761487").
    promo_number: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    promo_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    adwords_account_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    campaign: Mapped["InvocaCampaign"] = relationship(back_populates="promo_numbers")


# ──────────────────── jotform_forms (N) ────────────────────

class JotformForm(Base):
    """Registry for the Jotform → BigQuery lead pipeline — and its wiring.

    A row here (``active=1``, clinic not soft-deleted) means the ETL job
    ``jotform-ingest`` polls this form's submissions from the Jotform API every
    15 minutes into ``ClinicData.webforms`` (``cortex-data-ingestion/app/jotform/``,
    reader ``db.get_jotform_forms``). Registering IS enabling; nothing has to be
    configured on the Jotform side (the webhook relay was retired 2026-09-04).
    ``GET /webforms/coverage`` joins the registry against what has landed to
    surface a registered-but-silent form; ``configure_jotform.py`` maintains the
    hidden UTM fields and the location map.

    ``jotform_form_id`` is UNIQUE globally (not per clinic): the poller writes each
    submission once, to this DEFAULT clinic unless ``jotform_form_locations``
    re-points it, so mapping a form to two clinics would double-ingest every
    submission.
    """
    __tablename__ = "jotform_forms"
    __table_args__ = (
        UniqueConstraint("jotform_form_id", name="uq_jotform_form"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    jotform_form_id: Mapped[str] = mapped_column(String(32), nullable=False)
    form_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="jotform_forms")
    locations: Mapped[list["JotformFormLocation"]] = relationship(
        back_populates="form", cascade="all, delete-orphan",
    )


# ──────────────────── google_analytics_properties (N) ────────────────────

class GoogleAnalyticsProperty(Base):
    """Registry for the GA4 → BigQuery web-traffic pipeline (alembic 0033).

    A row here (``active=1``, clinic not soft-deleted) means the ETL job
    ``ga4-ingest`` pulls this property's daily GA4 Data API reports into
    ``ClinicData.ga4_*`` (``cortex-data-ingestion/app/ga4/``, reader
    ``db.get_ga4_properties``). Registering IS enabling.

    ``ga4_property_id`` is UNIQUE globally (Jotform semantics, not Google Ads):
    a multi-location business usually runs one property for one website, so the
    property is registered against a DEFAULT clinic and readers dedupe by
    property at the group level. Linking it to every clinic would count each
    session once per clinic. Plan: ``resources/google-analytics-integration-plan.md``.
    """
    __tablename__ = "google_analytics_properties"
    __table_args__ = (
        UniqueConstraint("ga4_property_id", name="uq_ga4_property"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    ga4_property_id: Mapped[str] = mapped_column(String(32), nullable=False)
    property_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="google_analytics_properties")


# ──────────────────── jotform_form_locations (N) ────────────────────

class JotformFormLocation(Base):
    """Maps one "choose your location" answer on a Jotform to a CORTEX clinic.

    A single form can serve every site in a group — Sense of Hearing's
    appointment-request form covers all 14 Ontario locations — but a webhook URL
    carries exactly ONE clinic_id in its path (see :class:`JotformForm`). Without
    this table every one of those submissions is attributed to whichever clinic
    the webhook happens to point at; on the Sense of Hearing form that would be
    right for 14% of leads and wrong for the other 86%.

    ``option_value`` is the dropdown answer **verbatim**, exactly as it arrives in
    the webhook's ``rawRequest`` (e.g. ``"Burlington: 11 - 1960 Appleby Line"``).
    Matching is on the literal string rather than on a parsed-out location name
    because these strings are marketing copy maintained in the Jotform builder and
    routinely disagree with our clinic names — the same form says "Limestone
    Hearing Care Centre (Kingston)" where the clinic is *Kingston*, and
    "Mississauga (Eglinton)" where the clinic is *Mississauga Central*. An
    explicit row per option is the only mapping that survives that.

    Not scoped to a field: forms reveal different location dropdowns by condition
    (this one has four — adult, 6-17, APD, 10-months-up — with overlapping option
    lists), so the resolver scans the submission's values instead of naming a
    field. See ``api/webforms.py::_resolve_location_clinic``.

    ``clinic_id`` is nullable and pairs with ``active`` exactly as
    :class:`PmsClinicLocation` does — a known option we cannot route yet is a
    different thing from an option nobody has ever mapped, and only the first is
    a decision:

        active=True,  clinic_id set    route this option's submissions there
        active=True,  clinic_id NULL   known option, clinic not created yet
        active=False                   retired option, ignore

    A NULL ``clinic_id`` is not an error: the submission falls back to the form's
    own clinic (the one in the webhook path) and the resolver logs the unmapped
    value, so it shows up as drift rather than silently vanishing.
    """
    __tablename__ = "jotform_form_locations"
    __table_args__ = (
        UniqueConstraint("jotform_form_id", "option_value", name="uq_jotform_form_location"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    jotform_form_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("jotform_forms.jotform_form_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 255 rather than TEXT so it can carry a UNIQUE index; the longest option on
    # any form today is 78 characters.
    option_value: Mapped[str] = mapped_column(String(255), nullable=False)
    clinic_id: Mapped[str | None] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    form: Mapped["JotformForm"] = relationship(back_populates="locations")
    clinic: Mapped["Clinic | None"] = relationship(back_populates="jotform_form_locations")


# ──────────────────── clinic_admins (N) ────────────────────

class ClinicAdmin(Base):
    __tablename__ = "clinic_admins"
    __table_args__ = (
        UniqueConstraint("uid", "instance_id", name="uq_uid_instance"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("instances.instance_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    instance: Mapped["Instance"] = relationship(back_populates="admins")


# ──────────────────── clinic_blueprint_entity_note (N) ────────────────────
#
# Admin free-text notes attached to Blueprint appointment types / providers
# (which live in Blueprint, not our DB — so we key on the Blueprint entity id).
# Merged into the voice-agent system prompt's Clinic Reference section.
# One row per (clinic, entity_kind, entity_id); empty notes are deleted.

class ClinicBlueprintEntityNote(Base):
    __tablename__ = "clinic_blueprint_entity_note"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)  # appointment_type | provider
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    __table_args__ = (
        UniqueConstraint("clinic_id", "entity_kind", "entity_id",
                         name="uq_clinic_entity_note"),
    )


# ──────────────────── clinic_worklist_taxonomy (1:1) ────────────────────
#
# Per-clinic definition of the reactivation worklist cohorts (tested-not-sold,
# fitted-not-sold, no-show, …). ``config`` is a JSON blob validated against
# ``api.account.worklist_taxonomy.WorklistTaxonomyConfig`` on write and hydrated
# through it on read — same validate-on-write pattern as ClinicProtocol.config.
# Which appointment event_types / statuses / invoice item_types define each
# cohort depends on the clinic's Blueprint taxonomy, so it lives per-clinic here
# rather than as hard-coded constants in intelligence_report/queries.py.

class CustomerIOEnrollment(Base):
    """Send-once log for the Customer.io database-reactivation pipeline.

    One row per (clinic, patient, cohort) — the daily sync skips any patient
    already present, so a person is enrolled into a given campaign at most
    once regardless of how many sync runs see them. ``status`` records why a
    row exists without a send: consent-blocked and no-contact patients are
    logged too, so reruns don't re-evaluate them and the pilot's funnel
    (eligible → sent) is auditable. ``dry_run`` rows are NOT written — a dry
    run must leave no state behind.
    """
    __tablename__ = "customerio_enrollments"
    __table_args__ = (
        UniqueConstraint("clinic_id", "client_id", "cohort_key",
                         name="uq_cio_enroll_clinic_client_cohort"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_key: Mapped[str] = mapped_column(String(64), nullable=False)
    event_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("sent", "blocked_consent", "no_contact",
             name="cio_enrollment_status_enum"),
        nullable=False,
    )
    enrolled_by: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship()


class ClinicWorklistTaxonomy(Base):
    __tablename__ = "clinic_worklist_taxonomy"

    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        primary_key=True,
    )
    config: Mapped[dict | None] = mapped_column(JSON)
    updated_by: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="worklist_taxonomy")
