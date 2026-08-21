"""ACNA placeholder-grid availability + the technician clean-and-check pair.

Covers the invariants that make grid-derived booking safe for ACNA, and pins
the two behaviours that are easy to regress silently:

  * **Multi-lane grids.** The clean-and-check grid seeds TWO concurrent
    placeholders per (technician, slot) — "C&C RA 1"/"C&C RA 2" — where the
    Annual grid seeds exactly one per (clinician, slot). A slot must be offered
    ONCE per free provider regardless of how many lanes back it, and a booked
    patient appointment for that provider closes the slot. That conservative
    rule matches how the clinic actually books: across 8 months the technician
    has 714 single-booked slots against 16 double-booked ones.
  * **Durations come from the FULL type pool.** ACNA's real 'Service' type
    (204) is not flagged for online booking, so reading the online-bookable
    subset reports its duration as null — for exactly the type the agent books.

Config-shape tests assert the seeded pairing is the evidence-backed one, and
that a CLINICIAN service visit stays unbookable (no placeholder grid exists for
it, so it must fall through to a message rather than book a technician).
"""
from __future__ import annotations

import pytest

from typing import get_args

from api.voice_agent.appointment_decision import Outcome
from api.voice_agent.protocols.acna_appointment_decision import (
    AppointmentDecisionConfig,
)
from api.voice_agent.protocols.acna_placeholder import ACNAAvailabilityConfig
from api.voice_agent.pms.blueprint import BlueprintAdapter

from tests.test_blueprint_appointments import (  # noqa: F401 — fixtures
    HttpxStub,
    StubResp,
    _FAKE_CONFIG,
    client,
    stub,
)


ANNUAL_PLACEHOLDER, ANNUAL_REAL = 200, 207
CC_PLACEHOLDER, CC_REAL = 8, 204

TECHNICIAN = "Lourenco Rubinick, Michelli"
CLINICIAN = "Roy, Natalie"


def _row(
    *,
    appointment_id: str,
    event_type_id: int,
    provider: str,
    start: str,
    end: str,
    patient_id: int | None = None,
    status: int = 2,
    summary: str = "",
    provider_id: int = 911,
) -> dict:
    """One Blueprint Search Appointments row.

    ``patient_id=None`` is a placeholder (capacity); a patient id makes it a
    real booking that consumes capacity. Times are UTC, as Blueprint returns
    them — the adapter converts to clinic-local.
    """
    return {
        "appointment_id": appointment_id,
        "status": status,
        "patient_name": None if patient_id is None else "Solo, Han",
        "patient_id": patient_id,
        "birthdate": None,
        "start_time": start,
        "end_time": end,
        "provider": provider,
        "provider_id": provider_id,
        "location": "ACNA",
        "location_id": 1,
        "busy": True,
        "onlineBookingSecret": None,
        "eventTypeId": event_type_id,
        "contactVerified": False,
        "summary": summary,
        "notes": "",
    }


def _adapter() -> BlueprintAdapter:
    """Adapter with config injected directly — no Cloud SQL, no Secret Manager.

    Pinned to ACNA's real timezone: the grid returns UTC and the adapter quotes
    clinic-local times, so the offset is part of what these tests check.
    """
    return BlueprintAdapter(
        clinic_id="ACNA",
        http_config={**_FAKE_CONFIG, "timezone": "America/Edmonton"},
    )


def _find(adapter, **kw):
    defaults = dict(
        placeholder_event_type_id=CC_PLACEHOLDER,
        real_event_type_id=CC_REAL,
        start_date="2026-08-24",
        end_date="2026-08-24",
    )
    return adapter.find_placeholder_availability(**{**defaults, **kw})


@pytest.fixture
def seeded_pairs(monkeypatch):
    """Serve the seeded type_pairs without touching Cloud SQL.

    The endpoint resolves pairs through ``load_protocol_config``; the tests
    assert against the shipped defaults, so hand back the config model itself.
    """
    monkeypatch.setattr(
        "api.voice_agent.protocols.load_protocol_config",
        lambda db, clinic_id, protocol_id: ACNAAvailabilityConfig(),
    )
    return ACNAAvailabilityConfig()


def _slot_times(result) -> dict[str, list[str]]:
    """{date: [times]} — flattens the availability result for assertions."""
    return {d.date: [s.time for s in d.slots] for d in result.days}


# ── Multi-lane capacity (the clean-and-check grid) ───────────────────────────


def test_two_lanes_same_provider_offered_once(stub):
    """Two concurrent C&C placeholders for one technician are ONE offer.

    The grid seeds "C&C RA 1" and "C&C RA 2" as separate rows with the same
    provider and start time. Without deduping, the agent would read two
    identical openings and could quote the same slot twice.
    """
    stub.set("POST", "/appointments/search", StubResp(200, [
        _row(appointment_id="1_0", event_type_id=CC_PLACEHOLDER,
             provider=TECHNICIAN, summary="C&C RA 1",
             start="2026-08-24 15:15:00 +0000", end="2026-08-24 15:45:00 +0000"),
        _row(appointment_id="2_0", event_type_id=CC_PLACEHOLDER,
             provider=TECHNICIAN, summary="C&C RA 2",
             start="2026-08-24 15:15:00 +0000", end="2026-08-24 15:45:00 +0000"),
    ]))

    result = _find(_adapter())

    assert len(result.days) == 1
    slots = result.days[0].slots
    assert len(slots) == 1, "two lanes must collapse to a single offered slot"
    assert slots[0].providers == [TECHNICIAN]


def test_booked_appointment_closes_the_slot(stub):
    """A patient-linked appointment for that provider/time closes the slot.

    Conservative by design: the clinic seeds two lanes but books the technician
    singly 714 times against 16 doubles, so one booking is treated as taking
    the slot rather than leaving a lane on offer.
    """
    stub.set("POST", "/appointments/search", StubResp(200, [
        _row(appointment_id="1_0", event_type_id=CC_PLACEHOLDER,
             provider=TECHNICIAN, summary="C&C RA 1",
             start="2026-08-24 15:15:00 +0000", end="2026-08-24 15:45:00 +0000"),
        _row(appointment_id="2_0", event_type_id=CC_PLACEHOLDER,
             provider=TECHNICIAN, summary="C&C RA 2",
             start="2026-08-24 15:15:00 +0000", end="2026-08-24 15:45:00 +0000"),
        # A real Service booked into one of the lanes.
        _row(appointment_id="3_0", event_type_id=CC_REAL, patient_id=316,
             provider=TECHNICIAN,
             start="2026-08-24 15:15:00 +0000", end="2026-08-24 15:45:00 +0000"),
    ]))

    assert _find(_adapter()).days == []


def test_cancelled_booking_reopens_the_slot(stub):
    """A cancelled real appointment (status 3) must not consume capacity.

    Blueprint keeps cancelled rows in the search results, so a genuinely
    re-opened slot would stay invisible if status were ignored.
    """
    stub.set("POST", "/appointments/search", StubResp(200, [
        _row(appointment_id="1_0", event_type_id=CC_PLACEHOLDER,
             provider=TECHNICIAN, summary="C&C RA 1",
             start="2026-08-24 15:15:00 +0000", end="2026-08-24 15:45:00 +0000"),
        _row(appointment_id="3_0", event_type_id=CC_REAL, patient_id=316,
             provider=TECHNICIAN, status=3,   # cancelled
             start="2026-08-24 15:15:00 +0000", end="2026-08-24 15:45:00 +0000"),
    ]))

    assert _slot_times(_find(_adapter())) == {"2026-08-24": ["09:15"]}


def test_cancelled_placeholder_is_not_capacity(stub):
    """A cancelled placeholder row is not a bookable lane."""
    stub.set("POST", "/appointments/search", StubResp(200, [
        _row(appointment_id="1_0", event_type_id=CC_PLACEHOLDER,
             provider=TECHNICIAN, status=3, summary="C&C RA 1",
             start="2026-08-24 15:15:00 +0000", end="2026-08-24 15:45:00 +0000"),
    ]))

    assert _find(_adapter()).days == []


def test_other_grids_do_not_leak_into_this_one(stub):
    """Only the requested placeholder type is capacity.

    ACNA's grid carries many Z-prefixed internal types (lunch, meetings,
    vacation). Searching the C&C grid must not offer an Annual space, and vice
    versa — the two are different durations and different provider populations.
    """
    rows = [
        _row(appointment_id="1_0", event_type_id=CC_PLACEHOLDER,
             provider=TECHNICIAN, summary="C&C RA 1",
             start="2026-08-24 15:15:00 +0000", end="2026-08-24 15:45:00 +0000"),
        _row(appointment_id="2_0", event_type_id=ANNUAL_PLACEHOLDER,
             provider=CLINICIAN,
             start="2026-08-24 17:00:00 +0000", end="2026-08-24 18:00:00 +0000"),
    ]
    stub.set("POST", "/appointments/search", StubResp(200, rows))

    cc = _find(_adapter())
    assert _slot_times(cc) == {"2026-08-24": ["09:15"]}
    assert cc.days[0].slots[0].providers == [TECHNICIAN]

    annual = _find(
        _adapter(),
        placeholder_event_type_id=ANNUAL_PLACEHOLDER,
        real_event_type_id=ANNUAL_REAL,
    )
    assert _slot_times(annual) == {"2026-08-24": ["11:00"]}
    assert annual.days[0].slots[0].providers == [CLINICIAN]


def test_excluded_pseudo_provider_is_not_offered(stub):
    """'Clinician, Z NEW' has no availability schedule — booking it fails, so
    its placeholder spaces must never be offered."""
    stub.set("POST", "/appointments/search", StubResp(200, [
        _row(appointment_id="1_0", event_type_id=ANNUAL_PLACEHOLDER,
             provider="Clinician, Z NEW",
             start="2026-08-24 17:00:00 +0000", end="2026-08-24 18:00:00 +0000"),
        _row(appointment_id="2_0", event_type_id=ANNUAL_PLACEHOLDER,
             provider=CLINICIAN,
             start="2026-08-24 17:00:00 +0000", end="2026-08-24 18:00:00 +0000"),
    ]))

    result = _find(
        _adapter(),
        placeholder_event_type_id=ANNUAL_PLACEHOLDER,
        real_event_type_id=ANNUAL_REAL,
        excluded_providers=["Clinician, Z NEW"],
    )

    assert result.days[0].slots[0].providers == [CLINICIAN]


# ── Durations must come from the full type pool ──────────────────────────────


def test_appointment_types_reports_duration_for_non_online_type(
    stub, client, seeded_pairs,
):
    """'Service' (204) is not online-booking-enabled but IS what the agent
    books. Its duration must still be reported.

    Blueprint returns name=null for types not flagged for online booking, and
    ``list_appointment_types`` drops those rows — so reading that subset gives
    a null duration for precisely the bookable type. The listing reads the full
    pool instead.
    """
    stub.set("GET", "/clinicConfiguration/", StubResp(200, {
        "appointmentTypes": [
            {"id": ANNUAL_REAL, "name": "Annual", "duration": 60},
            {"id": CC_REAL, "name": None, "duration": 30},   # not online-bookable
        ],
        "locations": [{"id": 1, "name": "ACNA"}],
    }))

    resp = client.post("/blueprint/ACNA/placeholder/appointment-types")
    assert resp.status_code == 200, resp.text

    by_id = {t["real_event_type_id"]: t for t in resp.json()["appointment_types"]}
    assert by_id[CC_REAL]["duration_minutes"] == 30, \
        "non-online real type must still report its duration"
    assert by_id[CC_REAL]["name"] == "Hearing Aid Clean and Check", \
        "name is the config display name, not Blueprint's null"
    assert by_id[ANNUAL_REAL]["duration_minutes"] == 60


def test_raw_event_types_keeps_null_named_types(stub):
    """The full pool keeps placeholder/non-online rows that
    list_appointment_types drops."""
    stub.set("GET", "/clinicConfiguration/", StubResp(200, {
        "appointmentTypes": [
            {"id": ANNUAL_REAL, "name": "Annual", "duration": 60},
            {"id": CC_REAL, "name": None, "duration": 30},
        ],
    }))
    adapter = _adapter()

    assert {t["id"] for t in adapter.raw_event_types()} == {ANNUAL_REAL, CC_REAL}
    assert {t.id for t in adapter.list_appointment_types()} == {ANNUAL_REAL}


# ── Seeded config shape ─────────────────────────────────────────────────────


def test_clean_check_pair_is_seeded():
    """The C&C pair maps placeholder 8 → real 204, derived from booked history."""
    pairs = {p.real_event_type_id: p for p in ACNAAvailabilityConfig().type_pairs}

    assert pairs[CC_REAL].placeholder_event_type_id == CC_PLACEHOLDER
    assert pairs[ANNUAL_REAL].placeholder_event_type_id == ANNUAL_PLACEHOLDER


def test_each_real_type_maps_to_exactly_one_grid():
    """``_resolve_pair`` keys on real_event_type_id, so a duplicate entry would
    make the grid ambiguous rather than additive."""
    ids = [p.real_event_type_id for p in ACNAAvailabilityConfig().type_pairs]

    assert len(ids) == len(set(ids))


def test_technician_clean_check_is_bookable_clinician_service_is_not():
    """The technician C&C books type 204; a CLINICIAN service visit must stay
    unmapped.

    Both would want the 'Service' type, but 204's only grid is the technician's
    — so mapping the clinician outcome too would book a clinician visit with a
    technician. It falls through to a message instead.
    """
    outcomes = AppointmentDecisionConfig().outcome_booking

    assert outcomes["BOOK_TECHNICIAN_CLEAN_AND_CHECK"] == CC_REAL
    assert outcomes["BOOK_CLINICIAN_SERVICE_VISIT"] is None
    assert outcomes["BOOK_NEW_PATIENT_INTAKE_WITH_CLINICIAN"] is None


def test_every_bookable_outcome_has_a_placeholder_pair():
    """Any outcome the engine marks bookable must resolve to a configured pair.

    This is the invariant that breaks first when someone maps a new outcome:
    the agent would relay a real_event_type_id that ``_resolve_pair`` then
    rejects with a 422 mid-call.
    """
    pairs = {p.real_event_type_id for p in ACNAAvailabilityConfig().type_pairs}
    mapped = {
        outcome: etid
        for outcome, etid in AppointmentDecisionConfig().outcome_booking.items()
        if etid is not None
    }

    assert mapped, "expected at least the annual + clean-and-check outcomes"
    unpaired = {o: e for o, e in mapped.items() if e not in pairs}
    assert not unpaired, f"outcomes map to types with no placeholder pair: {unpaired}"


def test_outcome_booking_covers_every_canonical_outcome():
    """Every outcome the engine can emit must appear in ``outcome_booking``.

    A missing key is indistinguishable from an intentional None at lookup time
    (``cfg.outcome_booking.get(outcome)``), so an outcome added to the engine
    without a mapping silently becomes unbookable. That is exactly how the
    self-pay annual ended up unbookable in production: the stored dict REPLACES
    this default wholesale, so a key added here later never reached the live
    row. Failing here is the cheap warning; the expensive one is an agent that
    quotes a price and then can't book.
    """
    canonical = set(get_args(Outcome))
    mapped = set(AppointmentDecisionConfig().outcome_booking)

    assert canonical - mapped == set(), "outcomes with no booking mapping"
    assert mapped - canonical == set(), "mappings for outcomes the engine never emits"


def test_self_pay_annual_books_the_annual_type():
    """Self-pay is the same appointment as a funded annual — only the payment
    conversation differs — so it must book type 207, not fall to a message."""
    outcomes = AppointmentDecisionConfig().outcome_booking

    assert outcomes["OFFER_SELF_PAY_ANNUAL_HEARING_TEST"] == ANNUAL_REAL
    assert outcomes["OFFER_SELF_PAY_ANNUAL_HEARING_TEST"] == \
        outcomes["BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"]


def test_referral_outcomes_are_never_bookable():
    """Referral outcomes are non-bookable by definition — the agent must hand
    off, never offer times."""
    outcomes = AppointmentDecisionConfig().outcome_booking

    for outcome in (o for o in get_args(Outcome) if o.startswith("REFER_TO_STAFF")):
        assert outcomes[outcome] is None, f"{outcome} must not be bookable"


# ── Out-of-province workers' comp must reach a person ───────────────────────


@pytest.mark.parametrize("insurer", [
    "WorkSafe BC",
    "WSIB",
    "WCB Manitoba-Account#001226-001262",
    "WCB-SK",
    "WCB NS",
    "WCB (NB)",
    "WCB-NWT",
    "Workers Safety & Compensation Commission",
])
def test_out_of_province_workers_comp_goes_to_staff(insurer):
    """Only WCB *Alberta* funds an annual here.

    The clinic was explicit: "Only WCB Alberta is annual... work safe BC and
    WSIB and Worksafe other provinces, I would just put all of those onto like
    a real person" (2026-07-27). These previously funded by two different
    routes — the "WCB *" spellings matched the bare `wcb` pattern and inherited
    Alberta's annual eligibility, while WorkSafe BC / WSIB / the commission
    matched nothing and fell through to `Other`, which funds. ~60 active
    patients across these spellings.
    """
    from api.voice_agent.appointment_decision import (
        ACTION_HUMAN_REVIEW,
        DEFAULT_PAYER_ACTIONS,
        PAYER_WCB_OUT_OF_PROVINCE,
        classify_insurer,
    )

    bucket = classify_insurer(insurer)
    assert bucket == PAYER_WCB_OUT_OF_PROVINCE, f"{insurer!r} → {bucket!r}"
    assert DEFAULT_PAYER_ACTIONS[bucket] == ACTION_HUMAN_REVIEW


def test_wcb_alberta_still_funds():
    """The Alberta board is the one that pays — this must not regress."""
    from api.voice_agent.appointment_decision import (
        ACTION_FUND, DEFAULT_PAYER_ACTIONS, PAYER_WCB, classify_insurer,
    )

    assert classify_insurer("WCB Alberta") == PAYER_WCB
    assert DEFAULT_PAYER_ACTIONS[PAYER_WCB] == ACTION_FUND


def test_non_workers_comp_insurers_unaffected():
    """The workers'-comp branch must not swallow other programs.

    'Alberta Aids to Daily Living (CS)' is the clinic's largest insurer by
    volume and contains 'Alberta' — it must still classify as AADL, not get
    pulled into the provincial workers'-comp branch.
    """
    from api.voice_agent.appointment_decision import (
        PAYER_AADL, PAYER_BLUE_CROSS, PAYER_NIHB, PAYER_VA, classify_insurer,
    )

    assert classify_insurer("Alberta Aids to Daily Living (CS)") == PAYER_AADL
    assert classify_insurer("Blue Cross") == PAYER_BLUE_CROSS
    assert classify_insurer("First Canadian Health (NIHB)") == PAYER_NIHB
    assert classify_insurer("Bigstone") == PAYER_NIHB
    assert classify_insurer("VAC") == PAYER_VA
    assert classify_insurer("Veterans Affairs Canada") == PAYER_VA


def test_warranty_trigger_is_five_months_not_six():
    """FIVE months, per the clinic's refined rule.

    She first said "coming up within the next six months", then clarified: "if
    we're six months away from the warranty expiring, we're safe... if it is
    less than five months, move it". The looser phrasing reads as six and has
    already prompted one attempt to change this.
    """
    from api.voice_agent.appointment_decision import (
        DEFAULT_WARRANTY_BUNDLE_TRIGGER_MONTHS,
        DEFAULT_WARRANTY_BUNDLE_WINDOW_MONTHS,
    )

    assert DEFAULT_WARRANTY_BUNDLE_TRIGGER_MONTHS == 5
    assert DEFAULT_WARRANTY_BUNDLE_WINDOW_MONTHS == 2
