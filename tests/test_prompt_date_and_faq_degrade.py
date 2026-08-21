"""Two call-time robustness invariants for the compiled voice-agent prompt.

**The date must resolve per call, not per sync.** The system prompt is compiled
once and pushed to VAPI, so any date computed in Python freezes at the moment of
that push. The live ACNA assistant spent ten days asserting it was 2026-08-10,
which silently skews every relative date a caller offers ("next Tuesday"). The
prompt therefore carries a LiquidJS template that VAPI renders on each call.

**FAQ retrieval must degrade to "no match", never raise.** It runs mid-call, so
an exception is a 500 to VAPI with a caller on the line. An empty result is a
state the contract already defines and the prompt already handles.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from google.api_core import exceptions as gapi_exceptions

from api.voice_agent import faq_retrieval, locale


# ── The locale prompt block ─────────────────────────────────────────────────


def _clinic(country: str = "CA", tz: str = "America/Edmonton"):
    """Minimal stand-in for a Clinic + its loaded location relationship."""
    return SimpleNamespace(
        clinic_id="TEST",
        country=country,
        location=SimpleNamespace(time_zone=tz),
    )


def test_prompt_date_is_a_call_time_template_not_a_literal():
    """The prompt must carry a Liquid template, not a rendered date.

    A literal date here is the regression: it would be correct on the day of
    the sync and wrong every day after.
    """
    block = locale.resolve(_clinic())["prompt_block"]

    assert '{{"now" | date:' in block, "date must be a LiquidJS template"
    # No bare ISO date anywhere in the block — that would mean a baked value.
    literals = [
        m for m in re.findall(r"\d{4}-\d{2}-\d{2}", block)
        if "%Y-%m-%d" not in block[max(0, block.find(m) - 12):block.find(m)]
    ]
    assert not literals, f"prompt block contains a baked date: {literals}"


def test_prompt_date_template_carries_the_clinic_timezone():
    """The timezone argument is what converts VAPI's UTC ``now`` to clinic-local.

    Without it, any call after 17:00 MST reads as tomorrow.
    """
    block = locale.resolve(_clinic(tz="America/Edmonton"))["prompt_block"]

    assert '"America/Edmonton"}}' in block
    assert '{{"now" | date: "%A, %Y-%m-%d", "America/Edmonton"}}' in block


def test_prompt_date_template_follows_a_clinic_in_another_timezone():
    """The template is built from the clinic's own timezone, not a constant."""
    block = locale.resolve(_clinic(country="US", tz="America/Chicago"))["prompt_block"]

    assert '"America/Chicago"}}' in block
    assert "Edmonton" not in block


def test_prompt_date_offers_weekday_and_iso_date():
    """The agent resolves "next Tuesday" against the weekday and passes the ISO
    date to the availability/booking tools, so both forms must be present."""
    block = locale.resolve(_clinic())["prompt_block"]

    assert "%A" in block, "weekday needed to resolve relative days"
    assert "%Y-%m-%d" in block, "ISO date is the form the booking tools take"


def test_today_local_still_returned_for_server_side_callers():
    """``today_local`` remains a real resolved date — it just must not be the
    thing the prompt uses."""
    resolved = locale.resolve(_clinic())

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolved["today_local"])
    assert resolved["today_local"] not in resolved["prompt_block"]


# ── FAQ retrieval degradation ──────────────────────────────────────────────


class _RaisingClient:
    def __init__(self, exc: Exception):
        self._exc = exc

    def query(self, *a, **kw):
        raise self._exc


@pytest.mark.parametrize("exc", [
    # The concrete motivating case: faq_embeddings not created yet.
    gapi_exceptions.NotFound("Table ClinicData.faq_embeddings not found"),
    # The remote embedding model's connection lacking Vertex AI permission.
    gapi_exceptions.Forbidden("caller lacks aiplatform.endpoints.predict"),
    gapi_exceptions.BadRequest("remote model not configured"),
])
def test_faq_search_degrades_to_no_match_on_api_error(monkeypatch, exc):
    """An infrastructure fault returns [] so the agent can hand off gracefully.

    Raising here would 500 to VAPI mid-call and lose the caller's turn.
    """
    monkeypatch.setattr(faq_retrieval, "bq_client", _RaisingClient(exc))

    assert faq_retrieval.search_faqs(clinic_id="ACNA", question="are you open?") == []


def test_faq_search_still_raises_on_programming_error(monkeypatch):
    """Only Google API errors degrade — a real bug must stay visible rather than
    masquerading as an empty corpus."""
    monkeypatch.setattr(
        faq_retrieval, "bq_client", _RaisingClient(TypeError("bad call")),
    )

    with pytest.raises(TypeError):
        faq_retrieval.search_faqs(clinic_id="ACNA", question="are you open?")
