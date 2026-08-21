"""Appointment-type decision engine — deterministic, pure, unit-testable.

Implements the clinic's declarative eligibility spec for "annual"/"service"
booking requests. The LLM voice agent NEVER evaluates these rules: it calls the
``determine_appointment_type`` tool, the server runs this engine over PMS data,
and the agent narrates the result. That split (conversation in the model,
business rules in code) is the core fix for rule-following reliability.

Spec (confirmed with the clinic 2026-07-21, revised 2026-08-10):

  Inputs: today, patient_is_existing, care_plan (name + active?), payer_type,
  last_hearing_test_date, last_clinician_visit_date, last_clean_check_date,
  date_of_birth. Missing dates = infinitely old.

  Payer handling is two-dimensional — an ACTION (does this program let us book
  at all?) and, for funding programs, an INTERVAL:
    WCB 1.0y, Alberta 1.0y, Veterans Affairs 2.0y, Other 1.0y  -> fund
    NIHB / Bigstone            -> prior authorization required, staff must call
    Blue Cross / AADL          -> staff review (rarely cover the care plan)

  Care plans allowing funded annuals: Complete Care Plan, CCP LACE,
  "Care Plan, No Batts" — **regardless of expiry** (2026-08-10 revision; expiry
  used to gate them). "Pre-Plan" never qualifies, but a Pre-Plan patient who is
  otherwise due is OFFERED the test as self-pay at ``self_pay_annual_price``.
  Unknown/no plan defaults to allowed (config can tighten later).

  Callers under ``minor_age_threshold`` always go to a human — some minors do
  have payer eligibility, and that determination isn't safe to automate.

  A hearing test must sit at least ``min_months_after_clean_check`` months after
  the last technician clean-and-check; that pushes out the earliest bookable
  date rather than changing the outcome.

  Warranty bundling (clinic call 2026-07-27). A device coming out of warranty
  generates its own visit, so an annual booked shortly before one means seeing
  the patient twice for no-problem appointments. When a warranty expires within
  ``warranty_bundle_trigger_months``, the annual is DELAYED into the window
  ``warranty_bundle_window_months`` before expiry so both happen in one visit.
  Note the direction: this pushes the appointment LATER, never earlier. Beyond
  the trigger there is no constraint ("if we're six months away, we're safe").
  The window is a PREFERENCE, not a gate — a caller who can't make it is booked
  anyway and the ticket records that they'll miss the warranty check.

  Decision table (first match wins):
    1. under age threshold                           -> REFER_TO_STAFF_MINOR
    2. payer action = prior_auth                     -> REFER_TO_STAFF_PRIOR_AUTHORIZATION
    3. payer action = human_review                   -> REFER_TO_STAFF_PAYER_REVIEW
    4. not existing                                  -> BOOK_NEW_PATIENT_INTAKE_WITH_CLINICIAN
    5. allows_annual AND test_due                    -> BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN
    6. NOT allows_annual AND test_due                -> OFFER_SELF_PAY_ANNUAL_HEARING_TEST
    7. else if years_since_clinician_visit >= 1.0    -> BOOK_CLINICIAN_SERVICE_VISIT
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
    "OFFER_SELF_PAY_ANNUAL_HEARING_TEST",
    "BOOK_CLINICIAN_SERVICE_VISIT",
    "BOOK_TECHNICIAN_CLEAN_AND_CHECK",
    "REFER_TO_STAFF_PRIOR_AUTHORIZATION",
    "REFER_TO_STAFF_PAYER_REVIEW",
    "REFER_TO_STAFF_MINOR",
]

# Canonical payer buckets. Anything unrecognized resolves to "Other".
PAYER_WCB = "WCB"
PAYER_VA = "Veterans Affairs"
PAYER_ALBERTA = "Alberta"
PAYER_NIHB = "NIHB"
PAYER_BLUE_CROSS = "Blue Cross"
PAYER_AADL = "AADL"
# Workers' compensation from any board other than Alberta's. A separate bucket
# because only WCB Alberta funds an annual here — every other province's board
# goes to staff ("Only WCB Alberta is annual… work safe BC and WSIB and
# Worksafe other provinces, I would just put all of those onto like a real
# person" — clinic, 2026-07-27).
PAYER_WCB_OUT_OF_PROVINCE = "Workers Comp (out of province)"
PAYER_OTHER = "Other"

KNOWN_PAYERS: frozenset[str] = frozenset({
    PAYER_WCB, PAYER_VA, PAYER_ALBERTA, PAYER_NIHB,
    PAYER_BLUE_CROSS, PAYER_AADL, PAYER_WCB_OUT_OF_PROVINCE, PAYER_OTHER,
})

# What a payer program means for automated booking.
ACTION_FUND = "fund"                    # book normally, on the program's interval
ACTION_PRIOR_AUTH = "prior_auth"        # authorization needed before the test
ACTION_HUMAN_REVIEW = "human_review"    # coverage is case-by-case; staff decide

DEFAULT_PAYER_ACTIONS: dict[str, str] = {
    PAYER_WCB: ACTION_FUND,
    PAYER_ALBERTA: ACTION_FUND,
    PAYER_VA: ACTION_FUND,
    PAYER_OTHER: ACTION_FUND,
    PAYER_NIHB: ACTION_PRIOR_AUTH,
    PAYER_BLUE_CROSS: ACTION_HUMAN_REVIEW,
    PAYER_AADL: ACTION_HUMAN_REVIEW,
    PAYER_WCB_OUT_OF_PROVINCE: ACTION_HUMAN_REVIEW,
}

DEFAULT_PAYER_MIN_YEARS: dict[str, float] = {
    PAYER_WCB: 1.0,
    PAYER_ALBERTA: 1.0,
    PAYER_VA: 2.0,
    PAYER_OTHER: 1.0,
    # Present so a policy flipped to "fund" in config still has an interval
    # rather than silently falling back to Other's.
    PAYER_NIHB: 1.0,
    PAYER_BLUE_CROSS: 1.0,
    PAYER_AADL: 1.0,
}

DEFAULT_QUALIFYING_PLANS = (
    "Complete Care Plan",
    "CCP LACE",
    "Care Plan, No Batts",
)
DEFAULT_NON_QUALIFYING_PLANS = ("Pre-Plan",)

DEFAULT_CLINICIAN_VISIT_THRESHOLD_YEARS = 1.0
DEFAULT_SELF_PAY_ANNUAL_PRICE = 112.0
DEFAULT_MINOR_AGE_THRESHOLD = 18
DEFAULT_MIN_MONTHS_AFTER_CLEAN_CHECK = 3
# Inside this many months to warranty expiry, bundle the annual with the
# warranty check. Larena, 2026-07-27: "if we're six months away from the
# warranty expiring, we're safe... if it is less than five months, move it."
#
# FIVE, not six. Earlier in that same call she said "coming up within the next
# six months", then refined it under questioning into the rule above: at six
# months out the rule does NOT apply, and "less than five months" is what
# triggers the move. That looser first phrasing has already prompted one
# attempt to change this to 6 — the refinement is the rule.
DEFAULT_WARRANTY_BUNDLE_TRIGGER_MONTHS = 5
# Target: land the visit within this many months BEFORE expiry — where the
# clinic's own warranty-check notices go out.
DEFAULT_WARRANTY_BUNDLE_WINDOW_MONTHS = 2


@dataclass(frozen=True)
class DecisionRules:
    """Per-clinic tunables (populated from the protocol's config)."""

    qualifying_plan_names: tuple[str, ...] = DEFAULT_QUALIFYING_PLANS
    non_qualifying_plan_names: tuple[str, ...] = DEFAULT_NON_QUALIFYING_PLANS
    # A plan name in neither list ("unknown"): does it allow funded annuals?
    unknown_plan_allows_annual: bool = True
    # 2026-08-10: a qualifying plan covers a funded annual even once EXPIRED.
    # Set False to restore the pre-revision behavior (expiry gates coverage).
    expired_qualifying_allows_annual: bool = True
    payer_min_years: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PAYER_MIN_YEARS)
    )
    payer_actions: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_PAYER_ACTIONS)
    )
    clinician_visit_threshold_years: float = DEFAULT_CLINICIAN_VISIT_THRESHOLD_YEARS
    # Quoted to a caller whose plan doesn't fund the test but who is due for one.
    self_pay_annual_price: float = DEFAULT_SELF_PAY_ANNUAL_PRICE
    # Callers younger than this always go to a human (some minors have payer
    # eligibility; that call isn't safe to automate).
    minor_age_threshold: int = DEFAULT_MINOR_AGE_THRESHOLD
    # Minimum gap between a technician clean-and-check and a hearing test.
    min_months_after_clean_check: int = DEFAULT_MIN_MONTHS_AFTER_CLEAN_CHECK
    # Warranty bundling. Set trigger to 0 to disable the rule entirely.
    warranty_bundle_trigger_months: int = DEFAULT_WARRANTY_BUNDLE_TRIGGER_MONTHS
    warranty_bundle_window_months: int = DEFAULT_WARRANTY_BUNDLE_WINDOW_MONTHS


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
    # Most recent completed TECHNICIAN visit (practitioner not on the clinician
    # list). Gates how soon a hearing test may follow. None = none on file.
    last_clean_check_date: date | None = None
    date_of_birth: date | None = None
    # Soonest UPCOMING device warranty expiry on file. Already-expired
    # warranties are irrelevant to bundling, so the loader excludes them.
    warranty_expiry_date: date | None = None


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
    # HARD floor — a booking may not be placed before this. Set only by the
    # clean-and-check gap. None = no constraint, agent searches from today.
    earliest_bookable_date: date | None = None
    # SOFT target window for warranty bundling. The agent offers times in here
    # FIRST and explains why, but books outside it if the caller can't make it —
    # distinct from earliest_bookable_date, which it may never cross. Both None
    # when no warranty is close enough to matter.
    preferred_earliest_date: date | None = None
    preferred_latest_date: date | None = None
    # The expiry driving that window, so the agent can name it to the caller.
    warranty_expiry_date: date | None = None
    # Set only on OFFER_SELF_PAY_ANNUAL_HEARING_TEST — the price to quote.
    self_pay_price: float | None = None


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


def _age_on(today: date, dob: date | None) -> int | None:
    """Whole years old on ``today``. None when no DOB is on file — absence must
    NOT read as "under age", so callers treat None as "not a known minor"."""
    if dob is None:
        return None
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _norm(s: str | None) -> str:
    """Casefolded, whitespace-trimmed key for plan-name comparison — the CSV
    feed's values aren't contractual about spacing/case, and an unmatched
    name FAILS OPEN (unknown → allowed by default), so match tolerantly."""
    return (s or "").strip().casefold()


def _allows_annual(name: str | None, active: bool, rules: DecisionRules) -> bool:
    """Care-plan gate for a FUNDED annual.

    Qualifying names allow a funded annual regardless of the plan's expiry date
    (2026-08-10 clinic revision — an expired Complete Care Plan / CCP LACE /
    Care Plan No Batts still covers the annual). Setting
    ``expired_qualifying_allows_annual=False`` restores the older behavior where
    expiry gated coverage. "Pre-Plan" never qualifies. No plan on file /
    unrecognized name falls to ``unknown_plan_allows_annual``. Names compare
    trimmed + case-insensitive so feed variance can't bypass the deny rules.
    """
    key = _norm(name)
    if not key:
        return rules.unknown_plan_allows_annual
    if key in {_norm(n) for n in rules.non_qualifying_plan_names}:
        return False
    if key in {_norm(n) for n in rules.qualifying_plan_names}:
        return True if rules.expired_qualifying_allows_annual else bool(active)
    return rules.unknown_plan_allows_annual


def _payer_action(payer: str, rules: DecisionRules) -> str:
    """The booking action for a payer bucket; unknown buckets fund normally."""
    return rules.payer_actions.get(payer, ACTION_FUND)


def _warranty_window(
    inputs: DecisionInputs, rules: DecisionRules,
) -> tuple[date | None, date | None]:
    """The preferred (start, end) for bundling the annual with a warranty check.

    Returns ``(None, None)`` when the rule doesn't apply: no warranty on file,
    the rule is disabled, the expiry already passed, or expiry is further out
    than the trigger ("if we're six months away, we're safe").

    ``start`` is clamped to today — once inside the target window there is
    nothing to delay, and a past start would read as a constraint that isn't one.
    """
    expiry = inputs.warranty_expiry_date
    if expiry is None or rules.warranty_bundle_trigger_months <= 0:
        return None, None
    if expiry <= inputs.today:
        return None, None
    trigger = _add_months(inputs.today, rules.warranty_bundle_trigger_months)
    if expiry > trigger:
        return None, None
    start = _add_months(expiry, -rules.warranty_bundle_window_months)
    return max(start, inputs.today), expiry


def decide(inputs: DecisionInputs, rules: DecisionRules | None = None) -> Decision:
    """Run the decision table. Pure — no I/O, no clock reads."""
    r = rules or DecisionRules()

    years_since_test = _years_since(inputs.today, inputs.last_hearing_test_date)
    years_since_clinician = _years_since(inputs.today, inputs.last_clinician_visit_date)
    min_years = r.payer_min_years.get(
        inputs.payer_type, r.payer_min_years.get(PAYER_OTHER, 1.0)
    )
    action = _payer_action(inputs.payer_type, r)
    allows = _allows_annual(inputs.care_plan_name, inputs.care_plan_active, r)
    age = _age_on(inputs.today, inputs.date_of_birth)
    # Calendar-anniversary gates (see _interval_elapsed) — the fractional
    # years_since_* values above are for the trace/reason text only.
    test_due = _interval_elapsed(inputs.today, inputs.last_hearing_test_date, min_years)
    clinician_stale = _interval_elapsed(
        inputs.today, inputs.last_clinician_visit_date,
        r.clinician_visit_threshold_years,
    )
    # A test must sit at least N months after the last clean-and-check. Only a
    # date in the FUTURE is a real constraint; a gap already satisfied is None.
    earliest_bookable: date | None = None
    if inputs.last_clean_check_date is not None and r.min_months_after_clean_check > 0:
        gap_end = _add_months(
            inputs.last_clean_check_date, r.min_months_after_clean_check,
        )
        if gap_end > inputs.today:
            earliest_bookable = gap_end
    # Warranty bundling — a SOFT preference that DELAYS the visit so the annual
    # and the warranty check happen together. Only bites inside the trigger
    # window; beyond it "we're safe" and there is no constraint. A window whose
    # start is already in the past collapses to None, which is the right answer:
    # the warranty is imminent enough that any time now is inside it.
    pref_start, pref_end = _warranty_window(inputs, r)
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
        "payer_action": action,
        "min_years_between_tests": min_years,
        "age": age,
        "years_since_last_test": None if math.isinf(years_since_test) else round(years_since_test, 2),
        "years_since_last_clinician_visit": (
            None if math.isinf(years_since_clinician) else round(years_since_clinician, 2)
        ),
        "last_clean_check_date": (
            inputs.last_clean_check_date.isoformat()
            if inputs.last_clean_check_date else None
        ),
        "earliest_bookable_date": (
            earliest_bookable.isoformat() if earliest_bookable else None
        ),
        "warranty_expiry_date": (
            inputs.warranty_expiry_date.isoformat()
            if inputs.warranty_expiry_date else None
        ),
        "warranty_window": (
            [pref_start.isoformat(), pref_end.isoformat()]
            if pref_start and pref_end else None
        ),
    }

    # ── Referral gates ────────────────────────────────────────────────────────
    # These precede everything else: they say "no automated booking on this
    # call", so evaluating plan/interval rules past them would be wasted and
    # could leak a coverage claim we aren't entitled to make.

    if age is not None and age < r.minor_age_threshold:
        return Decision(
            outcome="REFER_TO_STAFF_MINOR",
            reason=(
                "Because this appointment is for someone under "
                f"{r.minor_age_threshold}, a team member needs to arrange it "
                "personally — coverage for younger patients is handled case by case."
            ),
            trace=trace,
            annual_covered_by_plan=allows,
        )

    if action == ACTION_PRIOR_AUTH:
        return Decision(
            outcome="REFER_TO_STAFF_PRIOR_AUTHORIZATION",
            reason=(
                f"Your coverage through {inputs.payer_type} needs approval in "
                "place before a hearing test, so a team member will take it from "
                "here and get back to you once that's arranged."
            ),
            trace=trace,
            annual_covered_by_plan=allows,
        )

    if action == ACTION_HUMAN_REVIEW:
        return Decision(
            outcome="REFER_TO_STAFF_PAYER_REVIEW",
            reason=(
                f"Coverage through {inputs.payer_type} varies from person to "
                "person, so a team member will confirm what applies to you and "
                "book the right appointment."
            ),
            trace=trace,
            annual_covered_by_plan=allows,
        )

    # ── Booking outcomes ──────────────────────────────────────────────────────

    if not inputs.patient_is_existing:
        return Decision(
            outcome="BOOK_NEW_PATIENT_INTAKE_WITH_CLINICIAN",
            reason="As a new patient, the right first step is a new-patient intake with a clinician.",
            trace=trace,
            annual_covered_by_plan=allows,
        )

    if inputs.last_hearing_test_date is None:
        why = "we don't have a recent hearing test on file"
    else:
        why = f"your last hearing test was on {_speakable(inputs.last_hearing_test_date)}"

    if allows and test_due:
        plan_bit = (
            f" and your {inputs.care_plan_name} covers it"
            if inputs.care_plan_name else ""
        )
        gap_bit = (
            f" Because you had a clean-and-check recently, the test needs to be on "
            f"or after {_speakable(earliest_bookable)}."
            if earliest_bookable else ""
        )
        return Decision(
            outcome="BOOK_ANNUAL_HEARING_TEST_WITH_CLINICIAN",
            reason=(
                f"You're due for an annual hearing test — {why}{plan_bit}.{gap_bit}"
                f"{_warranty_bit(inputs, pref_start, pref_end)}"
            ),
            trace=trace,
            annual_covered_by_plan=allows,
            earliest_bookable_date=earliest_bookable,
            preferred_earliest_date=pref_start,
            preferred_latest_date=pref_end,
            warranty_expiry_date=inputs.warranty_expiry_date,
        )

    if test_due:
        # Due for a test, but no plan funds it — offer it as self-pay. The agent
        # quotes the price and books only if the caller accepts.
        plan_bit = (
            f"your {inputs.care_plan_name} doesn't include a funded hearing test"
            if inputs.care_plan_name else
            "we don't have a plan on file that covers a hearing test for you"
        )
        gap_bit = (
            f" It would need to be on or after {_speakable(earliest_bookable)}, "
            "since you had a clean-and-check recently."
            if earliest_bookable else ""
        )
        return Decision(
            outcome="OFFER_SELF_PAY_ANNUAL_HEARING_TEST",
            reason=(
                f"You're due for a hearing test — {why} — but {plan_bit}, so it "
                f"would be {_speakable_price(r.self_pay_annual_price)} out of "
                f"pocket.{gap_bit}{_warranty_bit(inputs, pref_start, pref_end)}"
            ),
            trace=trace,
            annual_covered_by_plan=allows,
            earliest_bookable_date=earliest_bookable,
            preferred_earliest_date=pref_start,
            preferred_latest_date=pref_end,
            warranty_expiry_date=inputs.warranty_expiry_date,
            self_pay_price=r.self_pay_annual_price,
        )

    # Not due for a test — service path. Give the caller the concrete
    # "why not / when instead" so the agent can answer "when am I due?"
    # directly instead of stonewalling a verified patient about their own record.
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


def _warranty_bit(
    inputs: DecisionInputs, start: date | None, end: date | None,
) -> str:
    """Speakable rationale for the bundling delay, or '' when it doesn't apply.

    Phrased as the clinic frames it — one visit instead of two — because the
    caller is being asked to wait longer than they need to, and "we can do your
    test and your warranty check together" is the reason that makes that
    reasonable. Without it the delay just sounds like poor availability.
    """
    if not (start and end and inputs.warranty_expiry_date):
        return ""
    return (
        f" Your hearing aids come out of warranty on "
        f"{_speakable(inputs.warranty_expiry_date)}, so it's best to do the test "
        f"and a warranty check on your devices in the same visit — ideally "
        f"between {_speakable(start)} and {_speakable(end)}."
    )


def _speakable_price(amount: float) -> str:
    """Voice-friendly price — drop the cents when they're zero ("$112", not
    "$112.00", which TTS reads as "one hundred twelve point zero zero")."""
    if float(amount).is_integer():
        return f"${int(amount)}"
    return f"${amount:.2f}"


# ── Payer resolution ──────────────────────────────────────────────────────────

# insurer_name (Blueprint) → canonical payer bucket, by substring match.
#
# ORDER IS LOAD-BEARING — first match wins, and Alberta program names nest:
# "WCB Alberta", "Alberta Blue Cross", and "Alberta Aids to Daily Living" all
# contain "alberta". The specific programs MUST precede the bare "alberta"
# fallback or every one of them collapses into PAYER_ALBERTA and gets funded on
# a 1-year interval instead of being routed to prior-auth / staff review.
#
# ACNA reality: "WCB Alberta"; "VAC" + "Veterans Affairs Canada".
_PAYER_PATTERNS: tuple[tuple[str, str], ...] = (
    # Veterans first — unambiguous. Workers' comp is NOT here: it needs a
    # province split rather than a single bucket, so classify_insurer handles
    # it before this table is consulted.
    ("vac", PAYER_VA),
    ("veterans", PAYER_VA),
    # Prior-authorization programs. Bigstone is a First Nation administering
    # NIHB benefits, so it lands in the same bucket.
    ("nihb", PAYER_NIHB),
    ("non-insured health", PAYER_NIHB),
    ("bigstone", PAYER_NIHB),
    # Staff-review programs. Both nest inside "alberta …" names.
    ("blue cross", PAYER_BLUE_CROSS),
    ("bluecross", PAYER_BLUE_CROSS),
    ("aadl", PAYER_AADL),
    ("aids to daily living", PAYER_AADL),
    # Bare provincial fallback — MUST stay last.
    ("alberta", PAYER_ALBERTA),
)


# Any workers'-compensation board, in the spellings ACNA's records actually
# use: WCB Alberta, WorkSafe BC, WSIB, WCB Manitoba/-SK/NS/NB/-NWT, and
# "Workers Safety & Compensation Commission".
_WORKERS_COMP_MARKERS: tuple[str, ...] = (
    "wcb",
    "worksafe",
    "work safe",
    "wsib",
    "workers comp",
    "worker's comp",
    "workers' comp",
    "workers safety",
    "compensation board",
    "compensation commission",
)


def classify_insurer(insurer_name: str | None) -> str:
    """Map a raw insurer_name to a canonical payer bucket."""
    n = (insurer_name or "").lower()
    # Workers' comp is split by PROVINCE before anything else, because only
    # Alberta's board funds an annual here. Matching a bare "wcb" to one
    # fundable bucket silently gave WCB Manitoba/-SK/NS/NB/-NWT Alberta's
    # eligibility, and the boards that don't say "WCB" at all (WorkSafe BC,
    # WSIB) fell through to `Other`, which also funds. Alberta is the
    # allow-list, not the fall-through.
    if any(m in n for m in _WORKERS_COMP_MARKERS):
        return PAYER_WCB if "alberta" in n else PAYER_WCB_OUT_OF_PROVINCE
    for pat, bucket in _PAYER_PATTERNS:
        if pat in n:
            return bucket
    return PAYER_OTHER


def resolve_payer(
    active_insurer_names: list[str],
    stated_payer: str | None = None,
) -> tuple[str | None, list[str]]:
    """Resolve the payer bucket for the decision.

    Returns ``(payer_type, identified_options)``:
      - ``stated_payer`` (the caller's answer, already canonical) wins when given.
      - One distinct identified bucket on file → that bucket.
      - Zero identified buckets → "Other".
      - Multiple distinct identified buckets → ``(None, options)`` — the caller
        must be asked which program this visit falls under (clinic decision:
        agent asks rather than guessing precedence).

    "Identified" means any bucket other than ``Other``, including the ones that
    route to staff — a patient holding both WCB and Alberta Blue Cross needs to
    say which applies before we can tell whether the call is bookable at all.
    """
    if stated_payer in KNOWN_PAYERS:
        return stated_payer, []
    identified = sorted({
        b for b in (classify_insurer(n) for n in active_insurer_names)
        if b != PAYER_OTHER
    })
    if len(identified) == 0:
        return PAYER_OTHER, []
    if len(identified) == 1:
        return identified[0], []
    return None, identified
