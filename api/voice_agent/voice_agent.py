"""
Voice agent lifecycle + capability toggles + ticket ingest.

Backed by Cloud SQL for the operational state (status, twilio_*, vapi_*) and
the capability toggles. The submit_ticket endpoint still writes to BigQuery
(`Users.voice_agent_tickets`) — call outcomes are analytics-shaped, append-only,
and the BQ-vs-Cloud-SQL boundary established by the migration plan keeps them
on the analytics side.

The previous activation gate (`services.script_approval.require_full_approval`)
is intentionally removed: the underlying `Users.agent_script_sections` table is
being dropped as part of the transcript-analysis rebuild. The new voice-agent
system (whatever replaces voice_agent_builder/) will reintroduce its own gate.

TODO (Round 3): wire activate/deactivate/verify_caller_id to Twilio + VAPI via
services/twilio_client.py and services/vapi_provisioner.py.
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Literal

log = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, Header, HTTPException
from google.cloud import bigquery
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import (
    PROJECT, bq_client, bq_table, require_read_access, require_write_access,
    verify_token,
)
from api.services import notify
from api.voice_agent import vapi as vapi_client
from api.voice_agent.blueprint import verify_vapi_secret
from api.core.db import get_session
from api.core.orm import (
    Clinic, ClinicProtocol, ClinicVoiceAgentCallerBucket,
    ClinicVoiceAgentConfiguration, ClinicVoiceAgentFaq,
    ClinicVoiceAgentPersona, ClinicVoiceAgentQualifyingQuestion,
    ClinicVoiceAgentScript, VoiceAgentCapability,
)
from api.voice_agent.protocols import (
    PROTOCOL_METADATA as CAPABILITY_METADATA,
    PROTOCOL_METADATA_BY_ID as CAPABILITY_METADATA_BY_ID,
    is_clinic_compatible,
    is_pms_compatible,
    unmet_dependencies,
)
from api.voice_agent.factory import build_agent_config, build_first_message


router = APIRouter()

# Transcript-extracted FAQ suggestions written by the ETL. Fully qualified,
# because it lives in ClinicData — `bq_table()` hardcodes the Users dataset and
# is only correct for the Users-resident tables here (voice_agent_tickets).
# Using it for this table produced `Users.faq`, which does not exist, so the
# import endpoint 404'd inside BigQuery and surfaced as a bare 500.
# Same convention as faq_retrieval.FAQ_EMBEDDINGS_TABLE / webforms.WEBFORMS_TABLE.
FAQ_SUGGESTIONS_TABLE = f"{PROJECT}.ClinicData.faq"


def _get_clinic_or_404(db: Session, clinic_id: str) -> Clinic:
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return clinic


def _get_voice_agent_or_create(db: Session, clinic_id: str) -> ClinicVoiceAgentConfiguration:
    """Voice-agent config row should always exist (provisioned with the clinic).
    Defensively create on demand for clinics imported before that invariant held."""
    va = db.get(ClinicVoiceAgentConfiguration, clinic_id)
    if va is None:
        va = ClinicVoiceAgentConfiguration(clinic_id=clinic_id)
        db.add(va)
    return va


def _sync_assistant_if_provisioned(db: Session, clinic: Clinic) -> dict:
    """If the clinic has a live VAPI assistant, rebuild its config from
    Cloud SQL and push the update. No-op when the agent hasn't been
    provisioned yet. VAPI errors are caught and logged so they cannot fail
    the caller's primary DB write.

    Returns one of:
      ``{"synced": True}``
      ``{"synced": False, "reason": "voice_agent_not_provisioned"}``
      ``{"synced": False, "error": "<ClassName>: <message>"}``
    """
    va = db.get(ClinicVoiceAgentConfiguration, clinic.clinic_id)
    if va is None or not va.vapi_assistant_id:
        return {"synced": False, "reason": "voice_agent_not_provisioned"}
    try:
        config = build_agent_config(db, clinic)
        vapi_client.update_assistant(va.vapi_assistant_id, config)
        return {"synced": True}
    except Exception as e:  # noqa: BLE001
        log.exception(
            "VAPI sync failed for clinic_id=%s assistant_id=%s",
            clinic.clinic_id, va.vapi_assistant_id,
        )
        return {"synced": False, "error": f"{type(e).__name__}: {e}"}


@router.get("/clinics/{clinic_id}/voice_agent")
def get_voice_agent(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Read-only snapshot of the voice-agent state for one clinic.

    Returns ``voice_agent_status``, Twilio + VAPI identifiers, and the
    clinic's ``pms_type`` so the dashboard can render the status panel and
    PMS-gate the capability toggles in a single fetch. Idempotent — no row
    is created on read.
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_read_access(clinic.instance_id, caller)

    va = db.get(ClinicVoiceAgentConfiguration, clinic_id)
    return {
        "clinic_id": clinic_id,
        "pms_type": clinic.pms_type or "none",
        "voice_agent_status": (va.voice_agent_status if va else "inactive"),
        "twilio_phone_number": (va.twilio_phone_number if va else None),
        "twilio_verified_caller_id": bool(va.twilio_verified_caller_id) if va else False,
        "vapi_assistant_id": (va.vapi_assistant_id if va else None),
        "vapi_phone_number_id": (va.vapi_phone_number_id if va else None),
        "updated_at": _isoformat(va.updated_at) if va else None,
    }


@router.post("/clinics/{clinic_id}/voice_agent/activate")
def activate_voice_agent(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    Provision (or re-provision) the clinic's VAPI assistant.

    Destructive-recreate semantics: if a VAPI assistant already exists for
    this clinic, it is deleted first; a fresh assistant is then created from
    the current factory config and its id is stored. Twilio number purchase
    is out of scope here — numbers are attached separately in the VAPI
    dashboard.
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    va = _get_voice_agent_or_create(db, clinic_id)

    deleted_assistant_id: str | None = None
    if va.vapi_assistant_id:
        deleted_assistant_id = va.vapi_assistant_id
        try:
            vapi_client.delete_assistant(va.vapi_assistant_id)
        except Exception:
            log.exception(
                "Failed to delete existing VAPI assistant %s for clinic %s; proceeding with create",
                va.vapi_assistant_id, clinic_id,
            )
        va.vapi_assistant_id = None

    try:
        config = build_agent_config(db, clinic)
        new_assistant_id = vapi_client.create_assistant(config)
    except Exception as e:
        va.voice_agent_status = "error"
        log.exception("VAPI create_assistant failed for clinic %s", clinic_id)
        raise HTTPException(
            status_code=502,
            detail=f"VAPI create_assistant failed: {type(e).__name__}: {e}",
        )

    va.vapi_assistant_id = new_assistant_id
    va.voice_agent_status = "active"

    return {
        "status": "active",
        "clinic_id": clinic_id,
        "vapi_assistant_id": new_assistant_id,
        "deleted_assistant_id": deleted_assistant_id,
        "message": (
            f"Re-provisioned VAPI assistant (new id: {new_assistant_id})."
            if deleted_assistant_id
            else f"Provisioned new VAPI assistant (id: {new_assistant_id})."
        ),
    }


@router.post("/clinics/{clinic_id}/voice_agent/assistant")
def upsert_voice_agent_assistant(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    Create or update the VAPI assistant for this clinic from the current
    factory config (system prompt, tools, voice, transcriber).

    Idempotent: first call creates the assistant and stores `vapi_assistant_id`;
    subsequent calls update the existing assistant in place. Independent of
    `voice_agent_status` — used to iterate on the prompt without touching
    Twilio provisioning.
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    va = _get_voice_agent_or_create(db, clinic_id)
    config = build_agent_config(db, clinic)

    if va.vapi_assistant_id:
        vapi_client.update_assistant(va.vapi_assistant_id, config)
        action = "updated"
    else:
        va.vapi_assistant_id = vapi_client.create_assistant(config)
        action = "created"

    return {
        "action": action,
        "clinic_id": clinic_id,
        "vapi_assistant_id": va.vapi_assistant_id,
    }


@router.delete("/clinics/{clinic_id}/voice_agent")
def deactivate_voice_agent(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    Deactivate and deprovision the voice agent for this clinic.

    Deletes the live VAPI assistant (best-effort — 404 from VAPI is treated
    as already-gone) and clears the DB pointers. Twilio number release is
    still out of scope.
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    va = _get_voice_agent_or_create(db, clinic_id)
    if va.voice_agent_status == "inactive":
        raise HTTPException(status_code=409, detail="Voice agent is not active for this clinic")

    deleted_assistant_id: str | None = None
    if va.vapi_assistant_id:
        deleted_assistant_id = va.vapi_assistant_id
        try:
            vapi_client.delete_assistant(va.vapi_assistant_id)
        except Exception:
            log.exception(
                "Failed to delete VAPI assistant %s for clinic %s during deactivate",
                va.vapi_assistant_id, clinic_id,
            )

    va.voice_agent_status = "inactive"
    va.twilio_phone_number = None
    va.twilio_phone_sid = None
    va.twilio_verified_caller_id = False
    va.vapi_assistant_id = None
    va.vapi_phone_number_id = None

    return {
        "status": "success",
        "clinic_id": clinic_id,
        "voice_agent_status": "inactive",
        "deleted_assistant_id": deleted_assistant_id,
    }


@router.post("/clinics/{clinic_id}/voice_agent/verify_caller_id")
def verify_caller_id(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    Initiate Twilio outbound caller ID verification for the clinic's primary phone.

    TODO (Round 3): Implement via services/twilio_client.initiate_caller_id_verification.
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    va = _get_voice_agent_or_create(db, clinic_id)
    if va.voice_agent_status not in ("active", "provisioning"):
        raise HTTPException(
            status_code=400,
            detail="Voice agent must be activated before verifying caller ID",
        )

    raise HTTPException(
        status_code=501,
        detail="Twilio caller ID verification not yet implemented — coming in Round 3",
    )


# ── VAPI-authed: submit_ticket (writes to BigQuery) ──────────────────────────


class TicketSubmitRequest(BaseModel):
    vapi_call_id: str | None = None
    caller_phone: str | None = None
    caller_name: str | None = None
    patient_match_status: Literal["matched", "unmatched", "new", "ambiguous"]
    blueprint_patient_id: str | None = None
    last4_confirmed: bool = False
    intent_category: str | None = None
    summary: str | None = None
    details: dict | None = None
    suggested_followup: str | None = None
    urgency: Literal["normal", "urgent"] = "normal"


class TicketSubmitResponse(BaseModel):
    ticket_id: str


@router.post(
    "/clinics/{clinic_id}/voice_agent/tickets",
    response_model=TicketSubmitResponse,
)
def submit_ticket(
    clinic_id: str,
    body: TicketSubmitRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
    x_vapi_caller_number: str | None = Header(default=None),
):
    """
    Called by VAPI's submit_ticket tool at the end of a voice call.

    Validates the clinic exists in Cloud SQL, then appends one row to
    `Users.voice_agent_tickets` in BigQuery (analytics store, intentionally
    separate from the operational config in Cloud SQL). Finally fires a
    best-effort staff alert so an after-hours message is never a lost lead.

    Callback number resolution: the agent-transcribed ``caller_phone`` is
    preferred (the caller may give a different callback number than they're
    calling from), falling back to VAPI's caller-ID (the ``X-Vapi-Caller-Number``
    header, populated from the ``{{customer.number}}`` template on the tool) so a
    number is captured even when the agent didn't get one verbally.
    """
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")

    callback_number = body.caller_phone or x_vapi_caller_number
    ticket_id = str(uuid.uuid4())
    details_json = json.dumps(body.details) if body.details is not None else None

    bq_client.query(
        f"""
        INSERT INTO {bq_table('voice_agent_tickets')} (
          ticket_id, clinic_id, vapi_call_id, created_at, caller_phone, caller_name,
          patient_match_status, blueprint_patient_id, last4_confirmed, intent_category,
          summary, details, suggested_followup, urgency, status
        ) VALUES (
          @ticket_id, @clinic_id, @vapi_call_id, CURRENT_TIMESTAMP(), @caller_phone, @caller_name,
          @patient_match_status, @blueprint_patient_id, @last4_confirmed, @intent_category,
          @summary, @details, @suggested_followup, @urgency, 'open'
        )
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("ticket_id", "STRING", ticket_id),
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
            bigquery.ScalarQueryParameter("vapi_call_id", "STRING", body.vapi_call_id),
            bigquery.ScalarQueryParameter("caller_phone", "STRING", callback_number),
            bigquery.ScalarQueryParameter("caller_name", "STRING", body.caller_name),
            bigquery.ScalarQueryParameter("patient_match_status", "STRING", body.patient_match_status),
            bigquery.ScalarQueryParameter("blueprint_patient_id", "STRING", body.blueprint_patient_id),
            bigquery.ScalarQueryParameter("last4_confirmed", "BOOL", body.last4_confirmed),
            bigquery.ScalarQueryParameter("intent_category", "STRING", body.intent_category),
            bigquery.ScalarQueryParameter("summary", "STRING", body.summary),
            bigquery.ScalarQueryParameter("details", "STRING", details_json),
            bigquery.ScalarQueryParameter("suggested_followup", "STRING", body.suggested_followup),
            bigquery.ScalarQueryParameter("urgency", "STRING", body.urgency),
        ])
    ).result()

    # Best-effort staff alert so an after-hours message is a captured lead, not
    # a fire-and-forget BigQuery row. Never allowed to fail the ticket write.
    try:
        va = db.get(ClinicVoiceAgentConfiguration, clinic_id)
        notify.notify_new_ticket(
            clinic_name=clinic.clinic_name,
            alert_sms_to=va.alert_sms_to if va else None,
            alert_email_to=va.alert_email_to if va else None,
            caller_name=body.caller_name,
            callback_number=callback_number,
            intent_category=body.intent_category,
            summary=body.summary,
            urgency=body.urgency,
        )
    except Exception:
        log.exception("submit_ticket: staff alert failed for clinic_id=%s", clinic_id)

    return TicketSubmitResponse(ticket_id=ticket_id)


# ── Capability toggles ────────────────────────────────────────────────────────


class CapabilityItem(BaseModel):
    id: str
    display_name: str
    description: str
    supported_pms: list[str] | None
    pms_compatible: bool
    enabled: bool
    updated_at: str | None = None
    updated_by: str | None = None
    # Other protocol ids this protocol depends on (informational — same
    # list on every clinic; the field is here so the frontend doesn't
    # need to ship a parallel dep table).
    depends_on: list[str] = []
    # Subset of ``depends_on`` whose corresponding protocol is NOT
    # currently enabled for THIS clinic. Empty when satisfied. The agent
    # sync drops a protocol with unmet deps, so a non-empty list means
    # the toggle is "on but not effective" — the admin UI should flag it.
    unmet_dependencies: list[str] = []
    # JSON Schema (Pydantic .model_json_schema()) for this protocol's
    # config_model. The frontend renders an editor from this; an empty
    # ``properties`` map means the protocol has no per-clinic knobs and
    # the Configure UI is hidden.
    config_schema: dict = {}
    # Current per-clinic config (validated against config_schema on
    # write). Defaults from the model when no row / null column.
    config: dict = {}
    # True when the clinic's agent_role hard-requires this protocol — the
    # role compiler includes it unconditionally, so its toggle is locked
    # (disable would be a silent no-op on the live agent). The UI should
    # render it as always-on-for-this-clinic.
    locked_by_role: bool = False
    # Populated only on write operations (toggle). Indicates whether the
    # change propagated to the live VAPI assistant. Absent on list reads.
    vapi_sync: dict | None = None


class CapabilitiesListResponse(BaseModel):
    clinic_id: str
    pms_type: str
    capabilities: list[CapabilityItem]


class CapabilityToggleRequest(BaseModel):
    enabled: bool
    config: dict | None = None


def _isoformat(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get(
    "/clinics/{clinic_id}/voice_agent/capabilities",
    response_model=CapabilitiesListResponse,
)
def list_capabilities(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    List toggleable voice-agent capabilities with per-clinic enablement state.
    Always-on capabilities are excluded.
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)  # read gated by write

    from api.voice_agent.roles import role_required_protocol_ids  # noqa: PLC0415
    locked_ids = role_required_protocol_ids(clinic)

    pms_type = clinic.pms_type or "none"

    # Read from clinic_protocols — the new source of truth as of step 3 of the
    # Protocol migration. The legacy `voice_agent_capabilities` table is still
    # dual-written so a rollback to old code stays consistent, but no read
    # paths consult it from this revision on.
    rows = list(db.scalars(
        select(ClinicProtocol).where(ClinicProtocol.clinic_id == clinic_id)
    ))
    state = {r.protocol_id: r for r in rows}

    # Snapshot of enabled ids for the clinic — used to compute each
    # protocol's unmet_dependencies as we render the response.
    enabled_ids = {r.protocol_id for r in rows if r.enabled}

    items: list[CapabilityItem] = []
    for cap in CAPABILITY_METADATA:
        if cap.always_on:
            continue
        # Clinic-scoped protocols (e.g. ACNA's placeholder-grid ones) only
        # surface for the clinics they're restricted to.
        if not is_clinic_compatible(cap, clinic_id):
            continue
        row = state.get(cap.id)
        # Effective config = persisted row (if any) merged through the
        # protocol's defaults via Pydantic. ``model_dump()`` gives the
        # canonical shape the frontend renders against the schema.
        try:
            cfg_obj = cap.config_model(**(row.config or {})) if row and row.config else cap.config_model()
        except Exception:
            # Stored config no longer parses (schema tightened since write).
            # Fall back to defaults so the UI still loads; the operator can
            # re-save through the form.
            cfg_obj = cap.config_model()
        items.append(CapabilityItem(
            id=cap.id,
            display_name=cap.display_name,
            description=cap.description,
            supported_pms=list(cap.supported_pms) if cap.supported_pms is not None else None,
            pms_compatible=is_pms_compatible(cap, pms_type),
            enabled=bool(row.enabled) if row else False,
            updated_at=_isoformat(row.updated_at) if row else None,
            updated_by=row.updated_by if row else None,
            depends_on=list(cap.depends_on),
            unmet_dependencies=unmet_dependencies(cap.id, enabled_ids),
            config_schema=cap.config_model.model_json_schema(),
            config=cfg_obj.model_dump(),
            locked_by_role=cap.id in locked_ids,
        ))

    return CapabilitiesListResponse(clinic_id=clinic_id, pms_type=pms_type, capabilities=items)


@router.put(
    "/clinics/{clinic_id}/voice_agent/capabilities/{capability_id}",
    response_model=CapabilityItem,
)
def toggle_capability(
    clinic_id: str,
    capability_id: str,
    body: CapabilityToggleRequest,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """
    Upsert a capability toggle for this clinic.
    """
    cap = CAPABILITY_METADATA_BY_ID.get(capability_id)
    if cap is None:
        raise HTTPException(status_code=404, detail=f"Unknown capability: {capability_id}")
    if cap.always_on:
        raise HTTPException(
            status_code=400,
            detail=f"Capability {capability_id} is always-on and cannot be toggled",
        )

    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    # Clinic-scoped protocols may only be toggled for the clinics they're
    # restricted to. Guard even on disable so a stray row can't be created
    # for the wrong clinic. Treated as 404 — the protocol doesn't exist for
    # this clinic, matching the list endpoint hiding it.
    if not is_clinic_compatible(cap, clinic_id):
        raise HTTPException(
            status_code=404,
            detail=f"Unknown capability: {capability_id}",
        )

    # Role-required protocols can't be disabled: the clinic's role compiler
    # includes them unconditionally, so a disable would write enabled=0 and
    # report success while changing nothing on the live agent. Config edits
    # (enabled=True + config) remain allowed.
    from api.voice_agent.roles import role_required_protocol_ids  # noqa: PLC0415
    if not body.enabled and capability_id in role_required_protocol_ids(clinic):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Capability {capability_id} is required by this clinic's agent "
                "role and cannot be disabled. Change the clinic's agent_role to "
                "'general' first if you need to turn it off."
            ),
        )

    pms_type = clinic.pms_type or "none"
    if body.enabled and not is_pms_compatible(cap, pms_type):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Capability {capability_id} is not supported for pms_type={pms_type!r}. "
                f"Supported: {cap.supported_pms}"
            ),
        )

    updater = caller.get("email") or caller.get("uid") or "unknown"

    # Validate incoming config against the protocol's config_model. A
    # ``None`` body.config is interpreted as "no change" → keep whatever's
    # stored (or fall back to defaults on first write). A non-None dict
    # must parse cleanly; bad input → 422 from Pydantic, surfaced as 400.
    if body.config is not None:
        try:
            validated_cfg = cap.config_model(**body.config).model_dump()
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid config for {capability_id}: {e}",
            )
    else:
        validated_cfg = None  # leave row.config unchanged below

    # Dual-write during the Protocol migration (step 3): the legacy
    # `voice_agent_capabilities` table receives the same write so a code
    # rollback stays consistent. Reads only consult `clinic_protocols`.
    # `capability_id` and `protocol_id` are the same string by design.
    legacy = db.get(VoiceAgentCapability, (clinic_id, capability_id))
    if legacy is None:
        legacy = VoiceAgentCapability(
            clinic_id=clinic_id,
            capability_id=capability_id,
            enabled=body.enabled,
            config=validated_cfg,
            updated_by=updater,
        )
        db.add(legacy)
    else:
        legacy.enabled = body.enabled
        if validated_cfg is not None:
            legacy.config = validated_cfg
        legacy.updated_by = updater

    row = db.get(ClinicProtocol, (clinic_id, capability_id))
    if row is None:
        row = ClinicProtocol(
            clinic_id=clinic_id,
            protocol_id=capability_id,
            enabled=body.enabled,
            config=validated_cfg,
            updated_by=updater,
        )
        db.add(row)
    else:
        row.enabled = body.enabled
        if validated_cfg is not None:
            row.config = validated_cfg
        row.updated_by = updater

    db.flush()  # ensure updated_at gets populated for the response
    db.refresh(row)

    # Propagate the change to the live VAPI assistant if one's provisioned.
    sync = _sync_assistant_if_provisioned(db, clinic)

    # Recompute the post-write enabled-id set so unmet_dependencies on the
    # response reflects the state the caller just produced (rather than
    # the state at the start of the request).
    post_rows = list(db.scalars(
        select(ClinicProtocol).where(
            ClinicProtocol.clinic_id == clinic_id,
            ClinicProtocol.enabled.is_(True),
        )
    ))
    post_enabled_ids = {r.protocol_id for r in post_rows}

    try:
        post_cfg = cap.config_model(**(row.config or {})) if row.config else cap.config_model()
    except Exception:
        post_cfg = cap.config_model()
    return CapabilityItem(
        id=cap.id,
        display_name=cap.display_name,
        description=cap.description,
        supported_pms=list(cap.supported_pms) if cap.supported_pms is not None else None,
        pms_compatible=is_pms_compatible(cap, pms_type),
        enabled=bool(row.enabled),
        updated_at=_isoformat(row.updated_at),
        updated_by=row.updated_by,
        depends_on=list(cap.depends_on),
        unmet_dependencies=unmet_dependencies(cap.id, post_enabled_ids),
        config_schema=cap.config_model.model_json_schema(),
        config=post_cfg.model_dump(),
        vapi_sync=sync,
    )


# ── Voice agent script (scope of practice) ───────────────────────────────────

class _VoiceAgentScriptResponse(BaseModel):
    clinic_id: str
    scope_of_practice:      str | None = None
    services_not_offered:   str | None = None
    additional_notes:       str | None = None
    existing_patient_intro: str | None = None
    updated_at:             str | None = None
    # Populated only on PUT — indicates whether the change propagated to
    # the live VAPI assistant. Absent on GET.
    vapi_sync:              dict | None = None


class _VoiceAgentScriptUpdate(BaseModel):
    """Partial update — only fields explicitly set in the payload are
    written. ``None`` clears a column; absent keys leave it alone."""
    scope_of_practice:      str | None = None
    services_not_offered:   str | None = None
    additional_notes:       str | None = None
    existing_patient_intro: str | None = None


_SCRIPT_FIELDS = (
    "scope_of_practice",
    "services_not_offered",
    "additional_notes",
    "existing_patient_intro",
)


def _script_to_response(clinic_id: str, row: ClinicVoiceAgentScript | None) -> _VoiceAgentScriptResponse:
    if row is None:
        return _VoiceAgentScriptResponse(clinic_id=clinic_id)
    return _VoiceAgentScriptResponse(
        clinic_id=clinic_id,
        scope_of_practice=row.scope_of_practice,
        services_not_offered=row.services_not_offered,
        additional_notes=row.additional_notes,
        existing_patient_intro=row.existing_patient_intro,
        updated_at=_isoformat(row.updated_at),
    )


@router.get(
    "/clinics/{clinic_id}/voice_agent/script",
    response_model=_VoiceAgentScriptResponse,
)
def get_voice_agent_script(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Return the editable script content for this clinic's voice agent.

    Returns null fields when no row has been created yet — the UI renders
    empty textareas and PUT-creates on first save.
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_read_access(clinic.instance_id, caller)

    row = db.get(ClinicVoiceAgentScript, clinic_id)
    return _script_to_response(clinic_id, row)


@router.put(
    "/clinics/{clinic_id}/voice_agent/script",
    response_model=_VoiceAgentScriptResponse,
)
def put_voice_agent_script(
    clinic_id: str,
    body: _VoiceAgentScriptUpdate,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Upsert the script content. Partial — only keys present in the body
    are written. Sending ``null`` clears the column; omitting the key leaves
    it unchanged. The dashboard editor sends all four fields at every save
    (including empty strings → null), so a one-shot save round-trip from
    the UI replaces the row contents wholesale.
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    row = db.get(ClinicVoiceAgentScript, clinic_id)
    if row is None:
        row = ClinicVoiceAgentScript(clinic_id=clinic_id)
        db.add(row)

    payload = body.model_dump(exclude_unset=True)
    for field in _SCRIPT_FIELDS:
        if field in payload:
            value = payload[field]
            # Treat empty string as null so the column clears cleanly.
            if isinstance(value, str) and value.strip() == "":
                value = None
            setattr(row, field, value)

    db.flush()

    # Propagate the change to the live VAPI assistant if one's provisioned.
    sync = _sync_assistant_if_provisioned(db, clinic)

    response = _script_to_response(clinic_id, row)
    response.vapi_sync = sync
    return response


# ── Voice agent persona ──────────────────────────────────────────────────────

class _PersonaResponse(BaseModel):
    clinic_id:     str
    agent_name:    str = "Emma"
    agent_title:   str = "virtual hearing assistant"
    voice_id:      str = "Emma"
    first_message: str | None = None
    # The greeting the agent actually plays right now: the stored override
    # when set, otherwise the computed templated default. Lets the dashboard
    # show "the current first message" rather than an empty override box.
    effective_first_message: str | None = None
    ai_model:      str = "gpt-4o"
    updated_at:    str | None = None
    vapi_sync:     dict | None = None


class _PersonaUpdate(BaseModel):
    agent_name:    str | None = None
    agent_title:   str | None = None
    voice_id:      str | None = None
    first_message: str | None = None
    ai_model:      str | None = None


_PERSONA_FIELDS = ("agent_name", "agent_title", "voice_id", "first_message", "ai_model")


def _persona_to_response(
    clinic_id: str,
    row: ClinicVoiceAgentPersona | None,
    clinic_name: str | None = None,
) -> _PersonaResponse:
    if row is None:
        return _PersonaResponse(
            clinic_id=clinic_id,
            effective_first_message=(
                build_first_message(clinic_name, None) if clinic_name else None
            ),
        )
    return _PersonaResponse(
        clinic_id=clinic_id,
        agent_name=row.agent_name,
        agent_title=row.agent_title,
        voice_id=row.voice_id,
        first_message=row.first_message,
        effective_first_message=(
            build_first_message(clinic_name, row) if clinic_name else None
        ),
        ai_model=row.ai_model,
        updated_at=_isoformat(row.updated_at),
    )


@router.get(
    "/clinics/{clinic_id}/voice_agent/persona",
    response_model=_PersonaResponse,
)
def get_voice_agent_persona(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Return the agent's presentation config (name, title, voice, model)."""
    clinic = _get_clinic_or_404(db, clinic_id)
    require_read_access(clinic.instance_id, caller)
    row = db.get(ClinicVoiceAgentPersona, clinic_id)
    return _persona_to_response(clinic_id, row, clinic_name=clinic.clinic_name)


@router.put(
    "/clinics/{clinic_id}/voice_agent/persona",
    response_model=_PersonaResponse,
)
def put_voice_agent_persona(
    clinic_id: str,
    body: _PersonaUpdate,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Upsert the persona row. Partial: only keys present in the payload
    are written. Empty strings on the four required fields (agent_name,
    agent_title, voice_id, ai_model) revert to the column's server default
    on next read since those columns are NOT NULL; the editor should send
    null or omit those keys to "reset to default" semantics.
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    row = db.get(ClinicVoiceAgentPersona, clinic_id)
    if row is None:
        row = ClinicVoiceAgentPersona(clinic_id=clinic_id)
        db.add(row)

    payload = body.model_dump(exclude_unset=True)
    for field in _PERSONA_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        # first_message is the only nullable persona field; clear with empty.
        if field == "first_message":
            if isinstance(value, str) and value.strip() == "":
                value = None
            setattr(row, field, value)
        else:
            # Required fields — ignore null / empty so we don't violate NOT NULL.
            if value is None:
                continue
            if isinstance(value, str) and value.strip() == "":
                continue
            setattr(row, field, value.strip() if isinstance(value, str) else value)

    db.flush()
    sync = _sync_assistant_if_provisioned(db, clinic)
    response = _persona_to_response(clinic_id, row, clinic_name=clinic.clinic_name)
    response.vapi_sync = sync
    return response


# ── Voice agent caller buckets ───────────────────────────────────────────────

class _CallerBucketItem(BaseModel):
    id:              int | None = None
    clinic_id:       str
    ordinal:         int = 0
    label:           str
    example_phrases: str | None = None
    canned_response: str | None = None
    active:          bool = True
    updated_at:      str | None = None


class _CallerBucketCreate(BaseModel):
    label:           str
    ordinal:         int | None = None
    example_phrases: str | None = None
    canned_response: str | None = None
    active:          bool = True


class _CallerBucketUpdate(BaseModel):
    label:           str | None = None
    ordinal:         int | None = None
    example_phrases: str | None = None
    canned_response: str | None = None
    active:          bool | None = None


class _CallerBucketsResponse(BaseModel):
    clinic_id: str
    buckets:   list[_CallerBucketItem]
    vapi_sync: dict | None = None


def _bucket_to_item(row: ClinicVoiceAgentCallerBucket) -> _CallerBucketItem:
    return _CallerBucketItem(
        id=row.id,
        clinic_id=row.clinic_id,
        ordinal=row.ordinal,
        label=row.label,
        example_phrases=row.example_phrases,
        canned_response=row.canned_response,
        active=bool(row.active),
        updated_at=_isoformat(row.updated_at),
    )


def _all_buckets_for_clinic(db: Session, clinic_id: str) -> list[ClinicVoiceAgentCallerBucket]:
    return list(db.scalars(
        select(ClinicVoiceAgentCallerBucket)
        .where(ClinicVoiceAgentCallerBucket.clinic_id == clinic_id)
        .order_by(ClinicVoiceAgentCallerBucket.ordinal.asc(),
                  ClinicVoiceAgentCallerBucket.id.asc())
    ))


@router.get(
    "/clinics/{clinic_id}/voice_agent/caller_buckets",
    response_model=_CallerBucketsResponse,
)
def list_voice_agent_caller_buckets(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """List the clinic's caller-intent buckets, ordered by ``ordinal``."""
    clinic = _get_clinic_or_404(db, clinic_id)
    require_read_access(clinic.instance_id, caller)
    rows = _all_buckets_for_clinic(db, clinic_id)
    return _CallerBucketsResponse(
        clinic_id=clinic_id,
        buckets=[_bucket_to_item(r) for r in rows],
    )


@router.post(
    "/clinics/{clinic_id}/voice_agent/caller_buckets",
    response_model=_CallerBucketsResponse,
)
def create_voice_agent_caller_bucket(
    clinic_id: str,
    body: _CallerBucketCreate,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Append a new caller bucket. If ``ordinal`` is null, places it at
    the end (max existing + 1)."""
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    if body.ordinal is None:
        existing = _all_buckets_for_clinic(db, clinic_id)
        next_ordinal = (max((b.ordinal for b in existing), default=-1) + 1)
    else:
        next_ordinal = body.ordinal

    row = ClinicVoiceAgentCallerBucket(
        clinic_id=clinic_id,
        ordinal=next_ordinal,
        label=body.label.strip(),
        example_phrases=body.example_phrases,
        canned_response=body.canned_response,
        active=body.active,
    )
    db.add(row)
    db.flush()
    sync = _sync_assistant_if_provisioned(db, clinic)

    rows = _all_buckets_for_clinic(db, clinic_id)
    return _CallerBucketsResponse(
        clinic_id=clinic_id,
        buckets=[_bucket_to_item(r) for r in rows],
        vapi_sync=sync,
    )


@router.put(
    "/clinics/{clinic_id}/voice_agent/caller_buckets/{bucket_id}",
    response_model=_CallerBucketsResponse,
)
def update_voice_agent_caller_bucket(
    clinic_id: str,
    bucket_id: int,
    body: _CallerBucketUpdate,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Update one caller bucket. Partial: only present fields are written."""
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    row = db.get(ClinicVoiceAgentCallerBucket, bucket_id)
    if row is None or row.clinic_id != clinic_id:
        raise HTTPException(status_code=404, detail="Caller bucket not found")

    payload = body.model_dump(exclude_unset=True)
    for field in ("label", "ordinal", "example_phrases", "canned_response", "active"):
        if field not in payload:
            continue
        value = payload[field]
        if field == "label":
            if value is None or (isinstance(value, str) and not value.strip()):
                raise HTTPException(status_code=400, detail="label cannot be empty")
            value = value.strip()
        setattr(row, field, value)

    db.flush()
    sync = _sync_assistant_if_provisioned(db, clinic)

    rows = _all_buckets_for_clinic(db, clinic_id)
    return _CallerBucketsResponse(
        clinic_id=clinic_id,
        buckets=[_bucket_to_item(r) for r in rows],
        vapi_sync=sync,
    )


@router.delete(
    "/clinics/{clinic_id}/voice_agent/caller_buckets/{bucket_id}",
    response_model=_CallerBucketsResponse,
)
def delete_voice_agent_caller_bucket(
    clinic_id: str,
    bucket_id: int,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Delete one caller bucket. Returns the post-delete list."""
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    row = db.get(ClinicVoiceAgentCallerBucket, bucket_id)
    if row is None or row.clinic_id != clinic_id:
        raise HTTPException(status_code=404, detail="Caller bucket not found")

    db.delete(row)
    db.flush()
    sync = _sync_assistant_if_provisioned(db, clinic)

    rows = _all_buckets_for_clinic(db, clinic_id)
    return _CallerBucketsResponse(
        clinic_id=clinic_id,
        buckets=[_bucket_to_item(r) for r in rows],
        vapi_sync=sync,
    )


# ── Voice agent new-patient qualifying questions ─────────────────────────────

class _QualifyingQuestionItem(BaseModel):
    id:                 int | None = None
    clinic_id:          str
    ordinal:            int = 0
    question_text:      str
    expected_responses: str | None = None
    active:             bool = True
    updated_at:         str | None = None


class _QualifyingQuestionCreate(BaseModel):
    question_text:      str
    ordinal:            int | None = None
    expected_responses: str | None = None
    active:             bool = True


class _QualifyingQuestionUpdate(BaseModel):
    question_text:      str | None = None
    ordinal:            int | None = None
    expected_responses: str | None = None
    active:             bool | None = None


class _QualifyingQuestionsResponse(BaseModel):
    clinic_id: str
    questions: list[_QualifyingQuestionItem]
    vapi_sync: dict | None = None


def _question_to_item(row: ClinicVoiceAgentQualifyingQuestion) -> _QualifyingQuestionItem:
    return _QualifyingQuestionItem(
        id=row.id,
        clinic_id=row.clinic_id,
        ordinal=row.ordinal,
        question_text=row.question_text,
        expected_responses=row.expected_responses,
        active=bool(row.active),
        updated_at=_isoformat(row.updated_at),
    )


def _all_questions_for_clinic(
    db: Session, clinic_id: str,
) -> list[ClinicVoiceAgentQualifyingQuestion]:
    return list(db.scalars(
        select(ClinicVoiceAgentQualifyingQuestion)
        .where(ClinicVoiceAgentQualifyingQuestion.clinic_id == clinic_id)
        .order_by(ClinicVoiceAgentQualifyingQuestion.ordinal.asc(),
                  ClinicVoiceAgentQualifyingQuestion.id.asc())
    ))


@router.get(
    "/clinics/{clinic_id}/voice_agent/qualifying_questions",
    response_model=_QualifyingQuestionsResponse,
)
def list_voice_agent_qualifying_questions(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """List the clinic's new-patient screening questions, ordered by ``ordinal``."""
    clinic = _get_clinic_or_404(db, clinic_id)
    require_read_access(clinic.instance_id, caller)
    rows = _all_questions_for_clinic(db, clinic_id)
    return _QualifyingQuestionsResponse(
        clinic_id=clinic_id,
        questions=[_question_to_item(r) for r in rows],
    )


@router.post(
    "/clinics/{clinic_id}/voice_agent/qualifying_questions",
    response_model=_QualifyingQuestionsResponse,
)
def create_voice_agent_qualifying_question(
    clinic_id: str,
    body: _QualifyingQuestionCreate,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Append a new qualifying question. If ``ordinal`` is null, places it at
    the end (max existing + 1)."""
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    if not body.question_text or not body.question_text.strip():
        raise HTTPException(status_code=400, detail="question_text cannot be empty")

    if body.ordinal is None:
        existing = _all_questions_for_clinic(db, clinic_id)
        next_ordinal = (max((q.ordinal for q in existing), default=-1) + 1)
    else:
        next_ordinal = body.ordinal

    row = ClinicVoiceAgentQualifyingQuestion(
        clinic_id=clinic_id,
        ordinal=next_ordinal,
        question_text=body.question_text.strip(),
        expected_responses=body.expected_responses,
        active=body.active,
    )
    db.add(row)
    db.flush()
    sync = _sync_assistant_if_provisioned(db, clinic)

    rows = _all_questions_for_clinic(db, clinic_id)
    return _QualifyingQuestionsResponse(
        clinic_id=clinic_id,
        questions=[_question_to_item(r) for r in rows],
        vapi_sync=sync,
    )


@router.put(
    "/clinics/{clinic_id}/voice_agent/qualifying_questions/{question_id}",
    response_model=_QualifyingQuestionsResponse,
)
def update_voice_agent_qualifying_question(
    clinic_id: str,
    question_id: int,
    body: _QualifyingQuestionUpdate,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Update one qualifying question. Partial: only present fields are written."""
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    row = db.get(ClinicVoiceAgentQualifyingQuestion, question_id)
    if row is None or row.clinic_id != clinic_id:
        raise HTTPException(status_code=404, detail="Qualifying question not found")

    payload = body.model_dump(exclude_unset=True)
    for field in ("question_text", "ordinal", "expected_responses", "active"):
        if field not in payload:
            continue
        value = payload[field]
        # ordinal/active are NOT NULL columns; an explicit null in the body
        # must be ignored rather than written (a NULL write would 500 on the
        # DB). expected_responses is nullable, so None there is a valid clear.
        if field in ("ordinal", "active") and value is None:
            continue
        if field == "question_text":
            if value is None or (isinstance(value, str) and not value.strip()):
                raise HTTPException(status_code=400, detail="question_text cannot be empty")
            value = value.strip()
        setattr(row, field, value)

    db.flush()
    sync = _sync_assistant_if_provisioned(db, clinic)

    rows = _all_questions_for_clinic(db, clinic_id)
    return _QualifyingQuestionsResponse(
        clinic_id=clinic_id,
        questions=[_question_to_item(r) for r in rows],
        vapi_sync=sync,
    )


@router.delete(
    "/clinics/{clinic_id}/voice_agent/qualifying_questions/{question_id}",
    response_model=_QualifyingQuestionsResponse,
)
def delete_voice_agent_qualifying_question(
    clinic_id: str,
    question_id: int,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Delete one qualifying question. Returns the post-delete list."""
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    row = db.get(ClinicVoiceAgentQualifyingQuestion, question_id)
    if row is None or row.clinic_id != clinic_id:
        raise HTTPException(status_code=404, detail="Qualifying question not found")

    db.delete(row)
    db.flush()
    sync = _sync_assistant_if_provisioned(db, clinic)

    rows = _all_questions_for_clinic(db, clinic_id)
    return _QualifyingQuestionsResponse(
        clinic_id=clinic_id,
        questions=[_question_to_item(r) for r in rows],
        vapi_sync=sync,
    )


# ── Voice agent FAQ (curated, retrieved at call time) ────────────────────────
#
# FAQ content deliberately never enters the system prompt — see
# api/voice_agent/protocols/faq_lookup.py and api/voice_agent/faq_retrieval.py.
# Cloud SQL owns the curated text + approval state; BigQuery owns the vectors.
# Approving embeds; unapproving/deleting/editing-an-approved-row re-syncs. The
# two stores are reconciled on `embedding_synced_at`, so a BigQuery failure
# leaves a visible NULL rather than silently diverging.

class _FaqItem(BaseModel):
    id:                  int | None = None
    clinic_id:           str
    question:            str
    answer:              str
    source:              Literal["etl", "manual"] = "manual"
    source_call_id:      str | None = None
    approved:            bool = False
    approved_by:         str | None = None
    embedding_synced_at: str | None = None
    updated_at:          str | None = None


class _FaqCreate(BaseModel):
    question: str
    answer:   str
    approved: bool = False


class _FaqUpdate(BaseModel):
    question: str | None = None
    answer:   str | None = None
    approved: bool | None = None


class _FaqsResponse(BaseModel):
    clinic_id: str
    faqs:      list[_FaqItem]
    # Populated when a write triggered an embedding sync that FAILED. The write
    # itself still succeeded, so this surfaces "approved but not retrievable"
    # instead of pretending the FAQ is live.
    embedding_error: str | None = None


class _FaqImportResponse(BaseModel):
    clinic_id: str
    imported:  int
    skipped:   int
    faqs:      list[_FaqItem]


class _FaqSearchRequest(BaseModel):
    question: str


def _faq_to_item(row: ClinicVoiceAgentFaq) -> _FaqItem:
    return _FaqItem(
        id=row.id,
        clinic_id=row.clinic_id,
        question=row.question,
        answer=row.answer,
        source=row.source,
        source_call_id=row.source_call_id,
        approved=bool(row.approved),
        approved_by=row.approved_by,
        embedding_synced_at=_isoformat(row.embedding_synced_at),
        updated_at=_isoformat(row.updated_at),
    )


def _all_faqs_for_clinic(db: Session, clinic_id: str) -> list[ClinicVoiceAgentFaq]:
    return list(db.scalars(
        select(ClinicVoiceAgentFaq)
        .where(ClinicVoiceAgentFaq.clinic_id == clinic_id)
        .order_by(ClinicVoiceAgentFaq.approved.desc(),
                  ClinicVoiceAgentFaq.id.asc())
    ))


def _apply_embedding(db: Session, row: ClinicVoiceAgentFaq) -> str | None:
    """Bring BigQuery in line with this row's approval state.

    Approved → embed + upsert and stamp ``embedding_synced_at``. Not approved →
    delete the vector and clear the stamp, so an unapproved answer stops being
    retrievable immediately rather than at the next rebuild.

    Returns an error string on failure instead of raising: the Cloud SQL write
    is already committed and rolling it back would leave the dashboard
    disagreeing with what the admin just did. A NULL ``embedding_synced_at`` on
    an approved row is the durable "not live yet" signal, and the message is
    handed to the caller so the UI can say so.
    """
    from api.voice_agent import faq_retrieval

    try:
        if row.approved:
            faq_retrieval.sync_faq_embedding(
                clinic_id=row.clinic_id, faq_id=row.id,
                question=row.question, answer=row.answer,
            )
            row.embedding_synced_at = datetime.utcnow()
        else:
            faq_retrieval.delete_faq_embedding(
                clinic_id=row.clinic_id, faq_id=row.id,
            )
            row.embedding_synced_at = None
        db.flush()
        return None
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller, see docstring
        log.exception(
            "FAQ embedding sync failed clinic=%s faq_id=%s", row.clinic_id, row.id,
        )
        return f"{type(exc).__name__}: {exc}"


@router.get(
    "/clinics/{clinic_id}/voice_agent/faqs",
    response_model=_FaqsResponse,
)
def list_voice_agent_faqs(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """List the clinic's curated FAQs, approved first."""
    clinic = _get_clinic_or_404(db, clinic_id)
    require_read_access(clinic.instance_id, caller)
    return _FaqsResponse(
        clinic_id=clinic_id,
        faqs=[_faq_to_item(r) for r in _all_faqs_for_clinic(db, clinic_id)],
    )


@router.post(
    "/clinics/{clinic_id}/voice_agent/faqs",
    response_model=_FaqsResponse,
)
def create_voice_agent_faq(
    clinic_id: str,
    body: _FaqCreate,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Hand-author a FAQ. Embeds immediately when created already approved."""
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    question = (body.question or "").strip()
    answer = (body.answer or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question cannot be empty")
    if not answer:
        raise HTTPException(status_code=400, detail="answer cannot be empty")

    existing = db.scalar(
        select(ClinicVoiceAgentFaq).where(
            ClinicVoiceAgentFaq.clinic_id == clinic_id,
            ClinicVoiceAgentFaq.question == question,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="A FAQ with this question already exists for this clinic",
        )

    row = ClinicVoiceAgentFaq(
        clinic_id=clinic_id,
        question=question,
        answer=answer,
        source="manual",
        approved=body.approved,
        approved_by=(caller.get("email") if body.approved else None),
    )
    db.add(row)
    db.flush()
    err = _apply_embedding(db, row) if row.approved else None

    return _FaqsResponse(
        clinic_id=clinic_id,
        faqs=[_faq_to_item(r) for r in _all_faqs_for_clinic(db, clinic_id)],
        embedding_error=err,
    )


@router.put(
    "/clinics/{clinic_id}/voice_agent/faqs/{faq_id}",
    response_model=_FaqsResponse,
)
def update_voice_agent_faq(
    clinic_id: str,
    faq_id: int,
    body: _FaqUpdate,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Update one FAQ. Partial — only present fields are written.

    Any change to an APPROVED row re-embeds: editing the answer text without
    re-syncing would leave the agent reading the previous wording, which is the
    worst kind of stale (invisible, and confidently spoken).
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    row = db.get(ClinicVoiceAgentFaq, faq_id)
    if row is None or row.clinic_id != clinic_id:
        raise HTTPException(status_code=404, detail="FAQ not found")

    if body.question is not None:
        q = body.question.strip()
        if not q:
            raise HTTPException(status_code=400, detail="question cannot be empty")
        row.question = q
    if body.answer is not None:
        a = body.answer.strip()
        if not a:
            raise HTTPException(status_code=400, detail="answer cannot be empty")
        row.answer = a
    if body.approved is not None and bool(body.approved) != bool(row.approved):
        row.approved = body.approved
        row.approved_by = caller.get("email") if body.approved else None

    db.flush()
    # Re-sync whenever the row is (or was) approved. An unapproved row that
    # stayed unapproved has no vector to maintain.
    err = _apply_embedding(db, row) if (row.approved or row.embedding_synced_at) else None

    return _FaqsResponse(
        clinic_id=clinic_id,
        faqs=[_faq_to_item(r) for r in _all_faqs_for_clinic(db, clinic_id)],
        embedding_error=err,
    )


@router.delete(
    "/clinics/{clinic_id}/voice_agent/faqs/{faq_id}",
    response_model=_FaqsResponse,
)
def delete_voice_agent_faq(
    clinic_id: str,
    faq_id: int,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Delete one FAQ, removing its vector first so nothing is left retrievable."""
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    row = db.get(ClinicVoiceAgentFaq, faq_id)
    if row is None or row.clinic_id != clinic_id:
        raise HTTPException(status_code=404, detail="FAQ not found")

    # Drop the vector BEFORE the row disappears — afterwards we'd have no
    # faq_id to target and the embedding would be orphaned in BigQuery,
    # answerable by the agent forever.
    row.approved = False
    err = _apply_embedding(db, row)

    db.delete(row)
    db.flush()
    return _FaqsResponse(
        clinic_id=clinic_id,
        faqs=[_faq_to_item(r) for r in _all_faqs_for_clinic(db, clinic_id)],
        embedding_error=err,
    )


@router.post(
    "/clinics/{clinic_id}/voice_agent/faqs/import",
    response_model=_FaqImportResponse,
)
def import_voice_agent_faq_suggestions(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Pull transcript-extracted FAQ suggestions from ``ClinicData.faq``.

    Imported rows land ``approved = False`` and ``source = 'etl'``, always.
    These are unreviewed LLM extractions from call recordings — auto-approving
    them would put an unverified answer about price or coverage in front of a
    patient in the agent's voice.

    Idempotent: questions already present for the clinic are skipped, so this
    can be run repeatedly as the extractor produces more.
    """
    clinic = _get_clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    sql = f"""
    SELECT question, ANY_VALUE(answer) AS answer,
           ANY_VALUE(complete_call_id) AS complete_call_id
    FROM `{FAQ_SUGGESTIONS_TABLE}`
    WHERE clinic_id = @clinic_id
      AND question IS NOT NULL AND TRIM(question) != ''
      AND answer   IS NOT NULL AND TRIM(answer)   != ''
    GROUP BY question
    """
    job = bq_client.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id),
        ]),
    )
    candidates = [
        (r["question"].strip(), r["answer"].strip(), r["complete_call_id"])
        for r in job.result()
    ]

    existing = {
        q for (q,) in db.execute(
            select(ClinicVoiceAgentFaq.question)
            .where(ClinicVoiceAgentFaq.clinic_id == clinic_id)
        )
    }

    imported = 0
    for question, answer, ccid in candidates:
        if question in existing:
            continue
        # The column is String(512); a longer extraction would otherwise fail
        # the insert and abort the whole import.
        if len(question) > 512:
            continue
        db.add(ClinicVoiceAgentFaq(
            clinic_id=clinic_id,
            question=question,
            answer=answer,
            source="etl",
            source_call_id=ccid,
            approved=False,
        ))
        existing.add(question)
        imported += 1
    db.flush()

    return _FaqImportResponse(
        clinic_id=clinic_id,
        imported=imported,
        skipped=len(candidates) - imported,
        faqs=[_faq_to_item(r) for r in _all_faqs_for_clinic(db, clinic_id)],
    )


@router.post("/clinics/{clinic_id}/voice_agent/faq/search")
def search_voice_agent_faq(
    clinic_id: str,
    body: _FaqSearchRequest,
    _: None = Depends(verify_vapi_secret),
    db: Session = Depends(get_session),
):
    """Semantic FAQ lookup for the live agent (`answer_clinic_question`).

    VAPI-authenticated, not Firebase — same posture as the Blueprint tools.
    Returns ``{matched, answers:[{question, answer}]}``; ``matched: false``
    means nothing was close enough, and the prompt instructs the agent to hand
    off rather than improvise.

    ``distance`` is intentionally NOT returned. It is a tuning signal for us,
    not something the model should reason about or mention to a caller.
    """
    from api.voice_agent import faq_retrieval
    from api.voice_agent.protocols.faq_lookup import FaqLookupConfig

    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    # Per-clinic tuning if configured; model defaults otherwise. A stored
    # config that no longer validates must not take the tool down mid-call —
    # falling back to defaults degrades retrieval quality, raising drops the
    # caller's question entirely.
    cfg = FaqLookupConfig()
    row = db.get(ClinicProtocol, (clinic_id, "faq_lookup"))
    if row is not None and row.config:
        try:
            cfg = FaqLookupConfig(**row.config)
        except Exception:  # noqa: BLE001 — see docstring
            log.warning(
                "faq_lookup config invalid for clinic=%s; using defaults", clinic_id,
            )

    results = faq_retrieval.search_faqs(
        clinic_id=clinic_id,
        question=question,
        top_k=cfg.top_k,
        max_distance=cfg.max_distance,
    )
    return {
        "matched": bool(results),
        "answers": [
            {"question": r["question"], "answer": r["answer"]} for r in results
        ],
    }
