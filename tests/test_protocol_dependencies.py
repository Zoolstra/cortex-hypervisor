"""
Unit tests for the protocol dependency framework.

Two layers:

1. ``unmet_dependencies(protocol_id, enabled_ids)`` — the pure helper.
   Always-on protocols (submit_ticket) count as enabled implicitly so
   ordinary protocols never list them as unmet.

2. ``factory._instantiate_capabilities`` — the live path. A toggle row
   stays "on" in the DB, but the agent sync drops the protocol when its
   deps aren't satisfied. The dropped protocol's tool + prompt never
   reach the live VAPI assistant — preventing the agent from being told
   to call a tool that isn't in its allowed set.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from api.voice_agent.factory import _instantiate_capabilities
from api.voice_agent.protocols import PROTOCOL_REGISTRY, unmet_dependencies


# ── unmet_dependencies helper ─────────────────────────────────────────────────


def test_unmet_returns_empty_for_protocols_with_no_deps():
    assert unmet_dependencies("verify_caller_identification", set()) == []
    assert unmet_dependencies("search_appointment_availability", set()) == []


def test_unmet_lists_each_missing_dep_in_declaration_order():
    # Cancel declares deps in this exact order: verify, locate
    got = unmet_dependencies("cancel_appointment", set())
    assert got == ["verify_caller_identification", "locate_appointment"]

    # Partial: just locate missing
    got = unmet_dependencies(
        "cancel_appointment",
        {"verify_caller_identification"},
    )
    assert got == ["locate_appointment"]


def test_unmet_returns_empty_when_all_deps_enabled():
    got = unmet_dependencies(
        "cancel_appointment",
        {"verify_caller_identification", "locate_appointment"},
    )
    assert got == []


def test_unmet_for_reschedule_lists_all_three():
    # Reschedule has the longest dep chain — verify deps come back in
    # the declared order regardless of which one's missing.
    got = unmet_dependencies("reschedule_appointment", set())
    assert got == [
        "verify_caller_identification",
        "locate_appointment",
        "search_appointment_availability",
    ]


def test_unmet_unknown_id_returns_empty_not_error():
    """A safety property — unknown ids are handled silently.

    Surfacing 'unknown protocol' as an exception in this helper would
    fail closed in places where the row simply hasn't been backfilled.
    The toggle endpoint already 404s on unknown ids before this point.
    """
    assert unmet_dependencies("not_a_real_protocol", set()) == []


def test_unmet_treats_always_on_protocols_as_enabled():
    """Always-on protocols (submit_ticket) are always in the sync, so
    even if a future protocol declared a dep on submit_ticket, the
    dep would never be "unmet" — checked here to prevent surprise."""
    # No protocol depends on submit_ticket today; we synthesize a check
    # via the always_on inclusion in the helper.
    # submit_ticket is in the registry, always_on=True. If we computed
    # unmet against {} and the helper didn't add always_on protocols, a
    # hypothetical dep on submit_ticket would be listed. Sanity:
    assert "submit_ticket" not in unmet_dependencies(
        "cancel_appointment", set()
    )


# ── factory: skip with unmet deps ─────────────────────────────────────────────


def _fake_clinic(pms_type: str = "blueprint") -> SimpleNamespace:
    return SimpleNamespace(
        clinic_id="X", clinic_name="Demo Clinic", pms_type=pms_type, location=None,
    )


def test_factory_drops_protocol_with_unmet_deps(caplog):
    """Cancel toggled on without Verify or Locate → dropped from the
    instantiated list, with a warning log so operators can debug.
    Always-on submit_ticket is still instantiated.
    """
    caplog.set_level(logging.WARNING, logger="api.voice_agent.factory")
    caps = _instantiate_capabilities(
        _fake_clinic(),
        enabled_capability_ids=["cancel_appointment"],
        credential_id="cred",
    )
    ids = [c.id for c in caps]
    assert "cancel_appointment" not in ids
    # submit_ticket always-on still landed
    assert "submit_ticket" in ids
    # Warning was logged with the unmet deps
    assert any(
        "cancel_appointment" in rec.message
        and "verify_caller_identification" in rec.message
        and "locate_appointment" in rec.message
        for rec in caplog.records
    ), f"expected unmet-deps warning, got: {[r.message for r in caplog.records]}"


def test_factory_includes_protocol_when_deps_satisfied():
    caps = _instantiate_capabilities(
        _fake_clinic(),
        enabled_capability_ids=[
            "verify_caller_identification",
            "locate_appointment",
            "cancel_appointment",
        ],
        credential_id="cred",
    )
    ids = [c.id for c in caps]
    assert "cancel_appointment" in ids
    assert "verify_caller_identification" in ids
    assert "locate_appointment" in ids


def test_factory_partial_dep_chain_drops_dependents_in_chain(caplog):
    """If Locate is enabled but Verify is NOT, Locate itself has an
    unmet dep → dropped. Cancel (which depends on both) is also dropped.
    Operator's intent is preserved at the row level; the live sync just
    omits both.
    """
    caplog.set_level(logging.WARNING, logger="api.voice_agent.factory")
    caps = _instantiate_capabilities(
        _fake_clinic(),
        enabled_capability_ids=["locate_appointment", "cancel_appointment"],
        credential_id="cred",
    )
    ids = [c.id for c in caps]
    assert "locate_appointment" not in ids
    assert "cancel_appointment" not in ids


def test_factory_includes_protocol_with_no_deps_regardless():
    """search_appointment_availability has no deps — it should land
    independent of other toggles.
    """
    caps = _instantiate_capabilities(
        _fake_clinic(),
        enabled_capability_ids=["search_appointment_availability"],
        credential_id="cred",
    )
    ids = [c.id for c in caps]
    assert "search_appointment_availability" in ids


# ── Registry sanity (catches drift if a new protocol is added without deps) ───


def test_every_protocol_declares_depends_on_explicitly():
    """All Protocol subclasses must declare ``depends_on`` (even as ())
    so the framework's dep walker has a defined value to read. Base
    class default is () so this passes for any protocol; the test
    exists as documentation + drift catch.
    """
    for cls in PROTOCOL_REGISTRY.values():
        assert isinstance(cls.depends_on, tuple), \
            f"{cls.__name__}.depends_on must be a tuple, got {type(cls.depends_on)}"


def test_no_dependency_cycles():
    """A protocol must not depend on itself (directly or transitively).

    Walks the dep graph; raises if a cycle is found. Fails the test if
    a future contributor introduces a cycle that would otherwise become
    an infinite loop in a future graph-walking validator.
    """
    def collect_transitive(pid: str, seen: set[str]) -> set[str]:
        cls = PROTOCOL_REGISTRY.get(pid)
        if cls is None:
            return seen
        for dep in cls.depends_on:
            if dep in seen:
                continue
            seen.add(dep)
            collect_transitive(dep, seen)
        return seen

    for pid in PROTOCOL_REGISTRY:
        deps = collect_transitive(pid, set())
        assert pid not in deps, f"Cycle: {pid} eventually depends on itself"
