"""Structural regression tests for the annual-booking role compiler.

These pin the properties that fix the "agent uses the wrong tools" failure:
exact tool set (no list_appointment_types), booking flow as the prompt spine,
take-a-message explicitly gated and AFTER the booking steps, closing block last,
and the imperative tool rules present. If a refactor breaks any of these, the
live agent will regress to message-taking — fail loudly here instead.
"""
from __future__ import annotations

import types

import pytest

from api.voice_agent import roles
from api.voice_agent.roles import build_annual_booking_config


ACNA = "0b5f0929-31fb-4e21-9dd4-030bd040335d"


class _FakeDB:
    """Protocol configs load via load_protocol_config, patched below."""

    def get(self, model, key):
        return None  # persona row absent → defaults ("Emma", gpt-4o)


@pytest.fixture
def cfg(monkeypatch):
    location = types.SimpleNamespace(
        time_zone="America/Edmonton",
        hours_monday="8-4", hours_tuesday="8-4", hours_wednesday="8-4",
        hours_thursday="8-4", hours_friday="8-4",
        hours_saturday=None, hours_sunday=None,
    )
    clinic = types.SimpleNamespace(
        clinic_id=ACNA,
        clinic_name="Audiology Clinic of Northern Alberta",
        pms_type="blueprint",
        location=location,
        country="CA",
        voice_agent=types.SimpleNamespace(agent_role="annual_booking"),
    )
    # No Cloud SQL: protocol configs fall back to model defaults.
    monkeypatch.setattr(
        roles, "load_protocol_config",
        lambda db, cid, pid: roles.PROTOCOL_REGISTRY[pid].config_model(),
    )
    # No Secret Manager.
    monkeypatch.setattr(
        "api.voice_agent.factory._vapi_credential_id", lambda: "cred_test",
    )
    return build_annual_booking_config(_FakeDB(), clinic)


def test_exact_tool_set(cfg):
    names = [t["name"] for t in cfg["model"]["tools"]]
    assert names == [
        "verify_caller_identification",
        "determine_appointment_type",
        "find_available_slots",
        "book_appointment",
        "submit_ticket",
    ]


def test_list_appointment_types_excluded(cfg):
    assert all(t["name"] != "list_appointment_types" for t in cfg["model"]["tools"])


def test_prompt_spine_ordering(cfg):
    sys = cfg["model"]["messages"][0]["content"]
    i_verify = sys.find("Step 2 — Identify the patient")
    i_decide = sys.find("Step 3 — Determine the right appointment")
    i_book = sys.find("Step 4 — Find a time and book it")
    i_msg = sys.find("Step 5 — Take a message (ONLY when sent here)")
    i_close = sys.find("Step 6 — Close (every call)")
    assert -1 < i_verify < i_decide < i_book < i_msg < i_close


def test_close_is_booking_aware_not_generic(cfg):
    sys = cfg["model"]["messages"][0]["content"]
    # The generic ticket fragment's "you cannot confirm a specific appointment
    # time" contradicts a successful booking — must not be inlined verbatim.
    assert "you cannot confirm a specific appointment time" not in sys
    assert "If you BOOKED on this call" in sys
    assert "Confirm tentative booking" in sys


def test_no_dangling_stage_or_tool_references(cfg):
    sys = cfg["model"]["messages"][0]["content"]
    for absent in ("Stage 1", "Stage 3", "cancel_appointment", "reschedule_appointment"):
        assert absent not in sys, f"dangling generalist reference: {absent!r}"
    # And the tool schemas must not point at the excluded list tool as the
    # only source of the event type id.
    for t in cfg["model"]["tools"]:
        desc = str(t)
        assert "from list_appointment_types. Required" not in desc


def test_message_path_is_gated_not_primary(cfg):
    sys = cfg["model"]["messages"][0]["content"]
    assert "Never present this step as the first option" in sys
    assert "do not fall back to taking a message unless a step below explicitly sends you there" in sys


def test_imperative_tool_rules_present(cfg):
    sys = cfg["model"]["messages"][0]["content"]
    assert "NEVER tell the caller an appointment is booked" in sys
    assert "NEVER quote an available time you did not receive from `find_available_slots`" in sys
    assert "NEVER decide eligibility or the appointment type yourself" in sys


def test_no_generalist_machinery(cfg):
    sys = cfg["model"]["messages"][0]["content"]
    for absent in ("Price Shopper", "Stage 3a", "Stage 3b", "caller buckets",
                   "New Patient Discovery"):
        assert absent not in sys, f"generalist fragment leaked into role prompt: {absent!r}"


def test_payload_shape_matches_general_factory(cfg):
    # Same envelope the sync endpoints/resync script expect.
    assert set(cfg) == {
        "name", "first_message", "first_message_interruptions_enabled",
        "model", "voice", "transcriber",
    }
    assert cfg["model"]["provider"] == "openai"
    assert cfg["model"]["messages"][0]["role"] == "system"
