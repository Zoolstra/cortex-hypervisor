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

from fastapi import APIRouter, Depends, HTTPException
from google.cloud import bigquery
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import bq_client, bq_table, require_read_access, require_write_access, verify_token
from api.voice_agent import vapi as vapi_client
from api.voice_agent.blueprint import verify_vapi_secret
from api.core.db import get_session
from api.core.orm import (
    Clinic, ClinicProtocol, ClinicVoiceAgentCallerBucket,
    ClinicVoiceAgentConfiguration, ClinicVoiceAgentPersona,
    ClinicVoiceAgentScript, VoiceAgentCapability,
)
from api.voice_agent.protocols import (
    PROTOCOL_METADATA as CAPABILITY_METADATA,
    PROTOCOL_METADATA_BY_ID as CAPABILITY_METADATA_BY_ID,
    is_pms_compatible,
    unmet_dependencies,
)
from api.voice_agent.factory import build_agent_config


router = APIRouter()


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
):
    """
    Called by VAPI's submit_ticket tool at the end of a voice call.

    Validates the clinic exists in Cloud SQL, then appends one row to
    `Users.voice_agent_tickets` in BigQuery (analytics store, intentionally
    separate from the operational config in Cloud SQL).
    """
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")

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
            bigquery.ScalarQueryParameter("caller_phone", "STRING", body.caller_phone),
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
    scope_of_practice:         str | None = None
    services_offered:          str | None = None
    services_not_offered:      str | None = None
    caller_needs:              str | None = None
    additional_notes:          str | None = None
    opening_overrides:         str | None = None
    new_patient_intake_prompt: str | None = None
    existing_patient_intro:    str | None = None
    updated_at:                str | None = None
    # Populated only on PUT — indicates whether the change propagated to
    # the live VAPI assistant. Absent on GET.
    vapi_sync:                 dict | None = None


class _VoiceAgentScriptUpdate(BaseModel):
    """Partial update — only fields explicitly set in the payload are
    written. ``None`` clears a column; absent keys leave it alone."""
    scope_of_practice:         str | None = None
    services_offered:          str | None = None
    services_not_offered:      str | None = None
    caller_needs:              str | None = None
    additional_notes:          str | None = None
    opening_overrides:         str | None = None
    new_patient_intake_prompt: str | None = None
    existing_patient_intro:    str | None = None


_SCRIPT_FIELDS = (
    "scope_of_practice",
    "services_offered",
    "services_not_offered",
    "caller_needs",
    "additional_notes",
    "opening_overrides",
    "new_patient_intake_prompt",
    "existing_patient_intro",
)


def _script_to_response(clinic_id: str, row: ClinicVoiceAgentScript | None) -> _VoiceAgentScriptResponse:
    if row is None:
        return _VoiceAgentScriptResponse(clinic_id=clinic_id)
    return _VoiceAgentScriptResponse(
        clinic_id=clinic_id,
        scope_of_practice=row.scope_of_practice,
        services_offered=row.services_offered,
        services_not_offered=row.services_not_offered,
        caller_needs=row.caller_needs,
        additional_notes=row.additional_notes,
        opening_overrides=row.opening_overrides,
        new_patient_intake_prompt=row.new_patient_intake_prompt,
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
    it unchanged. The dashboard editor sends all five fields at every save
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


def _persona_to_response(clinic_id: str, row: ClinicVoiceAgentPersona | None) -> _PersonaResponse:
    if row is None:
        return _PersonaResponse(clinic_id=clinic_id)
    return _PersonaResponse(
        clinic_id=clinic_id,
        agent_name=row.agent_name,
        agent_title=row.agent_title,
        voice_id=row.voice_id,
        first_message=row.first_message,
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
    return _persona_to_response(clinic_id, row)


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
    response = _persona_to_response(clinic_id, row)
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
