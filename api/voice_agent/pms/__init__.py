"""
PMS adapter layer for the voice agent.

A PMS adapter is the seam between protocol code and a specific Practice
Management System's API. Protocols call `find_patient`, `list_appointment_types`,
etc.; the adapter translates each call into the HTTP / BQ / SDK shape its
PMS understands.

The adapter is what kills the `if pms_type == "blueprint": ... elif pms_type
== "audit_data": ...` switches scattered across capability code today. A
protocol that needs patient lookup depends on `PMSAdapter`, not on any one
PMS string.

This module exports:

- `PMSAdapter` — the ABC every adapter implements.
- Result dataclasses (`PatientMatchResult`, `AppointmentType`,
  `AvailabilityDay`, `AvailabilityResult`) — the PMS-agnostic shapes
  protocols see.
- `adapter_for(clinic)` — picks the right adapter for a clinic's pms_type.

An adapter that doesn't implement a method should raise `NotImplementedError`
from that method — the calling protocol surfaces this as "PMS doesn't
support this operation."
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


# ── Result shapes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PatientMatchResult:
    """Outcome of `find_patient`.

    `status="matched"` always carries a `patient_id`. `ambiguous` and
    `unmatched` carry `patient_id=None`. `candidates_count` is the raw
    server-side row count; useful for telemetry, never leaked to callers.
    """

    status: Literal["matched", "ambiguous", "unmatched"]
    patient_id: str | None
    candidates_count: int


@dataclass(frozen=True)
class AppointmentType:
    """A bookable appointment type from the PMS."""

    id: int
    name: str
    duration_minutes: int | None


@dataclass(frozen=True)
class Location:
    """A bookable clinic location from the PMS."""

    id: int
    name: str | None
    address: str | None = None


@dataclass(frozen=True)
class Provider:
    """A bookable provider from the PMS — name ↔ id mapping for the agent."""

    id: int
    name: str | None


@dataclass(frozen=True)
class JournalEntry:
    """One patient journal/history entry surfaced to the agent for context.

    Clinical PHI — only the minimal fields the agent needs as background.
    Never includes the raw PMS row.
    """

    entry_time: str        # ISO-8601 (clinic-local) or YYYY-MM-DD
    entry_type: str | None
    text: str              # user_text preferred, generated_text fallback


@dataclass(frozen=True)
class AvailabilityDay:
    """One day of bookable slots in a search result."""

    date: str  # YYYY-MM-DD
    available_times: list[str]  # ["09:00", "09:30", ...]


@dataclass(frozen=True)
class AvailabilityResult:
    days: list[AvailabilityDay]


@dataclass(frozen=True)
class Appointment:
    """An existing appointment as seen by the voice agent.

    Returned by ``list_patient_appointments``. The PMS-internal fields
    needed to act on the appointment later (cancel/reschedule) — e.g.
    Blueprint's ``onlineBookingSecret`` — are deliberately NOT exposed
    here. Adapters re-resolve them server-side when an act-on operation
    runs.
    """

    appointment_id: str
    event_type_id: int | None
    event_type_name: str | None  # e.g. "Hearing test"; agent-facing
    summary: str | None
    start_time: str   # ISO-8601 in clinic local time, e.g. "2026-05-30T10:00"
    end_time: str
    provider_name: str | None
    location_name: str | None
    status: str       # "confirmed" / "tentative" / "cancelled" / "no_show" / etc.


@dataclass(frozen=True)
class BookingResult:
    """Outcome of ``book``, ``cancel``, or ``reschedule``.

    Blueprint's create-appointment returns 201 with empty body — no
    server-side ID. For the create path ``appointment_id`` is therefore
    ``None`` unless the adapter does a follow-up Search to recover it.
    Cancel and reschedule echo the affected appointment for the agent's
    confirmation message.
    """

    status: Literal["booked", "cancelled", "rescheduled", "partial"]
    appointment_id: str | None
    summary: str | None
    start_time: str | None  # ISO-8601 in clinic local time
    end_time: str | None
    # Set when status="partial" — non-atomic reschedule edge case where
    # the new booking landed but cancelling the old one failed. Caller
    # leaves with two appointments and staff must clean up.
    warning: str | None = None


# ── Adapter ABC ───────────────────────────────────────────────────────────────


class PMSAdapter(ABC):
    """Operations a Practice Management System exposes to voice-agent protocols.

    A concrete adapter declares `pms_type` (the string used in
    `clinics.pms_type`) and implements the operations the PMS supports.
    Operations the PMS does not support raise `NotImplementedError` —
    callers treat that as "this clinic can't do that protocol."

    Adapters are typically cheap to construct; configuration loading is
    lazy so a method that doesn't need clinic-level config (e.g. a BQ
    query against `_clinic_id`) doesn't pay a Cloud SQL round trip.
    """

    pms_type: str

    @abstractmethod
    def find_patient(
        self,
        *,
        first_name: str,
        last_name: str,
        last4_phone: str,
        dob: str | None = None,
    ) -> PatientMatchResult:
        """Look up an existing patient by name + last-4-phone (+ optional DOB)."""

    @abstractmethod
    def list_appointment_types(self) -> list[AppointmentType]:
        """Return the clinic's bookable appointment types."""

    @abstractmethod
    def list_locations(self) -> list[Location]:
        """Return the clinic's bookable locations.

        Used when a clinic opts into asking the caller which location to
        book into before searching availability. Single-location clinics
        still expose their one location here so callers can resolve it
        without separate config.
        """

    @abstractmethod
    def list_providers(self) -> list[Provider]:
        """Return the clinic's bookable providers as a name ↔ id mapping.

        Injected into the system prompt so the agent can map a caller's
        provider preference to an id and make sense of provider ids in
        availability responses.
        """

    @abstractmethod
    def get_patient_journal(self, *, patient_id: str) -> list[JournalEntry]:
        """Return recent journal/history entries for an identified patient.

        Clinical PHI — implementations MUST filter by both the clinic and
        the patient id, exclude deleted rows, and bound recency/volume.
        Only called after the caller has been verified as an existing patient.
        """

    @abstractmethod
    def find_available_slots(
        self,
        *,
        event_type_id: int,
        start_date: str,
        end_date: str,
        providers: list[int] | None = None,
        locations: list[int] | None = None,
    ) -> AvailabilityResult:
        """Concrete bookable slots in a date range for one appointment type."""

    @abstractmethod
    def list_patient_appointments(
        self,
        *,
        patient_id: str,
        days_back: int = 0,
        days_ahead: int = 60,
    ) -> list[Appointment]:
        """Upcoming / recent appointments for one patient.

        Used by the Locate Appointment / Cancel / Reschedule protocols to
        find what the caller wants to act on. Default window is the next
        60 days; adapters that need a wider net for their flow can pass
        ``days_back > 0`` to include very recent past appointments
        (useful for "I had an appointment yesterday but didn't show up"
        flows).
        """

    @abstractmethod
    def book(
        self,
        *,
        event_type_id: int,
        start_date: str,
        start_time: str,
        location_id: int | None = None,
        patient_id: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        phone: str | None = None,
        notes: str | None = None,
    ) -> BookingResult:
        """Create an appointment.

        ``end_time`` is derived server-side from the appointment type's
        configured duration — the agent doesn't do time math.

        ``location_id`` is the caller-chosen location (when the clinic
        prompts for it). When omitted, the adapter resolves the clinic's
        sole location; a multi-location clinic with no ``location_id``
        is an error.

        Pass ``patient_id`` for an existing patient; otherwise pass
        ``first_name`` + ``last_name`` + ``phone`` and the adapter will
        QuickAdd a new patient as part of the booking.
        """

    @abstractmethod
    def cancel(self, *, appointment_id: str) -> BookingResult:
        """Cancel an appointment. Returns echo details for the agent's
        confirmation message back to the caller.
        """

    @abstractmethod
    def reschedule(
        self,
        *,
        appointment_id: str,
        new_start_date: str,
        new_start_time: str,
    ) -> BookingResult:
        """Move an existing appointment to a new slot.

        Implementations that can't atomically move an appointment (e.g.
        Blueprint requires cancel-then-create) book the new slot FIRST,
        then cancel the old. If the cancel fails after the book succeeds,
        return ``status="partial"`` with a ``warning`` describing the
        cleanup need — the caller's new booking is in place, but the old
        one is still standing and staff must intervene.
        """


# ── Factory ───────────────────────────────────────────────────────────────────


def adapter_for(clinic) -> PMSAdapter:
    """Return the adapter matching `clinic.pms_type`.

    Imports the concrete adapter module lazily so this layer doesn't
    pull in every PMS's HTTP stack at import time. Callers needing
    HTTP-backed methods (list_appointment_types, find_available_slots)
    must construct the adapter with the http config they fetched via the
    PMS-specific config loader — see each adapter's docstring.
    """
    pms = (getattr(clinic, "pms_type", None) or "none").strip().lower()
    if pms == "blueprint":
        from api.voice_agent.pms.blueprint import BlueprintAdapter
        return BlueprintAdapter(clinic_id=clinic.clinic_id)
    raise ValueError(f"No PMS adapter for pms_type={pms!r}")
