"""BookAppointment — create a new appointment in the PMS.

Requires Search Appointment Availability to have surfaced a concrete
(event_type_id, date, slot) tuple. For existing patients, also requires
Verify Caller Identification to have returned the opaque patient_id; for
new patients the booking QuickAdds them with the collected name + phone.

End time is derived server-side from the event type's duration — the
agent never does time math.
"""
from __future__ import annotations

import os

from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")


class BookAppointmentProtocol(Protocol):
    id = "book_appointment"
    display_name = "Book Appointment"
    description = (
        "Create a new appointment for an existing or new patient at a slot the "
        "agent surfaced via Search Appointment Availability. End time is derived "
        "server-side from the appointment type's duration."
    )
    agent_tool_name = "book_appointment"
    supported_pms = ("blueprint",)
    depends_on = ("search_appointment_availability",)

    def _tool_url(self) -> str:
        if self.pms_type == "blueprint":
            return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/appointments/book"
        raise NotImplementedError(f"book_appointment not routed for pms={self.pms_type}")

    def tools(self) -> list[dict]:
        return [{
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Create a new appointment. Returns {status, summary, start_time, "
                "end_time} so you can confirm the booking back to the caller. "
                "For existing patients, pass the patient_id from "
                "verify_caller_identification. For new patients, pass first_name + "
                "last_name + phone — the booking will QuickAdd the patient record. "
                "End time is computed from the appointment type's duration; you "
                "don't need to pass it."
            ),
            "url": self._tool_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "event_type_id": {
                        "type": "integer",
                        "description": (
                            "The appointment type ID from list_appointment_types. "
                            "Required."
                        ),
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Appointment date in YYYY-MM-DD (clinic-local).",
                    },
                    "start_time": {
                        "type": "string",
                        "description": (
                            "Appointment start time in HH:MM (24h, clinic-local). "
                            "This MUST be one of the slots find_available_slots "
                            "returned for this date — picking an unavailable time "
                            "will be rejected by the PMS."
                        ),
                    },
                    "patient_id": {
                        "type": "string",
                        "description": (
                            "The opaque patient_id from verify_caller_identification "
                            "(when status='matched'). Provide this for existing "
                            "patients. Omit for new patients."
                        ),
                    },
                    "first_name": {
                        "type": "string",
                        "description": (
                            "New patient's first name. Required if patient_id is omitted."
                        ),
                    },
                    "last_name": {
                        "type": "string",
                        "description": (
                            "New patient's last name. Required if patient_id is omitted."
                        ),
                    },
                    "phone": {
                        "type": "string",
                        "description": (
                            "New patient's callback phone (E.164 preferred). "
                            "Helpful for staff follow-up; required if patient_id is omitted."
                        ),
                    },
                    "notes": {
                        "type": "string",
                        "description": (
                            "Optional free-text notes to attach to the appointment. "
                            "Use this for anything staff should see at booking time "
                            "(e.g. 'caller mentioned tinnitus on the right side')."
                        ),
                    },
                },
                "required": ["event_type_id", "start_date", "start_time"],
            },
        }]

    @property
    def prompt_fragment(self) -> str:
        return """## Book Appointment
Use this protocol to actually create a booking in the PMS. It's the step AFTER the caller has agreed on a specific appointment type + date + time slot.

### Preconditions (must be true before calling `book_appointment`)
1. **Slot is real.** You called `find_available_slots` (Search Appointment Availability) and the `start_time` you'll pass is one of the times returned for the chosen `start_date`. Do not pick a "close" time that wasn't in the list — the PMS will reject it.
2. **Patient is identified.** Either:
   - Existing patient: `verify_caller_identification` returned `matched` and you have a `patient_id`, OR
   - New patient: you have the first name, last name, and callback phone number — AND you have spell-confirmed both names letter-by-letter before this step (the booking creates a new patient record from these fields, and a wrong letter is permanent). Example:
     > "Before I book this, let me spell your name back — first name J-O-H-N, last name S-M-Y-T-H-E. Got every letter right?"
3. **Caller has explicitly agreed to the booking.** Read back the appointment type, day, and time, and get a "yes, book it" before the tool call. Example:
   > "So that's a Hearing Test on Tuesday May 30 at 10 AM with Dr. Vader. Should I go ahead and book that?"

### How to call
- For existing patients: pass `event_type_id`, `start_date`, `start_time`, and `patient_id`.
- For new patients: pass `event_type_id`, `start_date`, `start_time`, `first_name`, `last_name`, and `phone`.
- Optional `notes`: anything specific the caller mentioned that staff should see (e.g. side of complaint, hearing-aid brand, accessibility needs).

You don't pass end_time — the server computes it from the appointment type's duration.

### What you get back
`{status: "booked", summary, start_time, end_time}` — confirm to the caller in plain language ("You're all set — Hearing Test on Tuesday May 30 at 10 AM. A team member will reach out if anything changes.")

### If the call fails
If `book_appointment` returns a non-success status or errors, apologize, capture the requested slot in the ticket's `details`, set `suggested_followup` to "Staff to manually book the requested slot — automated booking failed", and let the caller know a team member will follow up.

### Booking status
New bookings land in the PMS as **Tentative**. Clinic staff confirm them. Don't tell the caller "your appointment is confirmed" — say "you're booked" or "we've got you on the schedule" so you don't promise something staff might need to adjust."""
