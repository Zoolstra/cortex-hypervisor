"""
Voice-agent protocols — composable units of tools + prompt + per-clinic config.

A protocol bundles VAPI tool definitions, a prompt fragment, and (eventually)
typed per-clinic configuration into a unit that can be turned on/off per
clinic. Protocols generalize the legacy `Capability` framework — see
`protocols/base.py` for the shape and `resources/protocols-design.md`
for the design rationale.

Active protocols (seven toggleable + one always-on):

  - `VerifyCallerIdentificationProtocol`   (Blueprint, Audit Data)
  - `RetrievePatientContextProtocol`       (Blueprint) journal/history context
  - `SearchAppointmentAvailabilityProtocol`  multi-tool: list types + find slots
  - `LocateAppointmentProtocol`            (Blueprint)
  - `BookAppointmentProtocol`              (Blueprint)
  - `CancelAppointmentProtocol`            (Blueprint)
  - `RescheduleAppointmentProtocol`        (Blueprint)
  - `SubmitTicketProtocol`                 (always-on, PMS-agnostic)

The hypervisor's existing `capabilities.py` is a thin compat shim
re-exporting from here under the legacy names (Capability, PatientMatch,
etc.). Old IDs in clinic_protocols are migrated by alembic revision 0005.

Display order in `PROTOCOL_METADATA` matches the dashboard rendering order:
toggleable first (in a sensible call-flow order), always-on last so
SubmitTicket's "closing & ticket" block sits at the end of the assembled
system prompt.
"""
from __future__ import annotations

from api.voice_agent.protocols.base import EmptyConfig, Protocol
from api.voice_agent.protocols.book_appointment import BookAppointmentProtocol
from api.voice_agent.protocols.cancel_appointment import CancelAppointmentProtocol
from api.voice_agent.protocols.locate_appointment import LocateAppointmentProtocol
from api.voice_agent.protocols.reschedule_appointment import RescheduleAppointmentProtocol
from api.voice_agent.protocols.retrieve_patient_context import (
    RetrievePatientContextProtocol,
)
from api.voice_agent.protocols.search_appointment_availability import (
    SearchAppointmentAvailabilityProtocol,
)
from api.voice_agent.protocols.submit_ticket import SubmitTicketProtocol
from api.voice_agent.protocols.verify_caller_identification import (
    VerifyCallerIdentificationProtocol,
)


# ── Registry ──────────────────────────────────────────────────────────────────


PROTOCOL_REGISTRY: dict[str, type[Protocol]] = {
    SubmitTicketProtocol.id:                  SubmitTicketProtocol,
    VerifyCallerIdentificationProtocol.id:    VerifyCallerIdentificationProtocol,
    RetrievePatientContextProtocol.id:        RetrievePatientContextProtocol,
    SearchAppointmentAvailabilityProtocol.id: SearchAppointmentAvailabilityProtocol,
    LocateAppointmentProtocol.id:             LocateAppointmentProtocol,
    BookAppointmentProtocol.id:               BookAppointmentProtocol,
    CancelAppointmentProtocol.id:             CancelAppointmentProtocol,
    RescheduleAppointmentProtocol.id:         RescheduleAppointmentProtocol,
}


# Display order for the dashboard + prompt assembly. Toggleable first (in
# call-flow order: verify → search → locate → book/cancel/reschedule),
# always-on last so SubmitTicket's closing block lands at the end of the
# assembled system prompt.
PROTOCOL_METADATA: list[type[Protocol]] = [
    VerifyCallerIdentificationProtocol,
    RetrievePatientContextProtocol,
    SearchAppointmentAvailabilityProtocol,
    LocateAppointmentProtocol,
    BookAppointmentProtocol,
    CancelAppointmentProtocol,
    RescheduleAppointmentProtocol,
    SubmitTicketProtocol,
]


PROTOCOL_METADATA_BY_ID: dict[str, type[Protocol]] = {
    cls.id: cls for cls in PROTOCOL_METADATA
}


# ── Compatibility helpers ─────────────────────────────────────────────────────


def is_pms_compatible(
    proto: type[Protocol] | Protocol,
    pms_type: str | None,
) -> bool:
    """True if the protocol supports a clinic with this `pms_type`.

    Accepts either the class or an instance — `supported_pms` is a ClassVar.
    """
    if proto.supported_pms is None:
        return True
    return (pms_type or "none") in proto.supported_pms


def toggleable_protocols() -> list[type[Protocol]]:
    """Protocols admins can enable/disable from the dashboard.

    Excludes always-on foundational protocols (e.g. `SubmitTicketProtocol`).
    """
    return [cls for cls in PROTOCOL_REGISTRY.values() if not cls.always_on]


def load_protocol_config(
    db,
    clinic_id: str,
    protocol_id: str,
):
    """Load + validate a clinic's per-protocol config row.

    Returns an instance of the protocol's ``config_model`` populated
    from ``clinic_protocols.config`` for this (clinic, protocol). Falls
    back to the model's defaults when:
      - no row exists for this clinic,
      - the row's ``config`` column is null,
      - the persisted JSON parses but is empty.

    Re-validates the stored JSON against the current schema. If the
    schema has tightened since the row was written and the data no
    longer parses, raises ``pydantic.ValidationError`` so the caller
    can decide how to surface it (the toggle endpoint maps to 422; the
    factory logs + falls back to defaults).

    Local import of ``ClinicProtocol`` avoids a circular dep at module
    load — protocols/__init__ shouldn't pull the ORM.
    """
    from api.core.orm import ClinicProtocol  # noqa: PLC0415 — see docstring

    cls = PROTOCOL_REGISTRY.get(protocol_id)
    if cls is None:
        raise KeyError(f"Unknown protocol_id: {protocol_id!r}")
    row = db.get(ClinicProtocol, (clinic_id, protocol_id))
    raw = (row.config if row and row.config else {}) or {}
    return cls.config_model(**raw)


def unmet_dependencies(
    protocol_id: str,
    enabled_ids: set[str] | frozenset[str] | list[str] | tuple[str, ...],
) -> list[str]:
    """Return the protocol's ``depends_on`` ids that aren't in ``enabled_ids``.

    Order matches the protocol's declaration so error messages list deps
    in a stable, meaningful sequence. Always-on protocols are treated as
    implicitly enabled — they're guaranteed present in the sync.

    Returns ``[]`` when the protocol id is unknown (defensive — unknown
    ids surface elsewhere, this isn't the right place to raise).
    """
    cls = PROTOCOL_REGISTRY.get(protocol_id)
    if cls is None:
        return []
    enabled = set(enabled_ids)
    # Always-on protocols are de-facto enabled.
    enabled.update(c.id for c in PROTOCOL_REGISTRY.values() if c.always_on)
    return [d for d in cls.depends_on if d not in enabled]


__all__ = [
    "EmptyConfig",
    "Protocol",
    "SubmitTicketProtocol",
    "VerifyCallerIdentificationProtocol",
    "SearchAppointmentAvailabilityProtocol",
    "LocateAppointmentProtocol",
    "BookAppointmentProtocol",
    "CancelAppointmentProtocol",
    "RescheduleAppointmentProtocol",
    "RetrievePatientContextProtocol",
    "PROTOCOL_REGISTRY",
    "PROTOCOL_METADATA",
    "PROTOCOL_METADATA_BY_ID",
    "is_pms_compatible",
    "load_protocol_config",
    "toggleable_protocols",
    "unmet_dependencies",
]
