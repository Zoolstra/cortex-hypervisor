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


def test_pre_plan_never_funds_an_annual_but_offers_self_pay():
    # Pre-Plan is still never FUNDED — but a due Pre-Plan patient is now offered
    # the test as self-pay rather than diverted to a service visit.
    d = decide(_inputs(
        care_plan_name="Pre-Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(3),
        last_clinician_visit_date=years_ago(2),
    ))
    assert d.outcome == "OFFER_SELF_PAY_ANNUAL_HEARING_TEST"
    assert d.annual_covered_by_plan is False


def test_pre_plan_not_due_still_takes_the_service_path():
    # The self-pay offer is gated on being DUE; a recently-tested Pre-Plan
    # patient goes to the service path exactly as before.
    d = decide(_inputs(
        care_plan_name="Pre-Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(0.2),
        last_clinician_visit_date=years_ago(2),
    ))
    assert d.outcome == "BOOK_CLINICIAN_SERVICE_VISIT"


def test_expired_qualifying_plan_still_covers_annual():
    # 2026-08-10 clinic revision, and a deliberate REVERSAL of the older rule
    # that treated an expired Complete Care Plan like Pre-Plan.
    d = decide(_inputs(
        care_plan_name="Complete Care Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(3),
        last_clinician_visit_date=years_ago(0.2),
    ))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"
    assert d.annual_covered_by_plan is True


@pytest.mark.parametrize("plan", ["Complete Care Plan", "CCP LACE", "Care Plan, No Batts"])
def test_all_qualifying_plans_cover_annual_when_expired(plan):
    d = decide(_inputs(care_plan_name=plan, care_plan_active=False,
                       last_hearing_test_date=years_ago(1.5)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_expiry_gating_is_restorable_by_config():
    # The pre-revision behavior stays reachable without a code change.
    rules = DecisionRules(expired_qualifying_allows_annual=False)
    d = decide(_inputs(
        care_plan_name="Complete Care Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(3),
        last_clinician_visit_date=years_ago(0.2),
    ), rules)
    assert d.outcome == "OFFER_SELF_PAY_ANNUAL_HEARING_TEST"


def test_no_plan_defaults_to_allowed():
    d = decide(_inputs(care_plan_name=None, care_plan_active=False,
                       last_hearing_test_date=years_ago(2)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_unknown_plan_default_can_be_tightened():
    # Tightening still denies the FUNDED annual; a due caller then gets the
    # self-pay offer instead of a covered booking.
    rules = DecisionRules(unknown_plan_allows_annual=False)
    d = decide(_inputs(care_plan_name="Mystery Plan", care_plan_active=True,
                       last_hearing_test_date=years_ago(2),
                       last_clinician_visit_date=years_ago(2)), rules)
    assert d.outcome == "OFFER_SELF_PAY_ANNUAL_HEARING_TEST"
    assert d.annual_covered_by_plan is False


def test_unknown_plan_tightened_and_not_due_goes_to_service():
    rules = DecisionRules(unknown_plan_allows_annual=False)
    d = decide(_inputs(care_plan_name="Mystery Plan", care_plan_active=True,
                       last_hearing_test_date=years_ago(0.2),
                       last_clinician_visit_date=years_ago(2)), rules)
    assert d.outcome == "BOOK_CLINICIAN_SERVICE_VISIT"


def test_plan_name_matching_tolerates_whitespace_and_case():
    # Feed variance must not fail OPEN past the deny rules: 'Pre-Plan ' with a
    # trailing space is still Pre-Plan, so still never FUNDED (self-pay, not a
    # covered annual).
    d = decide(_inputs(
        care_plan_name="Pre-Plan ", care_plan_active=False,
        last_hearing_test_date=years_ago(3),
        last_clinician_visit_date=years_ago(0.2),
    ))
    assert d.outcome == "OFFER_SELF_PAY_ANNUAL_HEARING_TEST"
    assert d.annual_covered_by_plan is False
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
    assert classify_insurer("Blue Cross") == "Blue Cross"
    assert classify_insurer("NIHB") == "NIHB"
    assert classify_insurer("AADL") == "AADL"
    assert classify_insurer(None) == "Other"
    assert classify_insurer("ARTA RETIREE BENEFITS PLAN") == "Other"


def test_alberta_program_names_do_not_collapse_into_the_bare_pattern():
    # ORDER TRAP: every one of these contains "alberta". If the bare provincial
    # pattern were matched first they would all become PAYER_ALBERTA and get
    # FUNDED on a 1-year interval instead of being routed to prior-auth or staff
    # review — a silent coverage claim we aren't entitled to make.
    assert classify_insurer("WCB Alberta") == "WCB"
    assert classify_insurer("Alberta Blue Cross") == "Blue Cross"
    assert classify_insurer("Alberta Aids to Daily Living") == "AADL"
    assert classify_insurer("Alberta AADL") == "AADL"
    # Only a name with no more specific program left resolves to Alberta.
    assert classify_insurer("Alberta Health") == "Alberta"


def test_bigstone_is_an_nihb_program():
    assert classify_insurer("Bigstone Cree Nation") == "NIHB"
    assert classify_insurer("Non-Insured Health Benefits") == "NIHB"


def test_resolve_single_identified_payer():
    assert resolve_payer(["WCB Alberta"]) == ("WCB", [])
    assert resolve_payer(["Alberta Blue Cross"]) == ("Blue Cross", [])


def test_resolve_no_identified_payer_is_other():
    assert resolve_payer(["ARTA RETIREE BENEFITS PLAN", "Self Pay"]) == ("Other", [])


def test_resolve_multiple_identified_needs_ask():
    payer, options = resolve_payer(["WCB Alberta", "VAC"])
    assert payer is None
    assert options == ["Veterans Affairs", "WCB"]


def test_staff_review_payer_still_triggers_the_ask_alongside_a_funder():
    # Blue Cross is not a "funder", but it changes whether the call is bookable
    # at all, so it must still force the question rather than being ignored in
    # favour of the WCB claim.
    payer, options = resolve_payer(["WCB Alberta", "Alberta Blue Cross"])
    assert payer is None
    assert options == ["Blue Cross", "WCB"]


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
    # Due + uncovered is now the self-pay offer, and the reason must name both
    # the plan and the price.
    d = decide(_inputs(
        care_plan_name="Pre-Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(3),
        last_clinician_visit_date=years_ago(0.2),
    ))
    assert d.outcome == "OFFER_SELF_PAY_ANNUAL_HEARING_TEST"
    assert d.annual_covered_by_plan is False
    assert d.next_annual_due is None
    assert "Pre-Plan" in d.reason
    assert "doesn't include a funded hearing test" in d.reason


def test_not_due_and_uncovered_still_explains_why_not():
    d = decide(_inputs(
        care_plan_name="Pre-Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(0.2),
        last_clinician_visit_date=years_ago(0.1),
    ))
    assert d.outcome == "BOOK_TECHNICIAN_CLEAN_AND_CHECK"
    assert "Pre-Plan" in d.reason and "doesn't currently cover" in d.reason


# ── Self-pay offer ────────────────────────────────────────────────────────────


def test_self_pay_carries_price_and_quotes_it_speakably():
    d = decide(_inputs(
        care_plan_name="Pre-Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(3),
    ))
    assert d.self_pay_price == 112.0
    # Whole dollars, not "$112.00" — TTS reads the cents aloud.
    assert "$112" in d.reason
    assert "$112.00" not in d.reason


def test_self_pay_price_is_configurable():
    rules = DecisionRules(self_pay_annual_price=139.5)
    d = decide(_inputs(
        care_plan_name="Pre-Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(3),
    ), rules)
    assert d.self_pay_price == 139.5
    assert "$139.50" in d.reason


def test_covered_outcomes_carry_no_price():
    d = decide(_inputs(last_hearing_test_date=years_ago(2)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"
    assert d.self_pay_price is None


# ── Payer actions (referral gates) ────────────────────────────────────────────


@pytest.mark.parametrize("payer", ["NIHB"])
def test_prior_auth_payer_refers_to_staff(payer):
    d = decide(_inputs(payer_type=payer, last_hearing_test_date=years_ago(3)))
    assert d.outcome == "REFER_TO_STAFF_PRIOR_AUTHORIZATION"


@pytest.mark.parametrize("payer", ["Blue Cross", "AADL"])
def test_human_review_payer_refers_to_staff(payer):
    d = decide(_inputs(payer_type=payer, last_hearing_test_date=years_ago(3)))
    assert d.outcome == "REFER_TO_STAFF_PAYER_REVIEW"


@pytest.mark.parametrize("payer", ["WCB", "Alberta", "Veterans Affairs", "Other"])
def test_funding_payers_book_normally(payer):
    d = decide(_inputs(payer_type=payer, last_hearing_test_date=years_ago(3)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_alberta_is_an_annual_interval():
    # Not due at 6 months, due at the anniversary.
    assert decide(_inputs(payer_type="Alberta",
                          last_hearing_test_date=years_ago(0.5),
                          last_clinician_visit_date=years_ago(0.1))
                  ).outcome == "BOOK_TECHNICIAN_CLEAN_AND_CHECK"
    d = decide(_inputs(payer_type="Alberta", last_hearing_test_date=date(2025, 7, 22)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_referral_gate_beats_a_due_covered_annual():
    # An NIHB patient with a qualifying plan who is overdue still must NOT be
    # offered a booking — authorization comes first.
    d = decide(_inputs(
        payer_type="NIHB", care_plan_name="Complete Care Plan",
        care_plan_active=True, last_hearing_test_date=years_ago(5),
    ))
    assert d.outcome == "REFER_TO_STAFF_PRIOR_AUTHORIZATION"


def test_referral_gate_beats_new_patient_intake():
    d = decide(_inputs(patient_is_existing=False, payer_type="AADL"))
    assert d.outcome == "REFER_TO_STAFF_PAYER_REVIEW"


def test_payer_action_is_configurable():
    # A clinic that negotiates direct billing can flip Blue Cross to funding.
    rules = DecisionRules(payer_actions={"Blue Cross": "fund"})
    d = decide(_inputs(payer_type="Blue Cross", last_hearing_test_date=years_ago(3)),
               rules)
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


# ── Minors ────────────────────────────────────────────────────────────────────


def test_minor_goes_to_a_person():
    d = decide(_inputs(date_of_birth=date(2012, 1, 1),   # 14 on TODAY
                       last_hearing_test_date=years_ago(3)))
    assert d.outcome == "REFER_TO_STAFF_MINOR"


def test_minor_gate_outranks_every_other_rule():
    d = decide(_inputs(
        date_of_birth=date(2012, 1, 1), payer_type="NIHB",
        patient_is_existing=False, care_plan_name="Pre-Plan",
    ))
    assert d.outcome == "REFER_TO_STAFF_MINOR"


def test_exactly_at_threshold_is_not_a_minor():
    d = decide(_inputs(date_of_birth=date(TODAY.year - 18, TODAY.month, TODAY.day),
                       last_hearing_test_date=years_ago(3)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_day_before_eighteenth_birthday_is_still_a_minor():
    dob = date(TODAY.year - 18, TODAY.month, TODAY.day) + timedelta(days=1)
    d = decide(_inputs(date_of_birth=dob, last_hearing_test_date=years_ago(3)))
    assert d.outcome == "REFER_TO_STAFF_MINOR"


def test_missing_dob_is_not_treated_as_a_minor():
    # Absence of a birthdate must NOT read as "under age" — that would send
    # every patient with an incomplete record to a human.
    d = decide(_inputs(date_of_birth=None, last_hearing_test_date=years_ago(3)))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


def test_minor_threshold_is_configurable():
    rules = DecisionRules(minor_age_threshold=0)
    d = decide(_inputs(date_of_birth=date(2012, 1, 1),
                       last_hearing_test_date=years_ago(3)), rules)
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"


# ── Clean-and-check gap ───────────────────────────────────────────────────────


def test_recent_clean_check_pushes_the_earliest_bookable_date():
    d = decide(_inputs(
        last_hearing_test_date=years_ago(3),
        last_clean_check_date=date(2026, 7, 1),   # 3 weeks before TODAY
    ))
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"
    assert d.earliest_bookable_date == date(2026, 10, 1)   # +3 months
    assert "October 1, 2026" in d.reason


def test_old_clean_check_imposes_no_constraint():
    d = decide(_inputs(
        last_hearing_test_date=years_ago(3),
        last_clean_check_date=years_ago(1),
    ))
    assert d.earliest_bookable_date is None


def test_clean_check_exactly_three_months_ago_is_bookable_now():
    d = decide(_inputs(
        last_hearing_test_date=years_ago(3),
        last_clean_check_date=date(2026, 4, 22),   # exactly 3 months before TODAY
    ))
    assert d.earliest_bookable_date is None


def test_clean_check_gap_also_applies_to_self_pay():
    d = decide(_inputs(
        care_plan_name="Pre-Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(3),
        last_clean_check_date=date(2026, 7, 1),
    ))
    assert d.outcome == "OFFER_SELF_PAY_ANNUAL_HEARING_TEST"
    assert d.earliest_bookable_date == date(2026, 10, 1)


def test_clean_check_gap_is_configurable():
    rules = DecisionRules(min_months_after_clean_check=0)
    d = decide(_inputs(
        last_hearing_test_date=years_ago(3),
        last_clean_check_date=date(2026, 7, 1),
    ), rules)
    assert d.earliest_bookable_date is None


def test_no_clean_check_on_file_imposes_no_constraint():
    d = decide(_inputs(last_hearing_test_date=years_ago(3),
                       last_clean_check_date=None))
    assert d.earliest_bookable_date is None


# ── Warranty bundling ─────────────────────────────────────────────────────────
#
# Clinic call 2026-07-27. The rule DELAYS the annual so it lands beside the
# warranty check — one visit instead of two. Direction matters: an earlier
# reading of this rule had it pulling appointments EARLIER, which is backwards
# and would have produced exactly the double-visit it exists to avoid.


def test_warranty_beyond_the_trigger_imposes_nothing():
    # "if we're six months away from the warranty expiring, we're safe."
    d = decide(_inputs(last_hearing_test_date=years_ago(2),
                       warranty_expiry_date=TODAY + timedelta(days=210)))  # ~7mo
    assert d.outcome == "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN"
    assert d.preferred_earliest_date is None
    assert d.preferred_latest_date is None


def test_warranty_inside_the_trigger_delays_into_the_window():
    # Larena's worked example: expiring in ~5 months → push ~3 months out so the
    # visit lands at the 2-month mark.
    expiry = date(2026, 12, 22)                      # ~5 months after TODAY
    d = decide(_inputs(last_hearing_test_date=years_ago(2),
                       warranty_expiry_date=expiry))
    assert d.preferred_earliest_date == date(2026, 10, 22)   # expiry − 2 months
    assert d.preferred_latest_date == expiry
    assert d.warranty_expiry_date == expiry


def test_warranty_window_is_soft_not_the_hard_floor():
    # The bundling preference must NOT become earliest_bookable_date — the agent
    # has to stay able to book sooner when the caller can't make the window.
    d = decide(_inputs(last_hearing_test_date=years_ago(2),
                       warranty_expiry_date=TODAY + timedelta(days=120)))
    assert d.preferred_earliest_date is not None
    assert d.earliest_bookable_date is None


def test_warranty_reason_explains_the_single_visit():
    d = decide(_inputs(last_hearing_test_date=years_ago(2),
                       warranty_expiry_date=date(2026, 12, 1)))
    assert "come out of warranty" in d.reason
    assert "same visit" in d.reason


def test_imminent_warranty_window_clamps_to_today():
    # Expiring in 3 weeks: expiry − 2 months is in the past. Any time now is
    # inside the window, so the start clamps rather than reading as a constraint.
    d = decide(_inputs(last_hearing_test_date=years_ago(2),
                       warranty_expiry_date=TODAY + timedelta(days=21)))
    assert d.preferred_earliest_date == TODAY


def test_expired_warranty_is_ignored():
    d = decide(_inputs(last_hearing_test_date=years_ago(2),
                       warranty_expiry_date=TODAY - timedelta(days=30)))
    assert d.preferred_earliest_date is None
    assert "warranty" not in d.reason.lower()


def test_no_warranty_on_file_is_ignored():
    d = decide(_inputs(last_hearing_test_date=years_ago(2),
                       warranty_expiry_date=None))
    assert d.preferred_earliest_date is None


def test_warranty_bundling_can_be_disabled():
    rules = DecisionRules(warranty_bundle_trigger_months=0)
    d = decide(_inputs(last_hearing_test_date=years_ago(2),
                       warranty_expiry_date=TODAY + timedelta(days=90)), rules)
    assert d.preferred_earliest_date is None


def test_warranty_window_also_applies_to_self_pay():
    d = decide(_inputs(
        care_plan_name="Pre-Plan", care_plan_active=False,
        last_hearing_test_date=years_ago(2),
        warranty_expiry_date=date(2026, 11, 30),
    ))
    assert d.outcome == "OFFER_SELF_PAY_ANNUAL_HEARING_TEST"
    assert d.preferred_earliest_date == date(2026, 9, 30)


def test_clean_check_floor_and_warranty_window_coexist():
    # Both can be active at once and must stay independent: the hard floor bars
    # earlier booking, the soft window says where to look first.
    d = decide(_inputs(
        last_hearing_test_date=years_ago(2),
        last_clean_check_date=date(2026, 7, 1),          # floor → 2026-10-01
        warranty_expiry_date=date(2026, 12, 22),         # window → 2026-10-22..
    ))
    assert d.earliest_bookable_date == date(2026, 10, 1)
    assert d.preferred_earliest_date == date(2026, 10, 22)
    assert d.preferred_latest_date == date(2026, 12, 22)


def test_referral_outcomes_carry_no_scheduling_hints():
    # Nothing to schedule, so no window should be advertised.
    d = decide(_inputs(payer_type="NIHB", last_hearing_test_date=years_ago(2),
                       warranty_expiry_date=TODAY + timedelta(days=60)))
    assert d.outcome == "REFER_TO_STAFF_PRIOR_AUTHORIZATION"
    assert d.preferred_earliest_date is None
    assert d.earliest_bookable_date is None
