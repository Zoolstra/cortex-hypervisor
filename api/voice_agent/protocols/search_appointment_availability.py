"""SearchAppointmentAvailability — multi-tool protocol bundling the two
appointment-search operations into one toggleable unit.

Replaces the legacy ``ListAppointmentTypesProtocol`` +
``FindAvailableSlotsProtocol`` pair. Tool names (``list_appointment_types``
and ``find_available_slots``) and backend URLs stay the same — only the
toggling surface collapses from two checkboxes to one.

Why combine: the two operations are useless apart. ``find_available_slots``
requires an ``event_type_id`` you can only get from
``list_appointment_types``. Splitting them as separate toggles let an
operator accidentally enable one without the other.
"""
from __future__ import annotations

import os

from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")


class SearchAppointmentAvailabilityProtocol(Protocol):
    id = "search_appointment_availability"
    display_name = "Search Appointment Availability"
    description = (
        "Surface the clinic's bookable appointment types and concrete available "
        "time slots to the agent so it can quote real options to callers. "
        "Required for Book Appointment and Reschedule Appointment."
    )
    # Multi-tool: this protocol contributes 2 VAPI tools. We don't set
    # `agent_tool_name` — the dashboard surfaces the protocol-level
    # display_name; the two tool names live on the tools themselves.
    supported_pms = ("blueprint", "audit_data")

    def _types_url(self) -> str:
        if self.pms_type == "blueprint":
            return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/appointment-types"
        if self.pms_type == "audit_data":
            return f"{_CORTEX_BASE}/audit_data/{self.clinic_id}/appointment-types"
        raise NotImplementedError(f"appointment-types not routed for pms={self.pms_type}")

    def _slots_url(self) -> str:
        if self.pms_type == "blueprint":
            return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/availability/find"
        if self.pms_type == "audit_data":
            return f"{_CORTEX_BASE}/audit_data/{self.clinic_id}/availability/find"
        raise NotImplementedError(f"availability/find not routed for pms={self.pms_type}")

    def tools(self) -> list[dict]:
        return [self._list_types_tool(), self._find_slots_tool()]

    def _list_types_tool(self) -> dict:
        return {
            "type": "apiRequest",
            "name": "list_appointment_types",
            "description": (
                "Return the clinic's bookable appointment types as a list of "
                "{id, name, duration_minutes}. Call this once before "
                "find_available_slots so you know which event_type_id to "
                "use. Match the caller's stated need to one of the names — "
                "for hearing concerns the type is usually 'Hearing test'; "
                "for hearing-aid fitting the type is usually 'Fitting'."
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
                            "A brief phrase describing why you're calling this tool "
                            "(e.g. 'caller wants a hearing test'). Optional — used "
                            "for call observability only; the server ignores it. "
                            "VAPI's schema requires the body to declare at least "
                            "one property."
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
                "Find bookable appointment slots in a date range for a specific "
                "appointment type. Returns {days: [{date, available_times: [HH:MM, ...]}]}. "
                "You MUST supply event_type_id — get it from list_appointment_types "
                "first. Use a 1-2 week window unless the caller specifies otherwise."
            ),
            "url": self._slots_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "event_type_id": {
                        "type": "integer",
                        "description": (
                            "The appointment type ID from list_appointment_types. "
                            "Required — without it the search has no idea what "
                            "duration / resource constraints apply."
                        ),
                    },
                    "start_date": {
                        "type": "string",
                        "description": (
                            "Start of the search window in YYYY-MM-DD format "
                            "(clinic local time)."
                        ),
                    },
                    "end_date": {
                        "type": "string",
                        "description": (
                            "End of the search window in YYYY-MM-DD format "
                            "(clinic local time, inclusive)."
                        ),
                    },
                },
                "required": ["event_type_id", "start_date", "end_date"],
            },
        }

    @property
    def prompt_fragment(self) -> str:
        return """## Search Appointment Availability
When the caller asks about availability — for a new booking, a reschedule, or general "when can I come in?" questions — use this protocol's two tools in sequence:

### Step 1: `list_appointment_types`
Call this FIRST. The response is a list of `{id, name, duration_minutes}`.

Match the caller's stated need to a `name` ONLY if the match is unambiguous. Examples:
- caller: "Hearing test" / "test my hearing" → match a type named 'Hearing test' (or close variant like 'Hearing assessment').
- caller: "Pick up my hearing aids" / "fitting" → match a type named 'Fitting'.
- caller: "Hearing aid maintenance" / "my aid is broken" → match 'Hearing Aid Maintenance/Problem' or similar.

**Critical rule — do NOT guess.** If no returned name clearly matches the caller's stated need, DO NOT pick the closest one. In that case:
1. Tell the caller something like: "I want to make sure we get the right appointment type for you — let me note your request and have a team member confirm the booking when they call you back."
2. SKIP `find_available_slots` entirely.
3. Still ask for their preferred day(s) and time window — capture it in the ticket's `details` field (e.g. `details.preferred_window = "Tuesday or Wednesday afternoon"`).
4. Set `suggested_followup` on the ticket to something like "Confirm appointment type with caller and book — caller asked for hearing test but no matching type configured in PMS."

When you DO have a clear match, hold onto the matching `id` AND `duration_minutes` — you'll pass the id to `find_available_slots`, and the duration is useful context if you go on to Book or Reschedule.

### Step 2: `find_available_slots`
Call this with the matched `event_type_id` and a 1-2 week date range. The response is `{days: [{date, available_times: [HH:MM, ...]}]}` — concrete bookable slots, not just provider work blocks. Use it to tell the caller which days/times look open ("Tuesday morning has 9am, 9:30, and 10:30 open").

If the response has zero days/slots, tell the caller you can't see availability in that window and ask for their preferred day/time as text — capture in the ticket. Don't keep retrying with different ranges.

Note: this protocol only SEARCHES availability. It does NOT book. If the caller wants to actually book the appointment, the Book Appointment protocol handles that step; otherwise capture the preferred slot in the ticket and let staff confirm."""
