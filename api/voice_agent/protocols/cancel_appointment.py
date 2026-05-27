"""CancelAppointment — cancel an existing appointment.

Requires Verify Caller Identification (caller is who they say) + Locate
Appointment (caller has confirmed which booking to cancel). The agent
passes the appointment_id from Locate; the server re-resolves the
Blueprint ``onlineBookingSecret`` internally and issues the PUT with
status=3 (Cancelled).
"""
from __future__ import annotations

import os

from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")


class CancelAppointmentProtocol(Protocol):
    id = "cancel_appointment"
    display_name = "Cancel Appointment"
    description = (
        "Cancel a verified caller's existing appointment. Requires Verify "
        "Caller Identification + Locate Appointment to identify which booking "
        "is being acted on."
    )
    agent_tool_name = "cancel_appointment"
    supported_pms = ("blueprint",)

    def _tool_url(self) -> str:
        if self.pms_type == "blueprint":
            return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/appointments/cancel"
        raise NotImplementedError(f"cancel_appointment not routed for pms={self.pms_type}")

    def tools(self) -> list[dict]:
        return [{
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Cancel an existing appointment. Returns {status, appointment_id, "
                "summary, start_time, end_time} so you can confirm to the caller "
                "what was cancelled. Only call this AFTER the caller has explicitly "
                "confirmed which specific appointment to cancel and said 'yes, "
                "cancel it'. The appointment_id comes from locate_appointment."
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
                            "specific booking the caller wants to cancel. Required."
                        ),
                    },
                },
                "required": ["appointment_id"],
            },
        }]

    @property
    def prompt_fragment(self) -> str:
        return """## Cancel Appointment
Use this when a caller wants to cancel a specific existing appointment.

### Preconditions (do NOT call `cancel_appointment` before these are met)
1. **Caller is verified.** `verify_caller_identification` returned `matched`. If they're a new patient or verification failed, you cannot cancel an existing appointment — capture the cancellation request in the ticket and let staff handle it.
2. **Appointment is located.** `locate_appointment` returned the caller's bookings, and you've read them back to confirm which one to cancel. Use the `appointment_id` of the confirmed booking.
3. **Caller has explicitly confirmed the cancellation.** Read back the appointment in plain language and ask one final time. Example:
   > "Just to confirm — you'd like me to cancel your Hearing Test with Dr. Vader on Tuesday May 30 at 10 AM. Is that right?"
   Wait for an explicit yes before calling the tool.

### How to call
Pass the `appointment_id` from `locate_appointment`. That's all.

### What you get back
`{status: "cancelled", summary, start_time, end_time}` — confirm to the caller naturally ("Done — that appointment is cancelled. You'll get a confirmation from the clinic."). Capture the cancellation in the ticket details so staff have a record.

### Edge case — already cancelled
The status comes back as "cancelled" even if the booking was already in that state. That's fine — just confirm to the caller as if you'd done it.

### If the call fails
Apologize, capture the appointment details and the cancellation request in the ticket, set `suggested_followup` to "Manually cancel appointment — caller requested, automated cancel failed", and tell the caller a team member will follow up to confirm."""
