"""LocateAppointment — surface a verified caller's upcoming appointments.

Used as a precondition by Cancel Appointment and Reschedule Appointment
(the agent must know WHICH appointment to act on), and useful on its own
when a caller asks "do I have anything coming up?"

Requires Verify Caller Identification to have succeeded — the tool input
``patient_id`` is the opaque ID returned by that protocol. The
``onlineBookingSecret`` Blueprint uses to authenticate cancel/reschedule
PUTs is never exposed; the server re-resolves it from the appointment_id
when the act-on protocols run.
"""
from __future__ import annotations

import os

from api.voice_agent.protocols.base import Protocol


_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")


class LocateAppointmentProtocol(Protocol):
    id = "locate_appointment"
    display_name = "Locate Appointment"
    description = (
        "Look up a verified caller's upcoming appointments by patient ID. "
        "Required precondition for Cancel Appointment and Reschedule Appointment; "
        "also usable standalone when a caller asks what they have booked."
    )
    agent_tool_name = "locate_appointment"
    supported_pms = ("blueprint",)

    def _tool_url(self) -> str:
        if self.pms_type == "blueprint":
            return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/appointments/locate"
        raise NotImplementedError(f"locate_appointment not routed for pms={self.pms_type}")

    def tools(self) -> list[dict]:
        return [{
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Return a verified patient's upcoming appointments. Each item has "
                "{appointment_id, event_type_name, summary, start_time, end_time, "
                "provider_name, location_name, status}. The appointment_id is what "
                "you pass to cancel_appointment or reschedule_appointment later. "
                "Times are in clinic-local time, ISO-8601 format. Only call this "
                "after verify_caller_identification has returned status='matched' — "
                "the patient_id is the one that tool returned."
            ),
            "url": self._tool_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "patient_id": {
                        "type": "string",
                        "description": (
                            "The opaque patient_id returned by "
                            "verify_caller_identification when status='matched'. "
                            "Required."
                        ),
                    },
                    "days_ahead": {
                        "type": "integer",
                        "description": (
                            "How many days into the future to search. Default 60. "
                            "Most callers care about the next few weeks; extend if "
                            "the caller says they have something booked further out."
                        ),
                    },
                },
                "required": ["patient_id"],
            },
        }]

    @property
    def prompt_fragment(self) -> str:
        return """## Locate Appointment
Use this when a caller wants to do something with an existing appointment — cancel it, reschedule it, ask about it, or just confirm what they have booked.

### Precondition
The caller must be a verified existing patient. If `verify_caller_identification` returned `matched`, use the returned `patient_id`. If they're a new patient or verification failed, you have no opaque id to look them up by — capture their request in the ticket and let staff handle it.

### How to call
Call `locate_appointment` with the verified `patient_id`. Default `days_ahead` is 60 days; if the caller mentions something further out ("my appointment in October"), extend the window.

### What you get back
A list of appointments with appointment_id, event_type_name, start_time, end_time, provider_name, status. If the list is empty, the caller has nothing booked in the window — read that back and ask if they meant something further out or a different clinic.

### How to use it
Read back the appointments to the caller in plain language ("I see a Hearing Test with Dr. Vader on Tuesday May 30 at 10 AM"). Confirm which one they mean.

**Keep appointment_id internal.** Don't read appointment IDs to the caller — they're an internal handle. Just describe the appointment naturally (type, date, time, provider).

If the caller wants to act on the appointment (cancel or reschedule), pass its appointment_id to the appropriate protocol. If they just wanted to confirm, summarize and move on."""
