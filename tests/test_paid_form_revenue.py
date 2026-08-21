"""Tests for `queries.paid_form_revenue` — the web-form leg of ad-attributed
revenue.

The thing worth testing here is not the arithmetic, it is the DOUBLE-COUNT
GUARD. This figure exists to be added to `paid_call_revenue`, so if the
incremental exclusion silently stops working, the ads tab over-reports revenue
and nothing on screen looks wrong. That failure is invisible at the clinic whose
data prompted the feature (Virsono's call/form populations barely overlap), which
is exactly why it needs a test rather than a spot check.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest

from intelligence_report import queries as q

WINDOW = q.Window("2026-01-01", "2026-08-01")


class _FakeRow:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeClient:
    """Captures SQL and returns canned rows."""

    def __init__(self, rows):
        self._rows = rows
        self.sql: list[str] = []
        self.params: list[list] = []

    def query(self, sql, job_config=None):
        self.sql.append(sql)
        self.params.append(list(job_config.query_parameters) if job_config else [])

        class _Job:
            def __init__(self, rows):
                self._rows = rows

            def result(self):
                return self._rows

        return _Job(self._rows)


ROW = dict(submissions=16, matched_patients=15, returning_patients=3,
           invoiced_patients=2, invoice_count=2, revenue=20980.0)


# ── the double-count guard ───────────────────────────────────────────────────

def test_exclusion_clause_present_by_default(monkeypatch):
    """The incremental exclusion must be in the SQL unless explicitly disabled.

    Asserted on the SQL rather than the result because a canned row cannot show
    whether the engine was asked to exclude anything.
    """
    client = _FakeClient([_FakeRow(**ROW)])
    monkeypatch.setattr(q, "_client", lambda: client)
    q.paid_form_revenue("C1", window=WINDOW, invoca_campaign_ids=["1"])
    sql = client.sql[0]
    assert "call_patients" in sql
    assert "NOT EXISTS" in sql
    # The exclusion must key on the patient, not the phone: two patient records
    # for one person is normal, and excluding on phone would leak one of them.
    assert "cp.client_id = fx.client_id" in sql


def test_no_exclusion_when_disabled(monkeypatch):
    client = _FakeClient([_FakeRow(**ROW)])
    monkeypatch.setattr(q, "_client", lambda: client)
    q.paid_form_revenue("C1", window=WINDOW, exclude_call_patients=False)
    sql = client.sql[0]
    assert "NOT EXISTS" not in sql
    assert "call_patients" not in sql


def test_no_invoca_returns_full_leg_not_zeros(monkeypatch):
    """No Invoca campaigns → the FULL form leg, with the exclusion dropped.

    There is no paid-call population to be incremental to (`paid_call_revenue`
    returns zeros under the same condition), so nothing can be double-counted
    and the full leg is correct. This shipped as a hard zero first, which blanked
    the entire leg for every clinic running web forms WITHOUT Invoca call
    tracking — precisely the clinics where form revenue is the only ad-attributed
    revenue that exists.
    """
    client = _FakeClient([_FakeRow(**ROW)])
    monkeypatch.setattr(q, "_client", lambda: client)
    out = q.paid_form_revenue("C1", window=WINDOW, invoca_campaign_ids=[])
    assert out["revenue"] == 20980.0
    # The exclusion must be absent — there is no call population to build it from.
    assert "NOT EXISTS" not in client.sql[0]
    assert "call_patients" not in client.sql[0]


def test_no_invoca_still_reports_window_days(monkeypatch):
    """Zero-row result must still carry window_days — the UI labels the period
    from it even when the leg is empty."""
    client = _FakeClient([])
    monkeypatch.setattr(q, "_client", lambda: client)
    out = q.paid_form_revenue("C1", window=WINDOW, invoca_campaign_ids=[])
    assert out["window_days"] == WINDOW.span_days


# ── invoice window: the ROAS-grade rule ──────────────────────────────────────

def test_invoices_are_window_bounded_not_all_history(monkeypatch):
    """Unlike `webform_revenue`, invoices must be bounded ON BOTH SIDES.

    webform_revenue counts every invoice on/after the first submission with no
    upper bound, which is lifetime value and cannot be a ROAS numerator against
    one period's spend. Regressing to that rule would inflate ROAS silently.
    """
    client = _FakeClient([_FakeRow(**ROW)])
    monkeypatch.setattr(q, "_client", lambda: client)
    q.paid_form_revenue("C1", window=WINDOW, exclude_call_patients=False)
    sql = client.sql[0]
    assert f"DATE '{WINDOW.start}'" in sql or str(WINDOW.start) in sql
    # The all-history gate from webform_revenue must NOT appear.
    assert ">= m.first_form_date" not in sql


def test_submission_touch_window_is_bounded(monkeypatch):
    client = _FakeClient([_FakeRow(**ROW)])
    monkeypatch.setattr(q, "_client", lambda: client)
    q.paid_form_revenue("C1", window=WINDOW, exclude_call_patients=False)
    sql = client.sql[0]
    assert "submitted_at >= TIMESTAMP(" in sql
    assert "submitted_at < TIMESTAMP(" in sql


# ── match key + fan-out ──────────────────────────────────────────────────────

def test_matches_on_phone_or_email(monkeypatch):
    client = _FakeClient([_FakeRow(**ROW)])
    monkeypatch.setattr(q, "_client", lambda: client)
    q.paid_form_revenue("C1", window=WINDOW, exclude_call_patients=False)
    sql = client.sql[0]
    assert "f.phone_norm = p.phone_norm" in sql
    assert "f.email_norm = p.email_norm" in sql
    # Phone must be length-checked: a truncated number would match many patients.
    assert "LENGTH(f.phone_norm) = 10" in sql


def test_collapses_to_distinct_patients_before_summing(monkeypatch):
    """One form row can fan out to several patient records (duplicate PMS
    records for one person — measured up to 2 in the Virsono backfill). The
    collapse has to happen before any invoice is touched, or that person's
    invoices are summed once per duplicate record."""
    client = _FakeClient([_FakeRow(**ROW)])
    monkeypatch.setattr(q, "_client", lambda: client)
    q.paid_form_revenue("C1", window=WINDOW, exclude_call_patients=False)
    sql = client.sql[0]
    assert "GROUP BY p.client_id" in sql
    # Invoices deduped per order as well, matching every other revenue path.
    assert "GROUP BY im.client_id, im.order_id" in sql


# ── disclosure fields ────────────────────────────────────────────────────────

def test_returning_patients_reported(monkeypatch):
    """The existing-customer count must survive to the caller: it is the part of
    this leg with the weakest causal claim, and the UI shows it."""
    client = _FakeClient([_FakeRow(**ROW)])
    monkeypatch.setattr(q, "_client", lambda: client)
    out = q.paid_form_revenue("C1", window=WINDOW, exclude_call_patients=False)
    assert out["returning_patients"] == 3


def test_fails_safe_to_zeros_on_query_error(monkeypatch):
    """A clinic with no webforms table access must read 0, never raise — the ads
    tab has to render for every clinic."""
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("no such table")

    monkeypatch.setattr(q, "_client", lambda: _Boom())
    out = q.paid_form_revenue("C1", window=WINDOW, exclude_call_patients=False)
    assert out["revenue"] == 0.0
    assert out["submissions"] == 0


# ── does it compile? ─────────────────────────────────────────────────────────

@pytest.mark.skipif(
    not os.environ.get("CORTEX_BQ_VALIDATE"),
    reason="needs live ADC; set CORTEX_BQ_VALIDATE=1 to run",
)
@pytest.mark.parametrize("exclude", [True, False])
def test_sql_compiles_against_bigquery(exclude):
    """Dry-run both branches. The SQL is assembled by string concatenation with
    an optional CTE spliced in, which is exactly the shape that produces text
    that reads fine and fails to parse — the class of bug the assertions above
    cannot catch."""
    from google.cloud import bigquery
    import unittest.mock as mock

    captured = {}

    class _Stop(Exception):
        pass

    class _DryRunClient:
        def query(self, sql, job_config=None):
            captured["sql"] = sql
            captured["params"] = list(job_config.query_parameters) if job_config else []
            raise _Stop

    with mock.patch.object(q, "_client", lambda: _DryRunClient()):
        try:
            q.paid_form_revenue(
                "C1", window=WINDOW, exclude_call_patients=exclude,
                invoca_campaign_ids=["1"] if exclude else None)
        except _Stop:
            pass

    assert "sql" in captured, "query was never issued"
    client = bigquery.Client(project="project-demo-2-482101")
    job = client.query(captured["sql"], job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False, query_parameters=captured["params"]))
    assert job.errors is None, job.errors


# ── per-campaign attribution + the single merge point ────────────────────────

def test_campaign_cascade_prefers_gad_campaignid(monkeypatch):
    """gad_campaignid must be checked BEFORE the gclid join.

    It resolved 13 of 13 click-bearing submissions where gclid resolved 9 — the
    other 4 fell outside ad_clicks_v2's 7-day settle window. Reversing the tiers
    would silently push those into the remainder row.
    """
    client = _FakeClient([])
    monkeypatch.setattr(q, "_client", lambda: client)
    q.webform_campaign_attribution("C1", ["111"], window=WINDOW)
    sql = client.sql[0]
    gad = sql.index("f.gad_campaignid IN UNNEST(@ga_ids)")
    gcl = sql.index("cl.cid IS NOT NULL")
    assert gad < gcl, "gclid tier must not precede gad_campaignid"


def test_campaign_cascade_has_no_name_tier(monkeypatch):
    """No campaign-NAME fallback. Form utm_campaign values are generic labels
    ("hearing-aid-search"), not Ads campaign names, so a name tier places nothing
    while risking false matches for clients whose labels collide."""
    client = _FakeClient([])
    monkeypatch.setattr(q, "_client", lambda: client)
    q.webform_campaign_attribution("C1", ["111"], window=WINDOW)
    assert "utm_campaign" not in client.sql[0]


def test_campaign_scoped_to_linked_campaigns(monkeypatch):
    """A click or campaign id belonging to another clinic in the same instance
    must not cross-attribute — both tiers are restricted to @ga_ids."""
    client = _FakeClient([])
    monkeypatch.setattr(q, "_client", lambda: client)
    q.webform_campaign_attribution("C1", ["111"], window=WINDOW)
    assert client.sql[0].count("UNNEST(@ga_ids)") >= 2


def test_no_ga_campaigns_returns_empty(monkeypatch):
    client = _FakeClient([])
    monkeypatch.setattr(q, "_client", lambda: client)
    assert q.webform_campaign_attribution("C1", [], window=WINDOW) == []
    assert client.sql == []


def test_merge_adds_totals_without_touching_frozen_fields():
    rows = [{"campaign_id": "A", "campaign_name": "Beta A", "revenue": 1000.0,
             "spend": 500.0, "calls": 10, "roas": 2.0, "unattributed": False}]
    forms = [{"campaign_id": "A", "forms": 3, "revenue": 200.0, "unattributed": False}]
    out = q.merge_campaign_forms(rows, forms)
    assert len(out) == 1
    r = out[0]
    # Frozen call-only fields survive untouched — the parity harness reads them.
    assert r["revenue"] == 1000.0 and r["roas"] == 2.0
    assert r["forms"] == 3 and r["form_revenue"] == 200.0
    assert r["revenue_total"] == 1200.0
    assert r["roas_total"] == 1200.0 / 500.0


def test_merge_appends_a_real_campaign_with_forms_and_no_calls():
    """A campaign can draw form fills and no paid calls, so it has no row from
    google_ads_roi. It must be appended rather than dropped, or those submissions
    disappear. Zero clicks/spend on the appended row, so it cannot invent a ROAS.

    (The *unattributed* forms remainder is a different case — it is dissolved into
    the real campaigns; see test_distribution_runs_without_a_headline_total.)"""
    out = q.merge_campaign_forms(
        [{"campaign_id": "A", "campaign_name": "Beta A", "revenue": 0.0,
          "spend": 100.0, "clicks": 10, "roas": 0.0, "unattributed": False}],
        [{"campaign_id": "B", "forms": 2, "revenue": 400.0, "unattributed": False}])
    assert len(out) == 2
    added = [r for r in out if r["campaign_id"] == "B"][0]
    assert added["forms"] == 2 and added["form_revenue"] == 400.0
    assert added["spend"] == 0.0 and added["roas_total"] is None
    assert not [r for r in out if r.get("unattributed")]


def test_merge_is_a_noop_without_a_forms_section():
    """Payload predating form attribution → totals still present and equal to the
    call figures, so the UI can read revenue_total unconditionally."""
    rows = [{"campaign_id": "A", "campaign_name": "Beta A", "revenue": 900.0,
             "spend": 300.0, "roas": 3.0, "unattributed": False}]
    out = q.merge_campaign_forms(rows, None)
    assert out[0]["forms"] == 0
    assert out[0]["revenue_total"] == 900.0
    assert out[0]["roas_total"] == 3.0


@pytest.mark.skipif(
    not os.environ.get("CORTEX_BQ_VALIDATE"),
    reason="needs live ADC; set CORTEX_BQ_VALIDATE=1 to run",
)
@pytest.mark.parametrize("iv", [["1"], None])
def test_campaign_sql_compiles_against_bigquery(iv):
    """Both branches of the call-exclusion splice."""
    from google.cloud import bigquery
    import unittest.mock as mock

    captured = {}

    class _Stop(Exception):
        pass

    class _DryRunClient:
        def query(self, sql, job_config=None):
            captured["sql"] = sql
            captured["params"] = list(job_config.query_parameters) if job_config else []
            raise _Stop

    with mock.patch.object(q, "_client", lambda: _DryRunClient()):
        try:
            q.webform_campaign_attribution(
                "C1", ["111"], window=WINDOW, invoca_campaign_ids=iv)
        except _Stop:
            pass

    client = bigquery.Client(project="project-demo-2-482101")
    job = client.query(captured["sql"], job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False, query_parameters=captured["params"]))
    assert job.errors is None, job.errors


# ── distributing the unplaced form leg across campaigns ──────────────────────

def test_unplaced_form_revenue_reaches_the_campaign_row():
    """The Princeton case. Its headline was $7,240 of form revenue over $5,737 of
    spend — 1.3x — while the Beta Princeton row read $0 and 0.0x, because its paid
    forms carry no campaign id. A row showing 0.0x against real spend beside a
    headline showing a return is the inconsistency; the row must carry it."""
    rows = [{"campaign_id": "P", "campaign_name": "Beta Princeton", "revenue": 0.0,
             "spend": 5737.0, "clicks": 655, "calls": 345, "roas": 0.0,
             "unattributed": False}]
    forms = [{"campaign_id": "__unattributed__", "forms": 2, "revenue": 0.0,
              "unattributed": True}]
    out = q.merge_campaign_forms(rows, forms, total_form_revenue=7240.0)
    row = [r for r in out if r["campaign_id"] == "P"][0]
    assert row["revenue_total"] == 7240.0
    assert abs(row["roas_total"] - 7240.0 / 5737.0) < 1e-9
    # The counts follow the revenue, or the two columns describe different people.
    assert row["forms"] == 2
    # Nothing left behind in an empty remainder row.
    assert not [r for r in out if r.get("unattributed")]


def test_distribution_is_weighted_by_clicks_and_sums_exactly():
    """Clicks are the traffic that produced the submissions, so they weight the
    split. Both columns must sum to their true totals — a per-row rounding drift
    would make the table stop reconciling with the headline."""
    rows = [
        {"campaign_id": "A", "campaign_name": "A", "revenue": 0.0, "spend": 100.0,
         "clicks": 300, "roas": 0.0, "unattributed": False},
        {"campaign_id": "B", "campaign_name": "B", "revenue": 0.0, "spend": 100.0,
         "clicks": 100, "roas": 0.0, "unattributed": False},
    ]
    forms = [{"campaign_id": "__unattributed__", "forms": 5, "revenue": 0.0,
              "unattributed": True}]
    out = q.merge_campaign_forms(rows, forms, total_form_revenue=1000.0)
    by = {r["campaign_id"]: r for r in out}
    assert by["A"]["revenue_total"] == 750.0
    assert by["B"]["revenue_total"] == 250.0
    # Largest-remainder allocation: 3.75 / 1.25 → 4 and 1, summing to 5.
    assert by["A"]["forms"] == 4 and by["B"]["forms"] == 1
    assert sum(r["forms"] for r in out) == 5
    assert sum(r["revenue_total"] for r in out) == 1000.0


def test_click_id_placed_revenue_is_not_redistributed():
    """Revenue a click id already placed stays on its own campaign; only the
    unplaced remainder is spread."""
    rows = [
        {"campaign_id": "A", "campaign_name": "A", "revenue": 0.0, "spend": 100.0,
         "clicks": 100, "roas": 0.0, "unattributed": False},
        {"campaign_id": "B", "campaign_name": "B", "revenue": 0.0, "spend": 100.0,
         "clicks": 100, "roas": 0.0, "unattributed": False},
    ]
    forms = [{"campaign_id": "A", "forms": 1, "revenue": 600.0, "unattributed": False}]
    out = q.merge_campaign_forms(rows, forms, total_form_revenue=800.0)
    by = {r["campaign_id"]: r for r in out}
    # A keeps its placed 600 plus half the 200 remainder; B gets the other half.
    assert by["A"]["revenue_total"] == 700.0
    assert by["B"]["revenue_total"] == 100.0
    assert sum(r["revenue_total"] for r in out) == 800.0


def test_no_spending_campaigns_leaves_the_leg_undistributed():
    """Nothing to attribute to → the figure is not invented onto a zero-spend row,
    which would show a ROAS from no spend."""
    out = q.merge_campaign_forms(
        [{"campaign_id": "__unattributed__", "campaign_name": "Unattributed",
          "revenue": 0.0, "spend": 0.0, "clicks": 0, "calls": 5, "roas": 0.0,
          "unattributed": True}],
        None, total_form_revenue=500.0)
    assert all(r.get("roas_total") is None for r in out)


def test_total_form_revenue_omitted_leaves_rows_untouched():
    """Callers not rendering a ROAS table must not gain distributed revenue."""
    out = q.merge_campaign_forms(
        [{"campaign_id": "A", "campaign_name": "Beta A", "revenue": 0.0,
          "spend": 10.0, "clicks": 5, "roas": 0.0, "unattributed": False}], None)
    assert out[0]["revenue_total"] == 0.0


def test_campaign_population_is_every_submission(monkeypatch):
    """No paid-evidence filter on the counted population.

    Restricting the COUNT to click-id-bearing forms while crediting ALL of their
    revenue to the campaigns made two numbers that could not both be right: the
    ads traffic fork read 2 submissions where the Web forms section read 11, for
    the same clinic and window. Forms are taken to be campaign-driven, so the
    populations must match.
    """
    client = _FakeClient([])
    monkeypatch.setattr(q, "_client", lambda: client)
    q.webform_campaign_attribution("C1", ["111"], window=WINDOW)
    sql = client.sql[0]
    # No paid-medium / click-id gate on the forms CTE.
    assert "utm_medium" not in sql
    assert "gbraid" not in sql or "cl.gclid" in sql  # ids only used for placement


def test_pool_harvests_the_readers_own_remainder_row():
    """The remainder row's revenue must be pulled into the pool and spread.

    Once the population became every submission, the reader's own remainder row
    became the DOMINANT source of unplaced revenue — computing the pool only from
    "headline minus what rows carry" then found nothing to distribute and left the
    whole figure parked on that row, with every campaign reading 0.0x.
    """
    rows = [{"campaign_id": "P", "campaign_name": "Beta Princeton", "revenue": 0.0,
             "spend": 5737.0, "clicks": 655, "calls": 345, "roas": 0.0,
             "unattributed": False}]
    forms = [{"campaign_id": "__unattributed__", "forms": 11, "revenue": 7240.0,
              "unattributed": True}]
    out = q.merge_campaign_forms(rows, forms, total_form_revenue=7240.0)
    assert len(out) == 1, "emptied remainder row should be dropped"
    row = out[0]
    assert row["revenue_total"] == 7240.0
    assert row["forms"] == 11
    assert abs(row["roas_total"] - 7240.0 / 5737.0) < 1e-9


def test_pool_combines_both_sources():
    """Remainder-row revenue AND headline revenue no row accounts for are both
    distributed — missing either strands money."""
    rows = [{"campaign_id": "A", "campaign_name": "A", "revenue": 0.0, "spend": 100.0,
             "clicks": 100, "roas": 0.0, "unattributed": False}]
    forms = [{"campaign_id": "__unattributed__", "forms": 2, "revenue": 300.0,
              "unattributed": True}]
    # 300 on the remainder + 200 the headline carries beyond it.
    out = q.merge_campaign_forms(rows, forms, total_form_revenue=500.0)
    assert sum(r["revenue_total"] for r in out) == 500.0
    assert sum(r["forms"] for r in out) == 2


def test_distribution_runs_without_a_headline_total():
    """The group builds these rows WITHOUT `total_form_revenue`, and gating
    distribution on that argument is why its ads table kept an "Unattributed paid
    forms" row holding the whole form leg while the clinic page looked correct.
    The trigger must be whether anything is unplaced."""
    rows = [{"campaign_id": "P", "campaign_name": "Beta Princeton", "revenue": 0.0,
             "spend": 5737.0, "clicks": 655, "calls": 345, "roas": 0.0,
             "unattributed": False}]
    forms = [{"campaign_id": "__unattributed__", "forms": 11, "revenue": 7240.0,
              "unattributed": True}]
    out = q.merge_campaign_forms(rows, forms)          # no total, as the group calls it
    assert not [r for r in out if r.get("unattributed")]
    assert out[0]["forms"] == 11 and out[0]["revenue_total"] == 7240.0
    # And identical to the clinic path, which does pass the total.
    with_total = q.merge_campaign_forms(rows, forms, total_form_revenue=7240.0)
    assert with_total[0]["revenue_total"] == out[0]["revenue_total"]
    assert with_total[0]["forms"] == out[0]["forms"]


def test_zero_spend_campaign_still_absorbs_the_forms():
    """A clinic whose campaigns were paused mid-window still owns the form fills
    its earlier clicks produced. Requiring spend > 0 to be a target left the
    unattributed row on screen for exactly the quietest clinics."""
    out = q.merge_campaign_forms(
        [{"campaign_id": "A", "campaign_name": "Beta A", "revenue": 0.0,
          "spend": 0.0, "clicks": 0, "roas": 0.0, "unattributed": False}],
        [{"campaign_id": "__unattributed__", "forms": 4, "revenue": 900.0,
          "unattributed": True}])
    assert not [r for r in out if r.get("unattributed")]
    assert out[0]["forms"] == 4 and out[0]["revenue_total"] == 900.0
    # No spend, so no ratio may be shown.
    assert out[0]["roas_total"] is None


def test_unattributed_CALLS_row_survives():
    """Only the FORM remainder is dissolved. Paid calls that no gclid or name
    match could place are a different claim — the table footnote promises Calls
    still sums to the headline — so that row keeps its calls."""
    out = q.merge_campaign_forms(
        [{"campaign_id": "A", "campaign_name": "Beta A", "revenue": 100.0,
          "spend": 50.0, "clicks": 10, "calls": 4, "roas": 2.0, "unattributed": False},
         {"campaign_id": "__unattributed__", "campaign_name": "Unattributed paid calls",
          "revenue": 0.0, "spend": 0.0, "clicks": 0, "calls": 7, "roas": 0.0,
          "unattributed": True}],
        [{"campaign_id": "__unattributed__", "forms": 2, "revenue": 300.0,
          "unattributed": True}])
    rem = [r for r in out if r.get("unattributed")]
    assert len(rem) == 1 and rem[0]["calls"] == 7
    # Its form revenue was harvested away, though.
    assert rem[0]["forms"] == 0 and rem[0]["revenue_total"] == 0.0
    assert sum(r["calls"] for r in out) == 11


# ── webform bookings on the ads traffic fork ─────────────────────────────────

def test_webform_appointments_exposes_a_bounded_count(monkeypatch):
    """`appt_submissions_matched` bounds bookings to the call leg's window.

    The unbounded `appt_submissions` counts ANY appointment created after the
    submission, with no upper limit, so it is not comparable to a call-booking
    count and must not be the figure drawn beside one in the traffic fork.
    """
    row = _FakeRow(submissions=16, matched_submissions=15, appt_submissions=13,
                   appt_submissions_matched=12, appt_patients=13)
    client = _FakeClient([row])
    monkeypatch.setattr(q, "_client", lambda: client)
    out = q.webform_appointments("C1", window=WINDOW)
    assert out["appt_submissions"] == 13
    assert out["appt_submissions_matched"] == 12
    # The bound must be in the SQL, keyed off the shared constant.
    assert f"lag_days <= {q.CALL_BOOKING_MATCH_DAYS}" in client.sql[0]


def test_bounded_count_defaults_to_zero_not_missing(monkeypatch):
    """Fail-safe shape: the UI reads the field unconditionally, so it must exist
    even when the query returns nothing."""
    monkeypatch.setattr(q, "_client", lambda: _FakeClient([]))
    out = q.webform_appointments("C1", window=WINDOW)
    assert out["appt_submissions_matched"] == 0


@pytest.mark.skipif(
    not os.environ.get("CORTEX_BQ_VALIDATE"),
    reason="needs live ADC; set CORTEX_BQ_VALIDATE=1 to run",
)
def test_webform_appointments_sql_compiles():
    """The bounded count adds a windowed aggregate over a new lag_days column."""
    from google.cloud import bigquery
    import unittest.mock as mock

    captured = {}

    class _Stop(Exception):
        pass

    class _DryRunClient:
        def query(self, sql, job_config=None):
            captured["sql"] = sql
            captured["params"] = list(job_config.query_parameters) if job_config else []
            raise _Stop

    with mock.patch.object(q, "_client", lambda: _DryRunClient()):
        try:
            q.webform_appointments("C1", window=WINDOW)
        except _Stop:
            pass

    client = bigquery.Client(project="project-demo-2-482101")
    job = client.query(captured["sql"], job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False, query_parameters=captured["params"]))
    assert job.errors is None, job.errors
