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
        # FAQ retrieval sits between booking and the close: the corpus is
        # fetched mid-call rather than compiled into the prompt.
        "answer_clinic_question",
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


def test_referral_and_price_rules_present(cfg):
    # The 2026-08-10 outcomes introduce two new ways to make a false promise:
    # booking a caller who must reach a person, and inventing a price.
    sys = cfg["model"]["messages"][0]["content"]
    assert "NEVER book on a `REFER_TO_STAFF_*` outcome" in sys
    assert "NEVER book before an `earliest_bookable_date`" in sys
    assert "NEVER state a price the decision tool didn't give you" in sys


def test_identity_check_is_prefaced_before_the_spell_back(cfg):
    # The framing must land BEFORE the verify fragment's letter-by-letter
    # instructions, or the agent spells names back cold.
    sys = cfg["model"]["messages"][0]["content"]
    i_preface = sys.find("We really need to get your identity right")
    i_spellback = sys.find("Confirm the spelling of BOTH names")
    assert -1 < i_preface < i_spellback


def test_time_preference_precedes_offering_slots(cfg):
    sys = cfg["model"]["messages"][0]["content"]
    assert "Ask for a time preference BEFORE you offer anything" in sys
    assert "Do mornings or afternoons work better for you?" in sys
    # The count instruction must sit after the preference ask.
    i_pref = sys.find("Ask for a time preference BEFORE you offer anything")
    i_count = sys.find("How many times to offer")
    assert -1 < i_pref < i_count


def test_slot_counts_are_two_with_a_preference_and_three_without(cfg):
    """Clinic call 2026-07-27: "even if they don't give a preference, give
    three. But if they do give a preference, give two." Both halves matter —
    an earlier revision only specified the preference case."""
    sys = cfg["model"]["messages"][0]["content"]
    assert "offer **TWO** slots that match it" in sys
    assert "offer **THREE**, nearest first" in sys
    assert "Never read out more than three" in sys


def test_hard_floor_and_soft_window_are_distinguished(cfg):
    # Collapsing these would either let the agent breach the clean-and-check
    # gap, or make the warranty preference sound like a refusal to book.
    sys = cfg["model"]["messages"][0]["content"]
    assert "never start before `earliest_bookable_date`" in sys
    assert "`earliest_bookable_date` — HARD" in sys
    assert "`preferred_window` — SOFT" in sys
    assert "This is a preference, never a refusal" in sys
    assert "Never tell the caller you can't see them until the window" in sys


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


def test_faq_corpus_is_not_in_the_prompt(cfg):
    """The whole point of the retrieval tool: instructions in, content out.

    The prompt may say HOW to look a question up; it must not contain answer
    text. If a future change goes back to compiling FAQs into the prompt, the
    booking spine starts getting diluted again — the exact regression roles.py
    was written to stop.
    """
    sys = cfg["model"]["messages"][0]["content"]
    assert "answer_clinic_question" in sys          # the instruction is present
    # Retrieval-shaped language, not a Q&A dump.
    assert "Never improvise an answer" in sys
    assert "## Answering General Questions" in sys


def test_faq_fragment_sits_after_the_booking_spine(cfg):
    # Ordering is the salience guard: booking flow first, FAQ as a supporting
    # capability, hard rules last.
    sys = cfg["model"]["messages"][0]["content"]
    i_book = sys.find("### Step 4 — Find a time and book it")
    i_faq = sys.find("## Answering General Questions")
    i_rules = sys.find("## Non-negotiable tool rules")
    assert -1 < i_book < i_faq < i_rules


def test_faq_tool_is_scoped_to_general_information(cfg):
    tool = next(t for t in cfg["model"]["tools"]
                if t["name"] == "answer_clinic_question")
    desc = tool["description"]
    # Must not become a back door to patient data — that's the PHI tools' job,
    # and this endpoint is not PHI-audit-logged.
    assert "never for anything about a specific patient" in desc.lower()
    assert set(tool["body"]["required"]) == {"question"}


def test_faq_miss_routes_to_a_handoff_not_a_guess(cfg):
    sys = cfg["model"]["messages"][0]["content"]
    assert "NEVER answer a general clinic question from your own knowledge" in sys
