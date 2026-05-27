"""
Voice-agent protocols — composable units of tools + prompt + per-clinic config.

A protocol bundles VAPI tool definitions, a prompt fragment, and (eventually)
typed per-clinic configuration into a unit that can be turned on/off per
clinic. Protocols generalize the legacy `Capability` framework — see
`protocols/base.py` for the shape and `resources/protocols-design.md`
for the design rationale.

Active protocols (six toggleable + one always-on):

  - `VerifyCallerIdentificationProtocol`   (Blueprint, Audit Data)
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
    "PROTOCOL_REGISTRY",
    "PROTOCOL_METADATA",
    "PROTOCOL_METADATA_BY_ID",
    "is_pms_compatible",
    "toggleable_protocols",
]
