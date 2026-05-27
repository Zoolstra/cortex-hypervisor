"""
Compatibility shim — ``Capability`` is now ``Protocol``.

The canonical home of the per-clinic tool/prompt framework lives at
``api/voice_agent/protocols/`` (see ``resources/protocols-design.md``).

This module re-exports the protocol classes + registry under their old
``Capability*`` names so existing imports don't break. New code should
import from ``api.voice_agent.protocols``.

The mapping after the v2 protocol set landed:

  Legacy alias                  → Current class
  -----------------------------   -----------------------------------------
  Capability                      Protocol
  CAPABILITY_REGISTRY             PROTOCOL_REGISTRY
  CAPABILITY_METADATA             PROTOCOL_METADATA
  CAPABILITY_METADATA_BY_ID       PROTOCOL_METADATA_BY_ID
  is_pms_compatible               (unchanged)
  toggleable_capabilities         toggleable_protocols
  SubmitTicket                    SubmitTicketProtocol
  PatientMatch                    VerifyCallerIdentificationProtocol
  ListAppointmentTypes            SearchAppointmentAvailabilityProtocol *
  FindAvailableSlots              SearchAppointmentAvailabilityProtocol *

* The two legacy list/find protocols collapsed into one multi-tool
  ``SearchAppointmentAvailabilityProtocol``. Both aliases point at the same
  class; toggling either flips the combined protocol on. Don't write new
  code against these aliases — they exist for in-flight imports only.

The HTTP API and the ``voice_agent_capabilities`` Cloud SQL table still
use "capability" terminology — that rename is a later step in the
migration. Only the Python framework was renamed.
"""
from __future__ import annotations

from api.voice_agent.protocols import (
    PROTOCOL_METADATA as CAPABILITY_METADATA,
    PROTOCOL_METADATA_BY_ID as CAPABILITY_METADATA_BY_ID,
    PROTOCOL_REGISTRY as CAPABILITY_REGISTRY,
    BookAppointmentProtocol,
    CancelAppointmentProtocol,
    LocateAppointmentProtocol,
    Protocol as Capability,
    RescheduleAppointmentProtocol,
    SearchAppointmentAvailabilityProtocol,
    SubmitTicketProtocol as SubmitTicket,
    VerifyCallerIdentificationProtocol,
    is_pms_compatible,
    toggleable_protocols as toggleable_capabilities,
)

# Legacy single-class aliases. The 1:1 ones (PatientMatch, SubmitTicket)
# remain unambiguous. ListAppointmentTypes/FindAvailableSlots both point at
# the merged Search protocol — don't rely on them.
PatientMatch = VerifyCallerIdentificationProtocol
ListAppointmentTypes = SearchAppointmentAvailabilityProtocol
FindAvailableSlots = SearchAppointmentAvailabilityProtocol


__all__ = [
    # Legacy names
    "Capability",
    "SubmitTicket",
    "PatientMatch",
    "ListAppointmentTypes",
    "FindAvailableSlots",
    "CAPABILITY_REGISTRY",
    "CAPABILITY_METADATA",
    "CAPABILITY_METADATA_BY_ID",
    "is_pms_compatible",
    "toggleable_capabilities",
    # Re-exported new names (so callers can also reach them via this module
    # if they want to incrementally migrate)
    "VerifyCallerIdentificationProtocol",
    "SearchAppointmentAvailabilityProtocol",
    "LocateAppointmentProtocol",
    "BookAppointmentProtocol",
    "CancelAppointmentProtocol",
    "RescheduleAppointmentProtocol",
]
