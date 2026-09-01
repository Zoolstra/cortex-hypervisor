"""ConfirmAppointment — a caller confirms they will attend a booking.

The inbound half of appointment confirmation: the patient rings in ("I'm
calling to confirm my appointment") and the agent flips the booking to
Blueprint status 0 (Confirmed) so the schedule reflects it with no staff step.

Requires Verify Caller Identification (the caller is who they say) + Locate
Appointment (which booking they mean). The agent passes the appointment_id
Locate returned; the server re-resolves the ``onlineBookingSecret`` and issues
the PUT. Same write path as Cancel, different status code.

**Not every appointment can be written.** Blueprint authorises Edit Appointment
with an ``onlineBookingSecret`` it mints only for bookings created through
Online Appointment Booking or the Scheduling API — so an appointment a
receptionist entered in the OMS has none and its status cannot be changed over
the API at all. That comes back as ``pending_staff_confirmation``: the caller's
answer is recorded and handed to staff, and the agent must NOT tell them the
appointment is confirmed. Expect this to be the common outcome until Blueprint
relaxes the constraint, because staff-booked appointments are most of them.

Deliberately confirms TENTATIVE bookings, including ones this agent created.
Tentative is precisely the state a confirmation resolves — an agent-booked
appointment the patient then rings to confirm is the workflow working, not a
bypass. Statuses that cannot be attended (cancelled, completed, no-show,
in-progress) come back ``not_confirmable`` and the agent says so.
"""
from __future__ import annotations

import os

from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")


class ConfirmAppointmentProtocol(Protocol):
    id = "confirm_appointment"
    display_name = "Confirm Appointment"
    description = (
        "Let a verified caller confirm they will attend an upcoming "
        "appointment. Marks the booking Confirmed in the PMS. Requires Verify "
        "Caller Identification + Locate Appointment to identify which booking "
        "is being confirmed."
    )
    agent_tool_name = "confirm_appointment"
    supported_pms = ("blueprint",)
    depends_on = ("verify_caller_identification", "locate_appointment")

    def _tool_url(self) -> str:
        if self.pms_type == "blueprint":
            return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/appointments/confirm"
        raise NotImplementedError(
            f"confirm_appointment not routed for pms={self.pms_type}"
        )

    def tools(self) -> list[dict]:
        return [{
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Mark an upcoming appointment as Confirmed after the caller says "
                "they will attend. Returns {status, appointment_id, summary, "
                "start_time, end_time, warning}. status is 'confirmed' when the "
                "booking is now confirmed (including when it already was); "
                "'not_confirmable' when it cannot be — cancelled, already past, "
                "or already in progress; or 'pending_staff_confirmation' when "
                "the clinic's system will not accept the update and a team "
                "member must apply it, which is common and is NOT an error. On "
                "either of the last two, `warning` says why and you must tell "
                "the caller instead of claiming success. The "
                "appointment_id comes from locate_appointment. Only call this "
                "after the caller has clearly said they will be attending the "
                "specific appointment you read back to them."
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
                            "The appointment_id of the booking the caller is "
                            "confirming, exactly as locate_appointment returned it."
                        ),
                    },
                },
                "required": ["appointment_id"],
            },
        }]

    @property
    def prompt_fragment(self) -> str:
        return """## Confirm Appointment
When a caller rings to confirm they'll be attending — "I'm calling to confirm", "just checking I'm still booked in", or they say yes to a reminder — use this after verifying who they are.

1. Call `locate_appointment` to find their upcoming bookings.
2. **Read back the one you're about to confirm** — appointment type, day, and time — and get a clear yes. If they have MORE than one upcoming, name them and ask which they mean; never assume the soonest. If they say "all of them", confirm them one at a time, reading each back first.
3. Call `confirm_appointment` with that `appointment_id`.
4. On `status: "confirmed"`, tell them plainly it's confirmed ("You're all set for Tuesday the 12th at ten thirty"). This is the one appointment state you MAY call confirmed rather than tentative — the PMS now says so.
5. On `status: "not_confirmable"`, do NOT say it's confirmed. Read the `warning` as plain speech ("that one shows as cancelled on my end"), offer to book a new appointment if they want one, and put it in the ticket for staff.
6. On `status: "pending_staff_confirmation"`, their answer IS recorded — the clinic's system just won't take the update from me. Thank them and say a person will finalise it: "Thanks — I've got you down as attending, and I'll have someone on the team mark it off in the schedule." Do NOT say it's confirmed, do NOT apologise at length, do NOT say anything went wrong, and do NOT offer to rebook — the appointment is still there. Always write this to the ticket with the appointment day and time and `suggested_followup` set to "mark this appointment Confirmed", because a staff member flipping that status is the only thing that completes it.

If they have no upcoming appointments, say so, offer to book one, and note it in the ticket. Never invent an appointment to confirm, and never confirm one you did not read back to the caller first."""
