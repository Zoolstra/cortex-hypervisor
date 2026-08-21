"""The Recoverable-revenue estimate must count PEOPLE, and the same people the
funnel counts calls for.

Two ways this goes wrong, both silent:

  1. Multiplying the CALL count by the average invoice. `qualified_not_booked`
     counts calls, so a caller who rang three times would treble the estimate.
  2. Drifting from the funnel's definition. The funnel splits no-booking NEW
     callers on `looking_to_book`, while `_BUCKET_PREDICATE` uses `qualified`
     (contract §16 — a documented divergence, reproduced as-is). A distinct-caller
     count built on the wrong flag would disagree with the call count printed
     beside it on the same tile.

No BigQuery — `_client` is stubbed, as in the other query tests.
"""
from __future__ import annotations

import pytest

from intelligence_report import queries as q


class _FakeRow:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _FakeClient:
    def __init__(self, rows):
        self.sql: list[str] = []
        self._rows = rows

    def query(self, sql, job_config=None):
        self.sql.append(sql)
        return _FakeJob(self._rows)


# Every field the funnel reads, zeroed; individual tests override what they assert.
_FUNNEL_FIELDS = dict(
    total=0, no_transcript=0, spam=0, wrong_number=0, genuine=0, connected=0,
    connected_new=0, connected_existing=0, missed=0, voicemail=0, hangup=0,
    booked=0, booked_new=0, booked_existing=0, booked_existing_active=0,
    booked_existing_lapsing=0, booked_existing_deep_dormant=0,
    booked_existing_never=0, existing_patient=0, existing_active=0,
    existing_lapsed=0, existing_lapsing=0, existing_deep_dormant=0,
    existing_dormant_never=0, unconfirmed_booking=0, qualified_not_booked=0,
    qualified_not_booked_callers=0, other=0,
)


@pytest.fixture
def run(monkeypatch):
    def _run(**overrides):
        client = _FakeClient([_FakeRow(**{**_FUNNEL_FIELDS, **overrides})])
        monkeypatch.setattr(q, "_client", lambda: client)
        out = q.call_outcomes_funnel(
            "C1", ["10009431"], window=q.Window("2026-05-19", "2026-08-18"))
        return out, client.sql[0]
    return _run


def test_caller_count_reuses_the_funnels_own_predicate(run):
    """Verbatim reuse, so the tile's two numbers cannot describe different cohorts."""
    _, sql = run()
    # `NOT appt_booked` is load-bearing: an unconfirmed booking is its own bucket
    # now (contract §4a), and pricing those callers as recoverable revenue was
    # exactly the overstatement that change removed. The caller count MUST carry
    # the exclusion or the tile prices a cohort the funnel no longer reports.
    assert "NOT appt_booked AND looking_to_book" in sql
    distinct = sql.split("COUNT(DISTINCT IF(", 1)[1].split("NULL)) AS", 1)[0]
    assert "NOT appt_booked" in distinct
    assert "NOT reconciled" in distinct and "NOT existing" in distinct
    # And on `looking_to_book`, NOT `qualified` — see §16.
    distinct = sql.split("COUNT(DISTINCT IF(", 1)[1]
    assert "looking_to_book" in distinct.split("NULL)) AS", 1)[0]


def test_caller_count_is_distinct_on_the_normalised_phone(run):
    _, sql = run()
    distinct = sql.split("COUNT(DISTINCT IF(", 1)[1].split("NULL)) AS", 1)[0]
    # Last 10 digits, matching every other phone match in this file.
    assert "RIGHT(REGEXP_REPLACE(IFNULL(phone_raw, ''), r'\\D', ''), 10)" in distinct
    # An unusable number falls back to the call id rather than collapsing every
    # such call into a single phantom caller.
    assert "complete_call_id" in distinct


def test_callers_are_reported_separately_from_calls(run):
    out, _ = run(qualified_not_booked=7, qualified_not_booked_callers=4)
    assert out["qualified_not_booked"] == 7
    assert out["qualified_not_booked_callers"] == 4


def test_callers_never_exceed_calls_in_the_shape_we_export(run):
    """One caller per call at most — a sanity invariant on the exported pair."""
    out, _ = run(qualified_not_booked=5, qualified_not_booked_callers=5)
    assert out["qualified_not_booked_callers"] <= out["qualified_not_booked"]


def test_zeroed_shape_carries_the_caller_key(monkeypatch):
    """A fail-safe return must include it, or the tile reads `undefined` and
    suppresses itself for a reason the reader cannot see."""
    class _Boom:
        def query(self, sql, job_config=None):
            raise RuntimeError("no such table")
    monkeypatch.setattr(q, "_client", lambda: _Boom())
    out = q.call_outcomes_funnel(
        "C1", ["1"], window=q.Window("2026-05-19", "2026-08-18"))
    assert out["qualified_not_booked_callers"] == 0


def test_unconfirmed_bookings_leave_the_recoverable_buckets(run):
    """The transcript says they booked, so they are neither a lost lead nor
    recoverable revenue — they are a PMS reconciliation gap."""
    _, sql = run()
    assert "AS unconfirmed_booking" in sql
    # Excluded from BOTH no-booking buckets, not just the qualified one.
    qnb = sql.split("AS unconfirmed_booking", 1)[1]
    assert "NOT appt_booked AND looking_to_book" in qnb
    assert "NOT appt_booked AND NOT looking_to_book" in qnb


def test_unconfirmed_booking_is_reported(run):
    out, _ = run(unconfirmed_booking=6, qualified_not_booked=17,
                 qualified_not_booked_callers=17)
    assert out["unconfirmed_booking"] == 6
    assert out["qualified_not_booked"] == 17
