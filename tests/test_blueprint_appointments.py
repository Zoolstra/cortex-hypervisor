"""
Adapter + route tests for the v2 protocol set:
- /blueprint/{id}/appointments/locate
- /blueprint/{id}/appointments/book
- /blueprint/{id}/appointments/cancel
- /blueprint/{id}/appointments/reschedule

The Blueprint HTTP API is stubbed via a replacement ``httpx`` object
injected into the adapter's namespace, so we can assert the exact
request shapes the adapter emits (e.g. cancel must PUT with ``status=3``
and the recovered ``onlineBookingSecret``; reschedule must book-new
BEFORE cancel-old).

The Cloud SQL config loader is patched to return a fixed fake config so
no DB call happens. ``get_session`` is overridden to yield ``None`` —
the routes pass ``db`` through to ``_get_blueprint_config``, which is
patched, so the unused session is fine.

These tests cover behavioural invariants the prompt fragments and PHI
isolation rely on:
  - locate_appointment never returns onlineBookingSecret to the agent
  - cancel sends Blueprint Edit-Appointment with status=3
  - cancel of an already-cancelled appointment is a no-op echo (no extra PUT)
  - reschedule books the new slot BEFORE cancelling the old one
  - reschedule reports status="partial" if cancel-old fails after book-new succeeds
  - reschedule aborts cleanly (no PUT issued) if book-new fails
"""
from __future__ import annotations

import pytest
import httpx
from fastapi.testclient import TestClient

from api import app
from api.voice_agent.blueprint import verify_vapi_secret
from api.core.db import get_session


# ── Fake Blueprint config (avoids Cloud SQL + Secret Manager) ─────────────────

_FAKE_CONFIG = {
    "clinic_name": "Test Clinic",
    "api_url": "https://test.bp-solutions.net/test_clinic/rest/hello",
    "clinic_code": "TEST",
    "api_key": "FAKE_API_KEY",
    "timezone": "America/Vancouver",
    "instance_id": "test-instance",
    "user_id": "42",
    "prompt_for_location": False,
}

# clinicConfiguration locations the adapter resolves a booking against. A
# single location → the adapter books into it without an explicit choice.
_LOCATIONS = [{"id": 1, "name": "Main Clinic", "formattedAddress": "1 Main St"}]


def _clinic_config(appointment_types: list[dict]) -> dict:
    """clinicConfiguration response carrying both appointment types and the
    single bookable location the book/reschedule flows resolve against."""
    return {"appointmentTypes": appointment_types, "locations": _LOCATIONS}


def _avail(date: str, time: str = "10:00", provider_id: int = 50, location_id: int = 1):
    """GET /availability/ response with one open slot — what book() reads to
    derive the providerId for the chosen time."""
    return [{
        "date": date,
        "available": True,
        "availabilityTimes": [{
            "time": f"{time}:00-0600",
            "available": [{"providerId": provider_id, "locationId": location_id}],
            "resources": [],
            "appointmentId": None,
        }],
    }]


# ── HTTP stub ─────────────────────────────────────────────────────────────────


class StubResp:
    def __init__(self, status_code: int = 200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://stub")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("stub", request=request, response=response)

    def json(self):
        return self._json


class HttpxStub:
    """Drop-in replacement for the `httpx` module inside the adapter.

    Routes are matched by (method, URL suffix) using ``endswith`` — NOT
    substring containment. Substring matching gets confused by paths like
    ``/appointments/search`` which contain ``/appointments/`` as a prefix
    (the book endpoint), so the cleanest contract is exact-suffix match.
    Last-registered wins.
    """

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.routes: list[tuple[str, str, object]] = []

    def set(self, method: str, url_suffix: str, response) -> None:
        self.routes.append((method, url_suffix, response))

    def _dispatch(self, method: str, url: str, payload):
        self.calls.append((method, url, payload))
        for m, frag, resp in reversed(self.routes):
            if m == method and url.endswith(frag):
                return resp(payload) if callable(resp) else resp
        return StubResp(404, {})

    def get(self, url, params=None, timeout=None, **kw):
        return self._dispatch("GET", url, params)

    def post(self, url, json=None, timeout=None, **kw):
        return self._dispatch("POST", url, json)

    def put(self, url, json=None, timeout=None, **kw):
        return self._dispatch("PUT", url, json)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def stub(monkeypatch):
    """Stub Blueprint HTTP + Cloud SQL config + auth, yield the HttpxStub."""
    s = HttpxStub()
    # Replace the `httpx` binding inside the adapter's module — calls
    # like `httpx.get(...)` now go through the stub.
    monkeypatch.setattr("api.voice_agent.pms.blueprint.httpx", s)
    # Bypass Cloud SQL + Secret Manager. The adapter resolves config via the
    # name in pms.blueprint; the router calls _get_blueprint_config directly
    # via its own imported reference — patch both so neither hits Cloud SQL.
    monkeypatch.setattr(
        "api.voice_agent.pms.blueprint._get_blueprint_config",
        lambda db, clinic_id: dict(_FAKE_CONFIG),
    )
    monkeypatch.setattr(
        "api.voice_agent.blueprint._get_blueprint_config",
        lambda db, clinic_id: dict(_FAKE_CONFIG),
    )
    # Bypass VAPI auth + the database session.
    app.dependency_overrides[verify_vapi_secret] = lambda: None
    app.dependency_overrides[get_session] = lambda: iter([None])
    yield s
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    # raise_server_exceptions=False so an unhandled adapter exception
    # surfaces as the 500 the live VAPI agent would see, rather than
    # propagating into the test as a raw httpx.HTTPStatusError.
    return TestClient(app, raise_server_exceptions=False)


# Helper — appointment dict with the fields Blueprint Search Appointments returns.
def _appt(
    appointment_id: str = "10995_0",
    patient_id: int = 316,
    status: int = 0,
    event_type_id: int = 1,
    secret: str = "ABC123",
    start: str = "2026-06-03 10:00:00 GMT",
    end: str = "2026-06-03 10:30:00 GMT",
) -> dict:
    return {
        "appointment_id": appointment_id,
        "status": status,
        "patient_id": patient_id,
        "patient_name": "Solo, Han",
        "birthdate": "1942-07-13",
        "start_time": start,
        "end_time": end,
        "provider": "Vader",
        "provider_id": 50,
        "location": "Weatherfield",
        "location_id": 1,
        "busy": True,
        "onlineBookingSecret": secret,
        "eventTypeId": event_type_id,
        "contactVerified": False,
        "summary": "Hearing Test",
        "notes": "",
    }


# ── Locate Appointment ────────────────────────────────────────────────────────


def test_locate_filters_to_requested_patient_only(stub, client):
    """Blueprint Search returns ALL appointments in the window; the adapter
    must filter to the requested patient_id client-side.
    """
    stub.set("POST", "/appointments/search", StubResp(200, [
        _appt(appointment_id="10999_0", patient_id=15),    # different patient
        _appt(appointment_id="10995_0", patient_id=316),   # the one we want
    ]))
    stub.set("GET", "/clinicConfiguration/", StubResp(200, {
        "appointmentTypes": [{"id": 1, "name": "Hearing Test", "duration": 30}],
    }))

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/locate",
        json={"patient_id": "316"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["appointments"]) == 1
    a = body["appointments"][0]
    assert a["appointment_id"] == "10995_0"
    assert a["event_type_name"] == "Hearing Test"
    assert a["start_time"] == "2026-06-03T10:00"  # ISO-trimmed, no tz suffix
    assert a["status"] == "confirmed"


def test_locate_never_returns_online_booking_secret(stub, client):
    """PHI/credential isolation: the agent must never see the secret used
    to authenticate cancel/reschedule PUTs.
    """
    stub.set("POST", "/appointments/search", StubResp(200, [
        _appt(secret="SUPER_SECRET_VALUE"),
    ]))
    stub.set("GET", "/clinicConfiguration/", StubResp(200, {"appointmentTypes": []}))

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/locate",
        json={"patient_id": "316"},
    )
    assert resp.status_code == 200
    assert "SUPER_SECRET_VALUE" not in resp.text


def test_locate_returns_empty_list_when_no_matches(stub, client):
    stub.set("POST", "/appointments/search", StubResp(200, []))
    stub.set("GET", "/clinicConfiguration/", StubResp(200, {"appointmentTypes": []}))

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/locate",
        json={"patient_id": "316"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"appointments": []}


# ── Book Appointment ──────────────────────────────────────────────────────────


def test_book_derives_end_time_from_event_type_duration(stub, client):
    """The agent passes only start_date + start_time; the adapter looks up
    the appointment type's duration and derives end_time. We assert that
    the PMS POST carries (endTime - startTime) == duration.
    """
    posts: list[dict] = []

    def capture(payload):
        posts.append(payload)
        return StubResp(201, {})

    stub.set("GET", "/clinicConfiguration/", StubResp(200,
        _clinic_config([{"id": 1, "name": "Hearing Test", "duration": 45}])))
    stub.set("GET", "/availability/", StubResp(200, _avail("2026-06-15", "10:00")))
    stub.set("POST", "/appointments/", capture)

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/book",
        json={
            "event_type_id": 1,
            "start_date": "2026-06-15",
            "start_time": "10:00",
            "patient_id": "316",
        },
    )
    assert resp.status_code == 200, resp.text
    assert len(posts) == 1
    payload = posts[0]
    diff_minutes = (payload["endTime"] - payload["startTime"]) // 60
    assert diff_minutes == 45
    assert payload["patientId"] == 316
    assert payload["status"] == 2  # Tentative — staff confirms
    # Location resolved from the clinic's sole location; provider derived
    # from the open availability slot.
    assert payload["locationId"] == 1
    assert payload["providerId"] == 50


def test_book_rejects_unknown_event_type_id(stub, client):
    stub.set("GET", "/clinicConfiguration/", StubResp(200,
        _clinic_config([{"id": 1, "name": "Hearing Test", "duration": 30}])))
    resp = client.post(
        "/blueprint/CLINIC_X/appointments/book",
        json={
            "event_type_id": 99,
            "start_date": "2026-06-15",
            "start_time": "10:00",
            "patient_id": "316",
        },
    )
    assert resp.status_code == 400
    assert "Unknown event_type_id" in resp.text


def test_book_quickadd_path_for_new_patients(stub, client):
    """When no patient_id is provided, the booking carries a QuickAdd
    patient object with name + mobile phone digits.
    """
    posts: list[dict] = []
    stub.set("GET", "/clinicConfiguration/", StubResp(200,
        _clinic_config([{"id": 1, "name": "X", "duration": 30}])))
    stub.set("GET", "/availability/", StubResp(200, _avail("2026-06-15", "10:00")))
    stub.set("POST", "/appointments/", lambda p: (posts.append(p), StubResp(201, {}))[1])

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/book",
        json={
            "event_type_id": 1,
            "start_date": "2026-06-15",
            "start_time": "10:00",
            "first_name": "Jane",
            "last_name": "Doe",
            "phone": "+1 (604) 555-1234",
        },
    )
    assert resp.status_code == 200, resp.text
    assert len(posts) == 1
    p = posts[0]
    assert p["patient"]["quickAdd"] is True
    assert p["patient"]["firstName"] == "Jane"
    assert p["patient"]["lastName"] == "Doe"
    # Non-digits stripped
    assert p["patient"]["mobilePhoneNumber"] == "16045551234"
    assert "patientId" not in p


# ── Cancel Appointment ────────────────────────────────────────────────────────


def test_cancel_sends_put_with_status_3_and_secret(stub, client):
    """Cancel must: (1) Search to recover the onlineBookingSecret, then
    (2) PUT to /appointments/{id} with status=3 (Cancelled) + the secret
    + the configured userId.
    """
    puts: list[dict] = []
    stub.set("POST", "/appointments/search", StubResp(200, [
        _appt(secret="THE_SECRET"),
    ]))
    stub.set("PUT", "/appointments/10995_0",
             lambda p: (puts.append(p), StubResp(200, {}))[1])

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/cancel",
        json={"appointment_id": "10995_0"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["appointment_id"] == "10995_0"

    assert len(puts) == 1
    put = puts[0]
    assert put["onlineBookingSecret"] == "THE_SECRET"
    assert put["status"] == 3
    assert put["userId"] == 42  # from fake config


def test_cancel_404s_when_appointment_not_in_lookup_window(stub, client):
    stub.set("POST", "/appointments/search", StubResp(200, []))
    resp = client.post(
        "/blueprint/CLINIC_X/appointments/cancel",
        json={"appointment_id": "ghost_0"},
    )
    assert resp.status_code == 404


def test_cancel_is_idempotent_when_already_cancelled(stub, client):
    """Already-cancelled appointments echo as cancelled without issuing
    a redundant PUT (so the agent's confirmation message reads naturally
    instead of surfacing a duplicate-action error).
    """
    puts: list[dict] = []
    stub.set("POST", "/appointments/search", StubResp(200, [
        _appt(status=3),  # Cancelled
    ]))
    stub.set("PUT", "/appointments/10995_0",
             lambda p: (puts.append(p), StubResp(200, {}))[1])

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/cancel",
        json={"appointment_id": "10995_0"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert puts == []  # no PUT issued


def test_cancel_never_returns_online_booking_secret(stub, client):
    stub.set("POST", "/appointments/search", StubResp(200, [
        _appt(secret="DONT_LEAK_ME"),
    ]))
    stub.set("PUT", "/appointments/10995_0", StubResp(200, {}))

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/cancel",
        json={"appointment_id": "10995_0"},
    )
    assert resp.status_code == 200
    assert "DONT_LEAK_ME" not in resp.text


# ── Reschedule (book-new-then-cancel-old) ─────────────────────────────────────


def test_reschedule_books_new_before_cancelling_old(stub, client):
    """The non-atomic flow must execute in the right order: if cancel is
    issued first and book then fails, the caller would lose both their
    old AND new slots. So we book first, then cancel.
    """
    stub.set("POST", "/appointments/search", StubResp(200, [_appt()]))
    stub.set("GET", "/clinicConfiguration/", StubResp(200,
        _clinic_config([{"id": 1, "name": "Hearing Test", "duration": 30}])))
    stub.set("GET", "/availability/", StubResp(200, _avail("2026-06-15", "14:00")))
    stub.set("POST", "/appointments/", StubResp(201, {}))
    stub.set("PUT", "/appointments/10995_0", StubResp(200, {}))

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/reschedule",
        json={
            "appointment_id": "10995_0",
            "new_start_date": "2026-06-15",
            "new_start_time": "14:00",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rescheduled"

    # Verify ordering: the POST /appointments/ (book) preceded the PUT
    # /appointments/10995_0 (cancel-old).
    book_index = next(
        i for i, c in enumerate(stub.calls)
        if c[0] == "POST" and c[1].endswith("/appointments/")
    )
    cancel_index = next(
        i for i, c in enumerate(stub.calls)
        if c[0] == "PUT" and "/appointments/10995_0" in c[1]
    )
    assert book_index < cancel_index


def test_reschedule_returns_partial_when_cancel_fails_after_book(stub, client):
    """If the new booking succeeds but the cancel of the old fails, the
    caller leaves with TWO appointments. Status must be 'partial' with a
    warning so the agent can surface it + the ticket can capture cleanup.
    """
    stub.set("POST", "/appointments/search", StubResp(200, [_appt()]))
    stub.set("GET", "/clinicConfiguration/", StubResp(200,
        _clinic_config([{"id": 1, "name": "Hearing Test", "duration": 30}])))
    stub.set("GET", "/availability/", StubResp(200, _avail("2026-06-15", "14:00")))
    stub.set("POST", "/appointments/", StubResp(201, {}))
    # Cancel of old fails
    stub.set("PUT", "/appointments/10995_0", StubResp(500, {}))

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/reschedule",
        json={
            "appointment_id": "10995_0",
            "new_start_date": "2026-06-15",
            "new_start_time": "14:00",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "partial"
    assert body["warning"], "partial reschedule must carry a warning"


def test_reschedule_aborts_cleanly_when_book_fails(stub, client):
    """If book-new fails, the old appointment must be untouched — never
    issue the PUT, otherwise the caller loses both slots on partial failure.
    """
    stub.set("POST", "/appointments/search", StubResp(200, [_appt()]))
    stub.set("GET", "/clinicConfiguration/", StubResp(200,
        _clinic_config([{"id": 1, "name": "Hearing Test", "duration": 30}])))
    stub.set("GET", "/availability/", StubResp(200, _avail("2026-06-15", "14:00")))
    # Book fails at the POST itself (location + provider resolve fine first)
    stub.set("POST", "/appointments/", StubResp(500, {}))
    puts: list[dict] = []
    stub.set("PUT", "/appointments/10995_0",
             lambda p: (puts.append(p), StubResp(200, {}))[1])

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/reschedule",
        json={
            "appointment_id": "10995_0",
            "new_start_date": "2026-06-15",
            "new_start_time": "14:00",
        },
    )
    assert resp.status_code >= 400
    assert puts == []  # old appointment untouched


# ── Location resolution + provider derivation ─────────────────────────────────


def test_book_honours_caller_provided_location_id(stub, client):
    """When the caller chose a location, book() uses it directly (no
    single-location fallback) and derives the provider open there."""
    posts: list[dict] = []
    stub.set("GET", "/clinicConfiguration/", StubResp(200, {
        "appointmentTypes": [{"id": 1, "name": "Hearing Test", "duration": 30}],
        "locations": [{"id": 1, "name": "North"}, {"id": 2, "name": "South"}],
    }))
    stub.set("GET", "/availability/",
             StubResp(200, _avail("2026-06-15", "10:00", provider_id=88, location_id=2)))
    stub.set("POST", "/appointments/", lambda p: (posts.append(p), StubResp(201, {}))[1])

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/book",
        json={
            "event_type_id": 1,
            "start_date": "2026-06-15",
            "start_time": "10:00",
            "location_id": 2,
            "patient_id": "316",
        },
    )
    assert resp.status_code == 200, resp.text
    assert posts[0]["locationId"] == 2
    assert posts[0]["providerId"] == 88


def test_book_rejects_multi_location_clinic_with_no_choice(stub, client):
    """A multi-location clinic with no caller-chosen location is a 400 —
    the adapter won't guess which location to book into."""
    stub.set("GET", "/clinicConfiguration/", StubResp(200, {
        "appointmentTypes": [{"id": 1, "name": "Hearing Test", "duration": 30}],
        "locations": [{"id": 1, "name": "North"}, {"id": 2, "name": "South"}],
    }))
    resp = client.post(
        "/blueprint/CLINIC_X/appointments/book",
        json={
            "event_type_id": 1,
            "start_date": "2026-06-15",
            "start_time": "10:00",
            "patient_id": "316",
        },
    )
    assert resp.status_code == 400
    assert "multiple locations" in resp.text


def test_book_409_when_slot_has_no_open_provider(stub, client):
    """If the chosen time has no available provider (slot just taken), book()
    refuses with 409 rather than posting an appointment with no provider."""
    taken = _avail("2026-06-15", "10:00")
    taken[0]["availabilityTimes"][0]["available"] = []  # nobody free
    stub.set("GET", "/clinicConfiguration/", StubResp(200,
        _clinic_config([{"id": 1, "name": "Hearing Test", "duration": 30}])))
    stub.set("GET", "/availability/", StubResp(200, taken))
    posts: list[dict] = []
    stub.set("POST", "/appointments/", lambda p: (posts.append(p), StubResp(201, {}))[1])

    resp = client.post(
        "/blueprint/CLINIC_X/appointments/book",
        json={
            "event_type_id": 1,
            "start_date": "2026-06-15",
            "start_time": "10:00",
            "patient_id": "316",
        },
    )
    assert resp.status_code == 409
    assert posts == []  # never posted a provider-less appointment


def test_find_available_slots_filters_out_booked_and_empty_slots(stub, client):
    """Blueprint's grid lists booked times too (available=[], appointmentId
    set). find_available_slots must surface only genuinely-open slots — an
    available provider AND no existing appointment — so the agent never
    offers a taken time."""
    stub.set("GET", "/availability/", StubResp(200, [
        {
            "date": "2026-06-15",
            "available": True,
            "availabilityTimes": [
                {"time": "09:00:00-0600",
                 "available": [{"providerId": 50, "locationId": 1}],
                 "appointmentId": None},                      # open → kept
                {"time": "10:00:00-0600",
                 "available": [], "appointmentId": None},      # nobody free → dropped
                {"time": "11:00:00-0600",
                 "available": [{"providerId": 50, "locationId": 1}],
                 "appointmentId": "555_0"},                    # booked → dropped
            ],
        },
        {
            "date": "2026-06-16",
            "available": False,                               # whole day off → dropped
            "availabilityTimes": [
                {"time": "09:00:00-0600",
                 "available": [{"providerId": 50, "locationId": 1}],
                 "appointmentId": None},
            ],
        },
    ]))

    resp = client.post(
        "/blueprint/CLINIC_X/availability/find",
        json={"event_type_id": 1, "start_date": "2026-06-15", "end_date": "2026-06-16"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["days"] == [
        {"date": "2026-06-15", "available_times": ["09:00"]},
    ]


def test_list_locations_endpoint_returns_locations_and_flag(stub, client):
    """The locations capability returns the bookable locations plus whether
    the agent should ask the caller to choose."""
    stub.set("GET", "/clinicConfiguration/", StubResp(200, {
        "locations": [
            {"id": 1, "name": "North", "formattedAddress": "1 North St"},
            {"id": 2, "name": "South"},
        ],
    }))
    resp = client.post("/blueprint/CLINIC_X/locations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["prompt_for_location"] is False  # from _FAKE_CONFIG
    assert {loc["id"] for loc in body["locations"]} == {1, 2}
    assert body["locations"][0]["address"] == "1 North St"
