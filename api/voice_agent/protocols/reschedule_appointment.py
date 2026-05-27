"""RescheduleAppointment — move an existing appointment to a new slot.

Server orchestrates book-new-then-cancel-old (Blueprint's Edit endpoint
can't change start/end time, only status). The agent passes the existing
appointment_id + the new (date, time); the server resolves the new
end_time from the original event type's duration.

The flow is non-atomic — if the new booking succeeds but cancelling the
old one fails, the response status is "partial" with a ``warning``
describing the cleanup need. The agent surfaces this honestly to the
caller and captures it in the ticket.
"""
from __future__ import annotations

import os

from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")


class RescheduleAppointmentProtocol(Protocol):
    id = "reschedule_appointment"
    display_name = "Reschedule Appointment"
    description = (
        "Move a verified caller's existing appointment to a new slot. Requires "
        "Verify Caller Identification, Locate Appointment, and Search Appointment "
        "Availability so the new slot is real."
    )
    agent_tool_name = "reschedule_appointment"
    supported_pms = ("blueprint",)

    def _tool_url(self) -> str:
        if self.pms_type == "blueprint":
            return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/appointments/reschedule"
        raise NotImplementedError(f"reschedule_appointment not routed for pms={self.pms_type}")

    def tools(self) -> list[dict]:
        return [{
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Move an existing appointment to a new date/time. Server-side this "
                "books the new slot first, then cancels the old; if the new "
                "booking fails the old appointment is untouched. The same "
                "appointment type (and duration) carries over to the new slot. "
                "Returns {status: 'rescheduled' | 'partial', summary, start_time, "
                "end_time, warning?}. status='partial' means the new booking "
                "landed but the old one couldn't be cancelled — surface this to "
                "the caller and capture it in the ticket."
            ),
            "url": self._tool_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "string",
                        "description": (
                            "The appointment_id from locate_appointment for the "
                            "booking being moved. Required."
                        ),
                    },
                    "new_start_date": {
                        "type": "string",
                        "description": "New appointment date in YYYY-MM-DD (clinic-local).",
                    },
                    "new_start_time": {
                        "type": "string",
                        "description": (
                            "New start time in HH:MM (24h, clinic-local). MUST be "
                            "one of the slots find_available_slots returned for "
                            "this date + the appointment's event type."
                        ),
                    },
                },
                "required": ["appointment_id", "new_start_date", "new_start_time"],
            },
        }]

    @property
    def prompt_fragment(self) -> str:
        return """## Reschedule Appointment
Use this to move an existing appointment to a new date/time. The same appointment type carries over — you're not changing what kind of visit it is, just when.

### Preconditions (all must be true before calling `reschedule_appointment`)
1. **Caller is verified.** `verify_caller_identification` returned `matched`.
2. **Appointment is located.** `locate_appointment` returned the caller's bookings; you've read them back and confirmed which one to move. Use its `appointment_id`.
3. **New slot is real.** You called `find_available_slots` (Search Appointment Availability) for the SAME `event_type_id` as the appointment being moved. The new `new_start_time` MUST be one of the times returned for `new_start_date`.
4. **Caller has explicitly agreed to BOTH the move and the new slot.** Read it all back and get an explicit yes:
   > "Just to confirm — you'd like to move your Hearing Test from Tuesday May 30 at 10 AM to Friday June 2 at 2 PM. Should I go ahead?"

### How to call
Pass `appointment_id` (from locate), `new_start_date`, and `new_start_time`. End time is derived from the event type's duration.

### What you get back
- `status: "rescheduled"` — clean success. Confirm to the caller ("You're all set — your hearing test is now Friday June 2 at 2 PM.").
- `status: "partial"` — the new booking landed but the old one couldn't be cancelled. The `warning` field describes what staff must clean up. Tell the caller honestly: "I've booked your new time, but I had trouble cancelling the old appointment — a team member will sort that out so you don't end up double-booked." Capture the partial state in the ticket's `details` field; set `suggested_followup` to the warning text.

### If the call fails entirely
If the new booking itself fails (PMS returns an error before the cancel even runs), the old appointment is untouched. Apologize, capture the request in the ticket, and let the caller know a team member will reach out to confirm the move.

### Booking status
The new appointment lands in the PMS as **Tentative** (same as Book Appointment). Use language like "I've moved you" or "you're rescheduled" rather than "confirmed.\""""
