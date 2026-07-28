"""Exhaustive tests for the appointment-type decision engine + payer resolution.

The engine is the clinic's confirmed declarative spec (see
api/voice_agent/appointment_decision.py). These tests pin the decision table so
any future rule change is a deliberate edit here, not an accident.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from api.voice_agent.appointment_decision import (
    DecisionInputs,
    DecisionRules,
    classify_insurer,
    decide,
    resolve_payer,
)

TODAY = date(2026, 7, 22)


def _inputs(**overrides) -> DecisionInputs:
    base = dict(
        today=TODAY,
        patient_is_existing=True,
        care_plan_name="Complete Care Plan",
        care_plan_active=True,
        payer_type="Other",
        last_hearing_test_date=TODAY - timedelta(days=800),   # ~2.2y → due
        last_clinician_visit_date=TODAY - timedelta(days=100),  # recent
    )
    base.update(overrides)
    return DecisionInputs(**base)


def years_ago(y: float) -> date:
    return TODAY - timedelta(days=int(y * 365.25))


# ── Decision table ─────────────────────────────────────────────────────────────


def test_new_patient_wins_over_everything():
    d = decide(_inputs(patient_is_existing=False))
    assert d.outcome == "BOOK_NEW_PATIENT_INTAKE_WITH_CLINICIAN"


@pytest.mark.parametrize("plan", ["Complete Care Plan", "CCP LACE", "Care Plan, No Batts"])
def test_active_qualifying_plan_due_test_books_annual(plan):
    d = decide(_inputs(care_plan_name=plan, last_hearing_test_date=years_ago(1.5)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_never_tested_is_infinitely_old_and_due():
    d = decide(_inputs(last_hearing_test_date=None))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_not_due_recent_clinician_gets_technician_cc():
    d = decide(_inputs(
        last_hearing_test_date=years_ago(0.5),
        last_clinician_visit_date=years_ago(0.4),
    ))
    assert d.outcome == "BOOK_TECHNICIAN_CLEAN_AND_CHECK"


def test_not_due_stale_clinician_gets_clinician_service():
    d = decide(_inputs(
        last_hearing_test_date=years_ago(0.5),
        last_clinician_visit_date=years_ago(1.5),
    ))
    assert d.outcome == "BOOK_CLINICIAN_SERVICE_VISIT"


def test_no_clinician_visit_on_file_counts_as_stale():
    d = decide(_inputs(
        last_hearing_test_date=years_ago(0.5),
        last_clinician_visit_date=None,
    ))
    assert d.outcome == "BOOK_CLINICIAN_SERVICE_VISIT"


# ── Care-plan gate ────────────────────────────────────────────────────────────


def test_pre_plan_never_allows_annual():
    d = decide(_inputs(
        care_plan_name="Pre-Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(3),
        last_clinician_visit_date=years_ago(2),
    ))
    assert d.outcome == "BOOK_CLINICIAN_SERVICE_VISIT"


def test_expired_qualifying_plan_behaves_like_pre_plan():
    d = decide(_inputs(
        care_plan_name="Complete Care Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(3),
        last_clinician_visit_date=years_ago(0.2),
    ))
    assert d.outcome == "BOOK_TECHNICIAN_CLEAN_AND_CHECK"


def test_no_plan_defaults_to_allowed():
    d = decide(_inputs(care_plan_name=None, care_plan_active=False,
                       last_hearing_test_date=years_ago(2)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_unknown_plan_default_can_be_tightened():
    rules = DecisionRules(unknown_plan_allows_annual=False)
    d = decide(_inputs(care_plan_name="Mystery Plan", care_plan_active=True,
                       last_hearing_test_date=years_ago(2),
                       last_clinician_visit_date=years_ago(2)), rules)
    assert d.outcome == "BOOK_CLINICIAN_SERVICE_VISIT"


def test_plan_name_matching_tolerates_whitespace_and_case():
    # Feed variance must not fail OPEN past the deny rules: 'Pre-Plan ' with a
    # trailing space is still Pre-Plan (never funded).
    d = decide(_inputs(
        care_plan_name="Pre-Plan ", care_plan_active=False,
        last_hearing_test_date=years_ago(3),
        last_clinician_visit_date=years_ago(0.2),
    ))
    assert d.outcome == "BOOK_TECHNICIAN_CLEAN_AND_CHECK"
    # And case variance still matches a qualifying plan.
    d2 = decide(_inputs(care_plan_name="ccp lace", care_plan_active=True,
                        last_hearing_test_date=years_ago(2)))
    assert d2.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_whitespace_only_plan_name_treated_as_no_plan():
    d = decide(_inputs(care_plan_name="   ", care_plan_active=False,
                       last_hearing_test_date=years_ago(2)))
    # unknown/no plan → default allowed → due → annual
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


# ── Payer intervals ───────────────────────────────────────────────────────────


def test_wcb_due_after_one_year():
    d = decide(_inputs(payer_type="WCB", last_hearing_test_date=years_ago(1.1)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_va_not_due_at_18_months():
    d = decide(_inputs(payer_type="Veterans Affairs",
                       last_hearing_test_date=years_ago(1.5)))
    assert d.outcome == "BOOK_TECHNICIAN_CLEAN_AND_CHECK"


def test_va_due_after_two_years():
    d = decide(_inputs(payer_type="Veterans Affairs",
                       last_hearing_test_date=years_ago(2.1)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_exact_anniversary_is_due():
    # Calendar anniversary counts as due — a caller on the exact date their
    # payer will fund a new test must not be denied by a 365/365.25 fraction.
    d = decide(_inputs(payer_type="WCB",
                       last_hearing_test_date=TODAY.replace(year=TODAY.year - 1)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_day_before_anniversary_is_not_due():
    d = decide(_inputs(
        payer_type="WCB",
        last_hearing_test_date=TODAY.replace(year=TODAY.year - 1) + timedelta(days=1),
        last_clinician_visit_date=TODAY - timedelta(days=30),
    ))
    assert d.outcome == "BOOK_TECHNICIAN_CLEAN_AND_CHECK"


def test_va_exact_two_year_anniversary_is_due():
    d = decide(_inputs(payer_type="Veterans Affairs",
                       last_hearing_test_date=TODAY.replace(year=TODAY.year - 2)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_clinician_exact_anniversary_counts_as_stale():
    # Same anniversary semantics for the clinician-visit threshold.
    d = decide(_inputs(
        last_hearing_test_date=years_ago(0.5),
        last_clinician_visit_date=TODAY.replace(year=TODAY.year - 1),
    ))
    assert d.outcome == "BOOK_CLINICIAN_SERVICE_VISIT"


# ── Payer resolution ──────────────────────────────────────────────────────────


def test_classify_insurer_buckets():
    assert classify_insurer("WCB Alberta") == "WCB"
    assert classify_insurer("VAC") == "Veterans Affairs"
    assert classify_insurer("Veterans Affairs Canada") == "Veterans Affairs"
    assert classify_insurer("Blue Cross") == "Other"
    assert classify_insurer(None) == "Other"


def test_resolve_single_funded_payer():
    assert resolve_payer(["WCB Alberta", "Blue Cross"]) == ("WCB", [])


def test_resolve_no_funded_payer_is_other():
    assert resolve_payer(["Blue Cross", "ARTA RETIREE BENEFITS PLAN"]) == ("Other", [])


def test_resolve_multiple_funded_needs_ask():
    payer, options = resolve_payer(["WCB Alberta", "VAC"])
    assert payer is None
    assert options == ["Veterans Affairs", "WCB"]


def test_stated_payer_wins():
    assert resolve_payer(["WCB Alberta", "VAC"], stated_payer="Veterans Affairs") == (
        "Veterans Affairs", [],
    )


def test_duplicate_funded_bucket_is_single():
    # VAC + "Veterans Affairs Canada" are the same bucket — no ask needed.
    assert resolve_payer(["VAC", "Veterans Affairs Canada"]) == ("Veterans Affairs", [])


# ── Trace / reason quality ────────────────────────────────────────────────────


def test_reason_mentions_active_plan_when_covered():
    d = decide(_inputs(last_hearing_test_date=years_ago(2)))
    assert "Complete Care Plan" in d.reason


def test_trace_carries_rule_inputs():
    d = decide(_inputs(last_hearing_test_date=years_ago(2)))
    assert d.trace["allows_annual_tests"] is True
    assert d.trace["min_years_between_tests"] == 1.0
    assert d.trace["years_since_last_test"] == pytest.approx(2.0, abs=0.05)


# ── Patient-facing context (next-due date, coverage flag) ─────────────────────


def test_not_due_carries_next_annual_due_date():
    last = date(2026, 6, 1)
    d = decide(_inputs(last_hearing_test_date=last,
                       last_clinician_visit_date=years_ago(0.2)))
    assert d.outcome == "BOOK_TECHNICIAN_CLEAN_AND_CHECK"
    assert d.next_annual_due == date(2027, 6, 1)   # +12 months, payer Other
    assert "June 1, 2027" in d.reason              # the agent can answer "when am I due?"


def test_va_next_due_is_two_years_out():
    last = date(2025, 3, 15)
    d = decide(_inputs(payer_type="Veterans Affairs", last_hearing_test_date=last,
                       last_clinician_visit_date=years_ago(0.2)))
    assert d.next_annual_due == date(2027, 3, 15)


def test_due_now_has_no_next_due():
    d = decide(_inputs(last_hearing_test_date=years_ago(2)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"
    assert d.next_annual_due is None


def test_plan_not_covering_sets_flag_and_reason():
    d = decide(_inputs(
        care_plan_name="Pre-Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(3),
        last_clinician_visit_date=years_ago(0.2),
    ))
    assert d.annual_covered_by_plan is False
    assert d.next_annual_due is None
    assert "Pre-Plan" in d.reason and "doesn't currently cover" in d.reason
