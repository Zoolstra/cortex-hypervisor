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
    Integer, SmallInteger, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


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
        Enum("blueprint", "audit_data", "none", name="pms_type_enum"),
        nullable=False, server_default="none",
    )
    etl_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
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
    voice_agent_script: Mapped["ClinicVoiceAgentScript"] = relationship(
        back_populates="clinic", uselist=False, cascade="all, delete-orphan"
    )
    voice_agent_persona: Mapped["ClinicVoiceAgentPersona"] = relationship(
        back_populates="clinic", uselist=False, cascade="all, delete-orphan"
    )
    voice_agent_caller_buckets: Mapped[list["ClinicVoiceAgentCallerBucket"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    capabilities: Mapped[list["VoiceAgentCapability"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    protocols: Mapped[list["ClinicProtocol"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    google_ads_campaigns: Mapped[list["GoogleAdsCampaign"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    invoca_campaigns: Mapped[list["InvocaCampaign"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
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
    __tablename__ = "clinic_blueprint_config"

    clinic_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("clinics.clinic_id", ondelete="CASCADE"),
        primary_key=True,
    )
    clinic_code: Mapped[str | None] = mapped_column(String(64))
    api_url: Mapped[str | None] = mapped_column(String(512))
    aws_url: Mapped[str | None] = mapped_column(String(512))

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp(),
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="blueprint_config")


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
    scope_of_practice:         Mapped[str | None] = mapped_column(Text)
    services_offered:          Mapped[str | None] = mapped_column(Text)
    services_not_offered:      Mapped[str | None] = mapped_column(Text)
    caller_needs:              Mapped[str | None] = mapped_column(Text)
    additional_notes:          Mapped[str | None] = mapped_column(Text)
    # 0003 — added when the prompt builder learned to render an editable
    # opening + new-patient intake + existing-patient transition.
    opening_overrides:         Mapped[str | None] = mapped_column(Text)
    new_patient_intake_prompt: Mapped[str | None] = mapped_column(Text)
    existing_patient_intro:    Mapped[str | None] = mapped_column(Text)

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
