""""Revenue attributed" must be the whole of what Cortex drove, counted once.

Two defects are guarded here. The first is under-counting: before the web-form
channel was folded into ``pipeline_revenue_by_source``, the dashboard's headline
credited tracked calls and CounselEar portal referrals only, so a patient who
arrived through a form contributed nothing to the figure that is presented as the
revenue Cortex led to.

The second is the double-count that the obvious fix would have introduced.
``webform_revenue`` credits form patients over the SAME invoices, so adding it to
the old total would have counted every patient who both called and submitted
twice — the defect written up as §5 of
``resources/form-revenue-attribution-plan.md``. First touch across all channels
is what makes the wider population safe, and it is what these assert.

No BigQuery — ``_client`` is stubbed, as in test_ad_campaign_attribution.
"""
from __future__ import annotations

import datetime as dt
import os

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
        self.params: list[list] = []
        self._rows = rows

    def query(self, sql, job_config=None):
        self.sql.append(sql)
        self.params.append(list(job_config.query_parameters) if job_config else [])
        return _FakeJob(self._rows)


def _row(source, channel, revenue, invoices=1, patients=1, medium="Paid"):
    """`medium` is one of TRAFFIC_CHANNELS for EVERY channel — it is a marketing
    dimension orthogonal to the acquisition channel, so there is no per-channel
    default to infer here."""
    return _FakeRow(source=source, channel=channel, medium=medium,
                    revenue=revenue, invoices=invoices, patients=patients)


WINDOW = q.Window("2026-05-01", "2026-08-31")


@pytest.fixture
def run(monkeypatch):
    def _run(rows, cio=None):
        client = _FakeClient(rows)
        monkeypatch.setattr(q, "_client", lambda: client)
        out = q.pipeline_revenue_by_source(
            "C1", ["10009431"], window=WINDOW, customerio_touches=cio)
        return out, client
    return _run


# ── SQL contract ─────────────────────────────────────────────────────────────

def test_all_four_channel_legs_are_unioned(run):
    """The form leg is the one that was missing; the others must not regress."""
    _, c = run([])
    sql = c.sql[0]
    for cte in ("call_touch AS (", "form_touch AS (", "portal_touch AS (",
                "customerio_touch AS ("):
        assert cte in sql, cte
    union = sql.split("touches AS (", 1)[1].split(")", 1)[0]
    for leg in ("call_touch", "form_touch", "portal_touch", "customerio_touch"):
        assert leg in union, leg


def test_form_leg_reuses_the_webform_revenue_matching_rule(run):
    """Phone last-10 OR email, or the two readers disagree on who is a form lead."""
    _, c = run([])
    leg = c.sql[0].split("form_touch AS (", 1)[1].split("),", 1)[0]
    assert "LENGTH(f.phone_norm) = 10" in leg
    assert "f.email_norm != ''" in leg
    assert " OR " in leg


def test_first_touch_picks_one_source_and_channel_per_patient(run):
    _, c = run([])
    ft = c.sql[0].split("first_touch AS (", 1)[1].split("),", 1)[0]
    # LIMIT 1 over a timestamp-ordered ARRAY_AGG is what makes both breakdowns
    # partitions instead of overlapping tallies.
    assert "LIMIT 1" in ft
    assert "GROUP BY client_id" in ft
    assert "STRUCT(source, channel, medium)" in ft
    # Full timestamp, not the date: same-day ties land on exactly the patients
    # most likely to have both a call and a form.
    assert "ORDER BY touch_ts, channel, source" in ft


def test_portal_leg_is_ordered_at_midnight(run):
    """appt_date has no clock, so it must lose same-day ties to a real timestamp."""
    _, c = run([])
    leg = c.sql[0].split("portal_touch AS (", 1)[1].split("),", 1)[0]
    assert "TIMESTAMP(appt_date)" in leg


def test_referrer_host_never_becomes_a_campaign_SOURCE(run):
    """Contract §14a: a referrer is not a campaign source. Presenting one as the
    other is the mislabel the ingest split was built to end.

    The MEDIUM dimension may consult it — a search-engine referrer is real
    evidence of organic arrival — but the source label must stay utm_source only,
    or `by_source` starts listing hostnames as if they were campaigns. The two
    dimensions are computed in the same CTE, so this is worth pinning.
    """
    _, c = run([])
    leg = c.sql[0].split("form_touch AS (", 1)[1].split("portal_touch AS (", 1)[0]
    source_expr = leg.split("AS source", 1)[0]
    assert "referrer_host" not in source_expr
    assert "fref" not in source_expr
    # And the source projection in form_src reads utm_source, nothing else.
    src = c.sql[0].split("form_src AS (", 1)[1].split("AS source", 1)[0]
    assert "referrer_host" not in src.split("utm_source", 1)[1]


def test_invoices_are_deduped_per_order(run):
    _, c = run([])
    inv = c.sql[0].split("inv AS (", 1)[1]
    assert "MAX(SAFE_CAST(im.order_total_with_tax AS NUMERIC))" in inv
    assert "GROUP BY im.order_id" in inv


def test_both_breakdowns_come_from_one_grouping(run):
    """Querying them separately would let the two splits disagree on the total."""
    _, c = run([])
    assert "GROUP BY source, channel" in c.sql[0]
    assert len(c.sql) == 1


# ── Customer.io seam ─────────────────────────────────────────────────────────

def test_customerio_leg_is_typed_empty_when_unfed(run):
    """An empty typed ARRAY, not `SELECT NULL … WHERE FALSE`.

    That earlier shape shipped and was invalid SQL — BigQuery rejects a WHERE
    clause on a query with no FROM — so the whole reader failed safe to zeros and
    the dashboard tile read $0 for every clinic. These string assertions could
    never have caught it; see test_sql_compiles_against_bigquery for the check
    that does.
    """
    _, c = run([])
    leg = c.sql[0].split("customerio_touch AS (", 1)[1].split("),", 1)[0]
    assert "FROM UNNEST(ARRAY<STRUCT<client_id STRING, touch_ts TIMESTAMP>>[])" in leg
    assert "WHERE" not in leg.upper()
    assert not any(p.name == "cio_touches" for p in c.params[0])


def test_customerio_touches_are_bound_as_a_struct_array(run):
    _, c = run([], cio=[("P1", dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.timezone.utc))])
    leg = c.sql[0].split("customerio_touch AS (", 1)[1].split("),", 1)[0]
    assert "UNNEST(@cio_touches)" in leg
    assert "WHERE FALSE" not in leg
    param = next(p for p in c.params[0] if p.name == "cio_touches")
    assert param.to_api_repr()["parameterType"]["arrayType"]["type"] == "STRUCT"


def test_every_channel_is_reported_even_at_zero(run):
    """Zero means "earned nothing"; absent would read as "not configured"."""
    out, _ = run([_row("google", "call", 100.0)])
    by_chan = {c["channel"]: c for c in out["by_channel"]}
    assert set(q.REVENUE_CHANNELS) <= set(by_chan)
    assert by_chan["customerio"]["revenue"] == 0.0
    assert by_chan["form"]["revenue"] == 0.0


# ── The headline figure ──────────────────────────────────────────────────────

def test_form_revenue_is_counted_in_the_total(run):
    """The whole point: a form-acquired patient must reach the headline."""
    out, _ = run([_row("direct / untagged", "form", 4200.0, 2, 1)])
    assert out["revenue"] == 4200.0
    by_chan = {c["channel"]: c["revenue"] for c in out["by_channel"]}
    assert by_chan["form"] == 4200.0


def test_medium_uses_the_canonical_traffic_channel_vocabulary(run):
    """Reuse, not a second taxonomy: `channel_mix` already buckets call traffic
    into Paid/Organic/Direct/Referral/Social, and a revenue chart that invented
    its own would describe the same traffic differently on the same page."""
    _, c = run([])
    # Split on the NEXT CTE, not on "),": the medium CASE contains that sequence
    # itself, which truncated the slice before the thing under test.
    leg = c.sql[0].split("call_touch AS (", 1)[1].split("form_src AS (", 1)[0]
    assert "AS medium" in leg
    # The CASE from _channel_case_sql, not a raw utm_medium GROUP BY — which would
    # split cpc / paid / 'paid search' into three slices for one medium.
    assert "'Paid'" in leg and "'Organic'" in leg and "'Social'" in leg
    for m in q._PAID_MEDIUMS:
        assert f"'{m}'" in leg, m


def test_medium_only_ever_holds_canonical_mediums(run):
    """Channel and medium are orthogonal. "Web form" is not a medium, and a form
    IS allowed a real one — so the medium split must never carry a channel name."""
    out, _ = run([
        _row("google", "form", 4200.0, medium="Paid"),
        _row("counselear portal referral", "portal", 1000.0, medium="No data"),
    ])
    meds = {m["medium"]: m["revenue"] for m in out["by_medium"]}
    assert meds == {"Paid": 4200.0, "No data": 1000.0}
    assert not ({"Web form", "Portal referral", "Customer.io"} & set(meds))


def test_form_medium_reads_click_ids_before_utm(run):
    """utm_medium is populated ZERO times across all 292 submissions, so a
    utm-first rule would file every form under 'No data' permanently. The click
    ids — including gad_campaignid, which resolved for 13/13 — are the only paid
    evidence a form carries."""
    _, c = run([])
    leg = c.sql[0].split("form_touch AS (", 1)[1].split("portal_touch AS (", 1)[0]
    paid = leg.index("'Paid'")
    for col in ("f.gclid", "f.gbraid", "f.wbraid", "f.gad_campaignid"):
        assert col in leg[:paid], col
    # The referrer fallback must sit AFTER every click-id and UTM branch, or a
    # google.com referrer would be labelled Organic on a submission that carried
    # real paid evidence.
    assert leg.index("f.fref") > paid
    assert leg.index("f.fref") > leg.index("f.fum")


def test_referrer_host_fallback_splits_search_direct_and_referral(run):
    """Accepted imprecision: a google.com referrer may be paid with a dropped
    click id. Bounded by ordering — anything with real paid evidence never reaches
    this branch. Without it, ~89 referrer-bearing submissions sit in 'No data'."""
    _, c = run([])
    leg = c.sql[0].split("form_touch AS (", 1)[1].split("portal_touch AS (", 1)[0]
    assert "REGEXP_CONTAINS(f.fref" in leg
    assert "'Organic'" in leg and "'Direct'" in leg and "'Referral'" in leg
    # 'direct' is the ingest's no-referrer sentinel, not a hostname.
    assert "f.fref = 'direct'" in leg


def test_all_three_breakdowns_partition_the_same_total(run):
    out, _ = run([
        _row("google", "call", 600.0, 3, 2, medium="Paid"),
        _row("bing", "call", 100.0, 1, 1, medium="Organic"),
        _row("direct / untagged", "form", 400.0, 2, 1),
    ])
    total = out["revenue"]
    assert total == 1100.0
    for key in ("by_source", "by_medium", "by_channel"):
        assert sum(s["revenue"] for s in out[key]) == total, key


def test_both_breakdowns_partition_the_same_total(run):
    out, _ = run([
        _row("google", "call", 600.0, 3, 2),
        _row("google", "form", 400.0, 2, 1),
        _row("counselear portal referral", "portal", 250.0, 1, 1),
    ])
    assert out["revenue"] == 1250.0
    # The identity both charts are drawn on.
    assert sum(s["revenue"] for s in out["by_source"]) == out["revenue"]
    assert sum(s["revenue"] for s in out["by_channel"]) == out["revenue"]
    assert out["invoices"] == 6
    assert out["patients"] == 4


def test_one_source_spanning_two_channels_folds_into_one_slice(run):
    """by_source must not split "google" in two just because two channels used it."""
    out, _ = run([
        _row("google", "call", 600.0, 3, 2),
        _row("google", "form", 400.0, 2, 1),
    ])
    assert [s["source"] for s in out["by_source"]] == ["google"]
    assert out["by_source"][0]["revenue"] == 1000.0
    assert out["by_source"][0]["invoices"] == 5


def test_slices_are_ordered_by_revenue_desc(run):
    out, _ = run([
        _row("bing", "call", 10.0),
        _row("google", "call", 900.0),
    ])
    assert [s["source"] for s in out["by_source"]] == ["google", "bing"]
    assert out["by_channel"][0]["channel"] == "call"


def test_unrecognised_channel_is_kept_not_dropped(run):
    """Dropping it would silently break the sums-to-headline promise."""
    out, _ = run([_row("sms blast", "sms", 42.0)])
    assert out["revenue"] == 42.0
    assert any(c["channel"] == "sms" and c["revenue"] == 42.0 for c in out["by_channel"])


def test_query_failure_returns_a_zeroed_shape(monkeypatch):
    class _Boom:
        def query(self, sql, job_config=None):
            raise RuntimeError("no such table")
    monkeypatch.setattr(q, "_client", lambda: _Boom())
    out = q.pipeline_revenue_by_source("C1", ["1"], window=WINDOW)
    assert out["revenue"] == 0.0
    assert out["by_source"] == []
    # Still shaped, so a caller can render the channel list without a None guard.
    assert {c["channel"] for c in out["by_channel"]} == set(q.REVENUE_CHANNELS)


def test_window_before_the_pinned_start_spends_nothing(monkeypatch):
    client = _FakeClient([])
    monkeypatch.setattr(q, "_client", lambda: client)
    out = q.pipeline_revenue_by_source(
        "C1", ["1"], window=q.Window("2025-01-01", "2025-02-01"))
    assert out["revenue"] == 0.0
    assert client.sql == []


# ── Does it actually compile? ────────────────────────────────────────────────

@pytest.mark.skipif(
    not os.environ.get("CORTEX_BQ_VALIDATE"),
    reason="needs live ADC; set CORTEX_BQ_VALIDATE=1 to run",
)
@pytest.mark.parametrize("cio", [
    None,
    [("P1", dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.timezone.utc))],
])
def test_sql_compiles_against_bigquery(cio):
    """Dry-run the generated SQL — free, scans nothing, and catches the class of
    bug the string assertions above cannot: SQL that is well-formed as TEXT and
    rejected by the engine.

    BOTH branches of the Customer.io seam are parametrised, because the unfed one
    is the branch that shipped broken and it is the branch that runs in production
    today.
    """
    from google.cloud import bigquery

    captured = {}

    class _DryRunClient:
        def query(self, sql, job_config=None):
            captured["sql"] = sql
            captured["params"] = list(job_config.query_parameters) if job_config else []
            raise _Stop

    class _Stop(Exception):
        pass

    import unittest.mock as mock
    with mock.patch.object(q, "_client", lambda: _DryRunClient()):
        q.pipeline_revenue_by_source("C1", ["1"], window=WINDOW, customerio_touches=cio)

    client = bigquery.Client(project="project-demo-2-482101")
    job = client.query(captured["sql"], job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False, query_parameters=captured["params"]))
    assert job.errors is None, job.errors
