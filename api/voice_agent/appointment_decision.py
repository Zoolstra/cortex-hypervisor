"""Appointment-type decision engine — deterministic, pure, unit-testable.

Implements the clinic's declarative eligibility spec for "annual"/"service"
booking requests. The LLM voice agent NEVER evaluates these rules: it calls the
``determine_appointment_type`` tool, the server runs this engine over PMS data,
and the agent narrates the result. That split (conversation in the model,
business rules in code) is the core fix for rule-following reliability.

Spec (confirmed with the clinic 2026-07-21):

  Inputs: today, patient_is_existing, care_plan (name + active?), payer_type,
  last_hearing_test_date, last_clinician_visit_date. Missing dates = infinitely
  old.

  Payer intervals (min years between funded tests): WCB 1.0, Veterans Affairs
  2.0, Other 1.0 (defaults; per-clinic config can override).

  Care plans allowing funded annuals: Complete Care Plan, CCP LACE,
  "Care Plan, No Batts" — when ACTIVE (unexpired). "Pre-Plan" never qualifies.
  Unknown/no plan defaults to allowed (config can tighten later).

  Decision table:
    1. not existing                                  -> BOOK_NEW_PATIENT_INTAKE_WITH_CLINICIAN
    2. allows_annual AND years_since_test >= min     -> BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN
    3. else if years_since_clinician_visit >= 1.0    -> BOOK_CLINICIAN_SERVICE_VISIT
       else                                          -> BOOK_TECHNICIAN_CLEAN_AND_CHECK

This module is I/O-free. Data loading lives in the Blueprint adapter; the
HTTP boundary lives in the blueprint router.
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal


Outcome = Literal[
    "BOOK_NEW_PATIENT_INTAKE_WITH_CLINICIAN",
    "BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN",
    "BOOK_CLINICIAN_SERVICE_VISIT",
    "BOOK_TECHNICIAN_CLEAN_AND_CHECK",
]

# Canonical payer buckets. Anything unrecognized resolves to "Other".
PAYER_WCB = "WCB"
PAYER_VA = "Veterans Affairs"
PAYER_OTHER = "Other"

DEFAULT_PAYER_MIN_YEARS: dict[str, float] = {
    PAYER_WCB: 1.0,
    PAYER_VA: 2.0,
    PAYER_OTHER: 1.0,
}

DEFAULT_QUALIFYING_PLANS = (
    "Complete Care Plan",
    "CCP LACE",
    "Care Plan, No Batts",
)
DEFAULT_NON_QUALIFYING_PLANS = ("Pre-Plan",)

DEFAULT_CLINICIAN_VISIT_THRESHOLD_YEARS = 1.0


@dataclass(frozen=True)
class DecisionRules:
    """Per-clinic tunables (populated from the protocol's config)."""

    qualifying_plan_names: tuple[str, ...] = DEFAULT_QUALIFYING_PLANS
    non_qualifying_plan_names: tuple[str, ...] = DEFAULT_NON_QUALIFYING_PLANS
    # A plan name in neither list ("unknown"): does it allow funded annuals?
    unknown_plan_allows_annual: bool = True
    payer_min_years: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PAYER_MIN_YEARS)
    )
    clinician_visit_threshold_years: float = DEFAULT_CLINICIAN_VISIT_THRESHOLD_YEARS


@dataclass(frozen=True)
class DecisionInputs:
    today: date
    patient_is_existing: bool
    # Latest-expiry service plan on file (from ClientAids); None = no plan rows.
    care_plan_name: str | None
    care_plan_active: bool           # expiry >= today (False when no plan)
    payer_type: str                  # one of the canonical buckets
    last_hearing_test_date: date | None
    last_clinician_visit_date: date | None


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    # Short, speakable explanation the agent can relay to the caller.
    reason: str
    # Machine-readable trace for tickets/analytics/debugging.
    trace: dict
    # Patient-facing facts the agent MAY share with the VERIFIED caller (it is
    # their own record): distinct from `trace`, which stays server-side.
    # next_annual_due: the date the next FUNDED annual becomes available
    # (None when the plan doesn't cover one, or when it's due now/never tested).
    annual_covered_by_plan: bool = True
    next_annual_due: date | None = None


def _years_since(today: date, d: date | None) -> float:
    """Years between d and today; missing date = infinitely old (per spec).

    Used for the human-readable trace/reason only — the due-date GATES use
    calendar-anniversary arithmetic (``_interval_elapsed``) so a caller on
    their exact anniversary counts as due (365/365.25 fractions would deny
    them by a fraction of a day).
    """
    if d is None:
        return math.inf
    return (today - d).days / 365.25


def _add_months(d: date, months: int) -> date:
    """Calendar-add months, clamping the day (Jan 31 +1mo → Feb 28/29)."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _interval_elapsed(today: date, since: date | None, years: float) -> bool:
    """True when at least ``years`` (calendar years, fractional → months) have
    elapsed since ``since``. Missing date = infinitely old = elapsed."""
    if since is None:
        return True
    return today >= _add_months(since, round(years * 12))


def _norm(s: str | None) -> str:
    """Casefolded, whitespace-trimmed key for plan-name comparison — the CSV
    feed's values aren't contractual about spacing/case, and an unmatched
    name FAILS OPEN (unknown → allowed by default), so match tolerantly."""
    return (s or "").strip().casefold()


def _allows_annual(name: str | None, active: bool, rules: DecisionRules) -> bool:
    """Care-plan gate for a FUNDED annual.

    Qualifying names allow only while the plan is active — an expired
    Complete Care Plan behaves like Pre-Plan (the spec's Pre-Plan=false rule
    exists precisely because those entries are all expired). No plan on file /
    unrecognized name falls to ``unknown_plan_allows_annual``. Names compare
    trimmed + case-insensitive so feed variance can't bypass the deny rules.
    """
    key = _norm(name)
    if not key:
        return rules.unknown_plan_allows_annual
    if key in {_norm(n) for n in rules.non_qualifying_plan_names}:
        return False
    if key in {_norm(n) for n in rules.qualifying_plan_names}:
        return bool(active)
    return rules.unknown_plan_allows_annual


def decide(inputs: DecisionInputs, rules: DecisionRules | None = None) -> Decision:
    """Run the decision table. Pure — no I/O, no clock reads."""
    r = rules or DecisionRules()

    years_since_test = _years_since(inputs.today, inputs.last_hearing_test_date)
    years_since_clinician = _years_since(inputs.today, inputs.last_clinician_visit_date)
    min_years = r.payer_min_years.get(
        inputs.payer_type, r.payer_min_years.get(PAYER_OTHER, 1.0)
    )
    allows = _allows_annual(inputs.care_plan_name, inputs.care_plan_active, r)
    # Calendar-anniversary gates (see _interval_elapsed) — the fractional
    # years_since_* values above are for the trace/reason text only.
    test_due = _interval_elapsed(inputs.today, inputs.last_hearing_test_date, min_years)
    clinician_stale = _interval_elapsed(
        inputs.today, inputs.last_clinician_visit_date,
        r.clinician_visit_threshold_years,
    )
    # When the next FUNDED annual becomes available — the direct answer to a
    # caller's "when am I due?". None when it's due now (or plan doesn't cover).
    next_due: date | None = None
    if allows and not test_due and inputs.last_hearing_test_date is not None:
        next_due = _add_months(inputs.last_hearing_test_date, round(min_years * 12))

    trace = {
        "care_plan_name": inputs.care_plan_name,
        "care_plan_active": inputs.care_plan_active,
        "allows_annual_tests": allows,
        "payer_type": inputs.payer_type,
        "min_years_between_tests": min_years,
        "years_since_last_test": None if math.isinf(years_since_test) else round(years_since_test, 2),
        "years_since_last_clinician_visit": (
            None if math.isinf(years_since_clinician) else round(years_since_clinician, 2)
        ),
    }

    if not inputs.patient_is_existing:
        return Decision(
            outcome="BOOK_NEW_PATIENT_INTAKE_WITH_CLINICIAN",
            reason="As a new patient, the right first step is a new-patient intake with a clinician.",
            trace=trace,
            annual_covered_by_plan=allows,
        )

    if allows and test_due:
        if inputs.last_hearing_test_date is None:
            why = "we don't have a recent hearing test on file"
        else:
            why = (
                "your last hearing test was on "
                f"{_speakable(inputs.last_hearing_test_date)}"
            )
        plan_bit = f" and your {inputs.care_plan_name} covers it" if (
            inputs.care_plan_name and inputs.care_plan_active
        ) else ""
        return Decision(
            outcome="BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN",
            reason=f"You're due for an annual hearing test — {why}{plan_bit}.",
            trace=trace,
            annual_covered_by_plan=allows,
        )

    # Not due for (or not covered for) a funded annual — service path. Give the
    # caller the concrete "why not / when instead" so the agent can answer
    # "when am I due?" directly instead of stonewalling a verified patient
    # about their own record.
    if not allows:
        not_due_bit = (
            f"your {inputs.care_plan_name} doesn't currently cover a funded annual test"
            if inputs.care_plan_name else
            "we don't have an active care plan covering annual tests on file for you"
        )
    elif next_due is not None:
        not_due_bit = (
            f"your next funded annual will be available on {_speakable(next_due)}"
        )
    else:
        not_due_bit = "you're not due for a funded annual test yet"

    if clinician_stale:
        return Decision(
            outcome="BOOK_CLINICIAN_SERVICE_VISIT",
            reason=(
                f"{not_due_bit[0].upper()}{not_due_bit[1:]}, and it's been over a "
                "year since you've seen a clinician — a clinician service visit is "
                "the right appointment."
            ),
            trace=trace,
            annual_covered_by_plan=allows,
            next_annual_due=next_due,
        )
    return Decision(
        outcome="BOOK_TECHNICIAN_CLEAN_AND_CHECK",
        reason=(
            f"{not_due_bit[0].upper()}{not_due_bit[1:]}, and you've seen a clinician "
            "recently — a technician clean-and-check is the right appointment."
        ),
        trace=trace,
        annual_covered_by_plan=allows,
        next_annual_due=next_due,
    )


def _speakable(d: date) -> str:
    """Voice-friendly date ("June 1, 2026") — %-d is glibc-only, so format
    manually for portability."""
    return f"{d.strftime('%B')} {d.day}, {d.year}"


# ── Payer resolution ──────────────────────────────────────────────────────────

# insurer_name (Blueprint) → canonical payer bucket, by substring match.
# ACNA reality: "WCB Alberta"; "VAC" + "Veterans Affairs Canada".
_FUNDED_PAYER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("wcb", PAYER_WCB),
    ("workers comp", PAYER_WCB),
    ("vac", PAYER_VA),
    ("veterans", PAYER_VA),
)


def classify_insurer(insurer_name: str | None) -> str:
    """Map a raw insurer_name to a canonical payer bucket."""
    n = (insurer_name or "").lower()
    for pat, bucket in _FUNDED_PAYER_PATTERNS:
        if pat in n:
            return bucket
    return PAYER_OTHER


def resolve_payer(
    active_insurer_names: list[str],
    stated_payer: str | None = None,
) -> tuple[str | None, list[str]]:
    """Resolve the payer bucket for the decision.

    Returns ``(payer_type, funded_options)``:
      - ``stated_payer`` (the caller's answer, already canonical) wins when given.
      - One distinct funded bucket on file → that bucket.
      - Zero funded buckets → "Other".
      - Multiple distinct funded buckets → ``(None, options)`` — the caller must
        be asked which program this visit falls under (clinic decision:
        agent asks rather than guessing precedence).
    """
    if stated_payer in (PAYER_WCB, PAYER_VA, PAYER_OTHER):
        return stated_payer, []
    funded = sorted({
        b for b in (classify_insurer(n) for n in active_insurer_names)
        if b != PAYER_OTHER
    })
    if len(funded) == 0:
        return PAYER_OTHER, []
    if len(funded) == 1:
        return funded[0], []
    return None, funded
