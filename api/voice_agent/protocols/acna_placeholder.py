"""ACNA placeholder-grid appointment protocols.

Audiology Clinic of Northern Alberta does not use Blueprint's native
online-booking availability (no provider availability schedules are
maintained, so ``GET /rest/availability/`` returns nothing). Instead every
real appointment type is paired with a **placeholder** type. The clinic seeds
the schedule grid with placeholder appointments — never linked to a patient,
each naming a provider and occupying one of the physical rooms — and a
placeholder is "available" until a real appointment for that same provider is
booked at the same time.

These two protocols mirror the generic Search Appointment Availability + Book
Appointment surface but derive availability from the appointment-search grid
(see ``BlueprintAdapter.find_placeholder_availability`` /
``book_into_placeholder``). They are **clinic-scoped** (``supported_clinics``)
so they only appear for ACNA.

The placeholder→real type pairing is per-clinic config (``type_pairs``), seeded
with the Annual pair (placeholder ``Z Space RA12M`` = 200 → real ``Annual`` =
207). Additional pairs are added as config, not code. The pairing is stored on
the SEARCH protocol's config; the book protocol reads the same rows (it
``depends_on`` search).
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")

# Clinic-scope gate. ACNA is the only clinic whose scheduling uses the
# placeholder-grid convention. Adding another such clinic = append its id here.
ACNA_CLINIC_ID = "0b5f0929-31fb-4e21-9dd4-030bd040335d"
_SUPPORTED_CLINICS = (ACNA_CLINIC_ID,)


class TypePair(BaseModel):
    """One placeholder↔real appointment-type pairing.

    ``placeholder_event_type_id`` is the Blueprint eventTypeId of the
    capacity-placeholder type (never patient-linked; e.g. 200 'Z Space RA12M').
    ``real_event_type_id`` is the type a booking actually creates (e.g. 207
    'Annual'). ``display_name`` is what the agent matches a caller's stated need
    against ("Annual", "Annual hearing test").
    """

    placeholder_event_type_id: int
    real_event_type_id: int
    display_name: str

    model_config = {"extra": "forbid"}


class ACNAAvailabilityConfig(BaseModel):
    """Per-clinic config for the placeholder-grid protocols.

    Seeded with the Annual pair. The search protocol owns this config; the book
    protocol resolves the same pairs server-side from the search protocol's row.
    """

    type_pairs: list[TypePair] = Field(
        default_factory=lambda: [
            TypePair(placeholder_event_type_id=200, real_event_type_id=207,
                     display_name="Annual"),
            # Technician clean-and-check. Placeholder 8 ('Z Maintenance/RA',
            # 30m, titled "C&C RA 1"/"C&C RA 2") → real 204 ('Service', 30m).
            # Derived from ACNA's booked history, not guessed: of 1,786 Service
            # appointments only the technician-grid ones are placeholder-paired
            # at a matching duration, and 'Z Maintenance/RA' carries only the
            # two technicians. Named for what the visit IS so the agent does
            # not offer it as a clinician appointment — a CLINICIAN service
            # visit has no placeholder grid (11% paired, durations mismatched)
            # and stays unbookable by design.
            TypePair(placeholder_event_type_id=8, real_event_type_id=204,
                     display_name="Hearing Aid Clean and Check"),
        ]
    )

    # Placeholder rows naming these "providers" are NOT offered or booked:
    # pseudo-providers (e.g. the unassigned-capacity marker "Clinician, Z NEW")
    # have no Blueprint availability schedule, so booking against them fails
    # with BOOKING_OUTSIDE_AVAILABILITY — verified live 2026-07-27. Staff can
    # still assign those slots manually.
    excluded_placeholder_providers: list[str] = Field(
        default_factory=lambda: ["Clinician, Z NEW"]
    )

    model_config = {"extra": "ignore"}


class ACNASearchAvailabilityProtocol(Protocol):
    id = "acna_search_availability"
    display_name = "Search Availability (ACNA placeholder grid)"
    description = (
        "ACNA-specific availability search. Derives bookable annual (and other "
        "paired-type) slots from the schedule grid's placeholder appointments "
        "rather than Blueprint online-booking availability. Surfaces the open "
        "times and which providers are free. Required for ACNA Book Appointment."
    )
    supported_pms = ("blueprint",)
    supported_clinics = _SUPPORTED_CLINICS
    config_model = ACNAAvailabilityConfig

    def _types_url(self) -> str:
        return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/placeholder/appointment-types"

    def _find_url(self) -> str:
        return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/placeholder/availability/find"

    def tools(self) -> list[dict]:
        return [self._list_types_tool(), self._find_slots_tool()]

    def _list_types_tool(self) -> dict:
        return {
            "type": "apiRequest",
            "name": "list_appointment_types",
            "description": (
                "Return this clinic's bookable appointment types as a list of "
                "{real_event_type_id, name, duration_minutes}. Call this ONCE "
                "before find_available_slots so you know which real_event_type_id "
                "to search. Match the caller's stated need to a name (e.g. an "
                "annual hearing check → 'Annual')."
            ),
            "url": self._types_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": (
                            "Brief phrase describing why you're calling this "
                            "(observability only; server ignores it). VAPI "
                            "requires at least one body property."
                        ),
                    },
                },
            },
        }

    def _find_slots_tool(self) -> dict:
        return {
            "type": "apiRequest",
            "name": "find_available_slots",
            "description": (
                "Find bookable slots for one appointment type over a date range. "
                "Returns {days: [{date, slots: [{time, providers: [names]}]}]} — "
                "each slot lists which providers are open at that time. Use a 1-2 "
                "week window unless the caller specifies otherwise. Pass "
                "provider_name only if the caller named a provider they want."
            ),
            "url": self._find_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "real_event_type_id": {
                        "type": "integer",
                        "description": (
                            "The real_event_type_id of the appointment type being "
                            "booked — use the id another tool already gave you "
                            "(determine_appointment_type, or list_appointment_types "
                            "if available). Never invent one. Required."
                        ),
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start of the window, YYYY-MM-DD (clinic-local).",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End of the window, YYYY-MM-DD (clinic-local, inclusive).",
                    },
                    "provider_name": {
                        "type": "string",
                        "description": (
                            "Optional. Only set this if the caller asked for a "
                            "specific provider — narrows results to that provider's "
                            "open slots."
                        ),
                    },
                },
                "required": ["real_event_type_id", "start_date", "end_date"],
            },
        }

    @property
    def prompt_fragment(self) -> str:
        return """## Search Appointment Availability
When the caller asks about availability for a booking, use this protocol's two tools in sequence.

### Step 1: `list_appointment_types`
Call this FIRST. The response is a list of `{real_event_type_id, name, duration_minutes}`. Match the caller's stated need to a `name` ONLY if the match is unambiguous (e.g. "annual", "yearly hearing check" → 'Annual'). If nothing clearly matches, DO NOT guess — capture the request in a ticket and let staff confirm the appointment type.

Hold onto the matching `real_event_type_id` — you pass it to `find_available_slots` and, later, to `book_appointment`.

### Step 2: `find_available_slots`
Call with the matched `real_event_type_id` and a 1-2 week date range. The response is `{days: [{date, slots: [{time, providers: [...]}]}]}`. Each slot's `providers` are the clinicians open at that time.

Presenting slots to the caller:
- Quote days/times naturally ("Tuesday has 11 AM and 1 PM open").
- If the caller asked for a specific provider, pass `provider_name` and offer only that provider's times. Otherwise you don't need to name a provider — just offer the time.
- Every provider name in the results is a real, bookable clinician (unassigned placeholder rooms are filtered out server-side).
- If the caller volunteers a provider preference and that provider has an open slot, offer it. If their preferred provider has nothing open, say so and offer the nearest open times with other providers.
- If there are many results, summarize ("we have several mornings open next week") rather than listing every slot.

If the response has zero days/slots, tell the caller you can't see availability in that window, ask their preferred day/time as text, capture it in the ticket, and don't keep retrying different ranges.

This protocol only SEARCHES. Booking is the Book Appointment protocol."""


class ACNABookAppointmentProtocol(Protocol):
    id = "acna_book_appointment"
    display_name = "Book Appointment (ACNA placeholder grid)"
    description = (
        "ACNA-specific booking. Creates the real appointment into a free "
        "placeholder space surfaced by ACNA Search Availability — resolving the "
        "provider and room from the grid. End time is derived server-side from "
        "the appointment type's duration."
    )
    agent_tool_name = "book_appointment"
    supported_pms = ("blueprint",)
    supported_clinics = _SUPPORTED_CLINICS
    depends_on = ("acna_search_availability",)

    def _book_url(self) -> str:
        return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/placeholder/appointments/book"

    def tools(self) -> list[dict]:
        return [{
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Create a new appointment in a free slot surfaced by "
                "find_available_slots. Returns {status, summary, start_time, "
                "end_time}. Pass the real_event_type_id, the chosen start_date + "
                "start_time, and — if the caller wanted a specific provider — "
                "provider_name. For existing patients pass patient_id (from "
                "verify_caller_identification); for new patients pass first_name + "
                "last_name + phone (a patient record is QuickAdded). End time is "
                "computed server-side."
            ),
            "url": self._book_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "real_event_type_id": {
                        "type": "integer",
                        "description": (
                            "The real_event_type_id of the appointment type being "
                            "booked — the same id you passed to "
                            "find_available_slots. Required."
                        ),
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Appointment date, YYYY-MM-DD (clinic-local).",
                    },
                    "start_time": {
                        "type": "string",
                        "description": (
                            "Start time, HH:MM (24h, clinic-local). MUST be one of "
                            "the times find_available_slots returned for this date."
                        ),
                    },
                    "provider_name": {
                        "type": "string",
                        "description": (
                            "Optional. Set only if the caller chose a specific "
                            "provider that was shown as open at this slot."
                        ),
                    },
                    "patient_id": {
                        "type": "string",
                        "description": (
                            "Opaque patient_id from verify_caller_identification "
                            "(status='matched'). Provide for existing patients; "
                            "omit for new patients."
                        ),
                    },
                    "first_name": {
                        "type": "string",
                        "description": "New patient's first name. Required if patient_id omitted.",
                    },
                    "last_name": {
                        "type": "string",
                        "description": "New patient's last name. Required if patient_id omitted.",
                    },
                    "phone": {
                        "type": "string",
                        "description": (
                            "New patient's callback phone. Required if patient_id omitted."
                        ),
                    },
                    "notes": {
                        "type": "string",
                        "description": (
                            "Optional free-text notes for staff (side of complaint, "
                            "accessibility needs, screening answers). Single field — "
                            "merge everything into one string."
                        ),
                    },
                },
                "required": ["real_event_type_id", "start_date", "start_time"],
            },
        }]

    @property
    def prompt_fragment(self) -> str:
        return """## Book Appointment
Use this to actually create the booking, AFTER the caller has agreed on an appointment type + date + time.

### Preconditions (must be true before calling `book_appointment`)
1. **Slot is real.** You called `find_available_slots` and the `start_time` you pass is one of the times it returned for the chosen `start_date`. Don't pick a "close" time — booking will be rejected.
2. **Patient is identified.** Either an existing patient (`verify_caller_identification` returned `matched` → use `patient_id`), OR a new patient where you have first name, last name, and callback phone — and you have spell-confirmed both names letter-by-letter first (a wrong letter creates a permanent bad record).
3. **Caller agreed.** Read back the appointment type, day, and time and get a clear "yes, book it".

### Provider
- If the caller chose a specific provider that was shown open at the slot, pass `provider_name`. Otherwise omit it — the clinic assigns the room/provider from the open placeholder.

### What you get back
`{status: "booked", summary, start_time, end_time}` — confirm in plain language ("You're on the schedule — Annual on Tuesday July 21 at 11 AM. A team member will reach out if anything changes.").

Bookings land as **Tentative**; staff confirm them. Say "you're booked" / "we've got you on the schedule", not "confirmed".

### If it fails
If `book_appointment` errors or returns a non-success status, apologize, capture the requested slot in the ticket's `details`, set `suggested_followup` to "Staff to manually book — automated booking failed", and tell the caller a team member will follow up."""
