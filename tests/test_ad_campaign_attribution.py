"""§04 per-campaign ad rows must reconcile with the §04 headline.

The bug these guard against: ``google_ads_roi`` attributed paid calls to a
Google Ads campaign by normalized campaign name ONLY, and inner-joined the match
away. For a clinic whose Invoca campaigns are named per location while its Ads
campaigns are named anything else (Hope Hearing: Invoca "Southlake" vs Ads "Hope
Hearing TX PPC 8000") nothing matched, so the table read 0 calls / 0 booked for
every campaign while the headline read 2 paid calls / 1 booked.

No BigQuery here — ``_client`` is stubbed, so these assert the SQL contract
(gclid tier present, cascade order, no call dropped, remainder row emitted) and
the Python row-shaping around the sentinel key.
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
    """Records every SQL string and replays canned rows in order."""

    def __init__(self, row_batches):
        self.sql: list[str] = []
        self._batches = list(row_batches)

    def query(self, sql, job_config=None):
        self.sql.append(sql)
        return _FakeJob(self._batches.pop(0) if self._batches else [])


def _cte(sql: str, name: str) -> str:
    """Body of a named CTE, matched on balanced parentheses.

    Splitting on the first ")," is not enough: the cascade's own COALESCE
    contains nested calls (``CAST(NULL AS STRING),``, ``IF(..., NULL),``) that
    end in exactly that sequence, so a naive split silently truncates the text
    the assertion then searches — and a truncated haystack fails as "cascade
    reordered" no matter what the cascade actually says.
    """
    body = sql.split(f"{name} AS (", 1)[1]
    depth = 0
    for i, ch in enumerate(body):
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return body[:i]
            depth -= 1
    raise AssertionError(f"unbalanced parentheses in CTE {name}")


def _row(campaign_id, **kw):
    base = dict(campaign_name=None, clicks=0, calls=0, booked=0,
                spend=0.0, revenue=0.0, invoice_count=0)
    base.update(kw)
    return _FakeRow(campaign_id=campaign_id, **base)


@pytest.fixture
def run(monkeypatch):
    """Call google_ads_roi with canned rows; hand back (rows, sql)."""
    def _run(rows, ga=("111", "222"), iv=("10009431",)):
        client = _FakeClient([rows])
        monkeypatch.setattr(q, "_client", lambda: client)
        out = q.google_ads_roi("C1", list(ga), list(iv), window=q.Window("2026-07-01", "2026-07-31"))
        return out, client.sql[0]
    return _run


# ── SQL contract ─────────────────────────────────────────────────────────────

def test_gclid_tier_is_present_and_scoped_to_linked_campaigns(run):
    _, sql = run([])
    assert "gclid_clicks AS (" in sql
    assert "click_view_gclid AS gclid" in sql
    # Restricted to the clinic's linked campaigns — a click from an unlinked
    # campaign must fall through to the name match, not invent a row.
    gclid_cte = sql.split("gclid_clicks AS (", 1)[1].split("),", 1)[0]
    assert "google_ads_campaign_id IN ('111', '222')" in gclid_cte
    assert "NOT IN ('nan', '')" in gclid_cte


def test_attribution_cascade_prefers_gclid_then_name_then_remainder(run):
    _, sql = run([])
    cascade = _cte(sql, "call_camp")
    # COALESCE order IS the cascade: gclid wins, then the name map, then the
    # sentinel. Reordering these silently changes attribution.
    gk = cascade.index("gk.google_ads_campaign_id")
    gn = cascade.index("gn.google_ads_campaign_id")
    un = cascade.index(q.UNATTRIBUTED_CAMPAIGN_ID)
    assert gk < gn < un
    # Both tiers must be LEFT joins — an inner join is what dropped the calls.
    assert "LEFT JOIN gclid_clicks gk" in cascade
    assert "LEFT JOIN ga_name_map gn" in cascade
    assert "JOIN ga_norm g ON" not in cascade


def test_sole_linked_campaign_claims_calls_the_name_match_missed(run):
    """Tier 3: one linked campaign leaves nothing to disambiguate.

    Guards the CHAA "Sunterra Day Beta" failure — an Ads name carrying "Beta" as
    a TRAILING token normalizes to "sunterra day beta", never matches Invoca's
    "sunterra", and dropped 143 of 145 paid calls into the remainder row.
    """
    _, sql = run([], ga=("111",))
    cascade = _cte(sql, "call_camp")
    gn = cascade.index("gn.google_ads_campaign_id")
    sole = cascade.index("'111'")
    un = cascade.index(q.UNATTRIBUTED_CAMPAIGN_ID)
    # Strictly LAST before the sentinel: it is the weakest evidence in the
    # cascade and must never pre-empt a gclid or a name match.
    assert gn < sole < un
    # Bing/Meta calls are excluded — 'Paid' is not Google-only, and crediting
    # them here would overstate the sole campaign's calls and its ROAS.
    assert "other_network_click" in cascade


def test_sole_tier_is_inert_when_several_campaigns_are_linked(run):
    """With 2+ campaigns, "which one" is the question the earlier tiers answer.

    Guessing would invent a split, so the tier must contribute nothing — and it
    must not smuggle a campaign id into the COALESCE.
    """
    _, sql = run([], ga=("111", "222"))
    cascade = _cte(sql, "call_camp")
    gn = cascade.index("gn.google_ads_campaign_id")
    un = cascade.index(q.UNATTRIBUTED_CAMPAIGN_ID)
    between = cascade[gn:un]
    assert "CAST(NULL AS STRING)" in between
    assert "'111'" not in between and "'222'" not in between


def test_duplicate_campaign_names_resolve_to_one_campaign(run):
    """Two linked campaigns can normalize to the same name; crediting a call to
    both would double-count and break the sum-to-headline property."""
    _, sql = run([])
    name_map = sql.split("ga_name_map AS (", 1)[1].split("),", 1)[0]
    assert "MIN(google_ads_campaign_id)" in name_map
    assert "GROUP BY norm" in name_map


def test_remainder_row_is_suppressed_when_nothing_is_unattributed(run):
    _, sql = run([])
    base = sql.split("base AS (", 1)[1].split("\n        )", 1)[0]
    assert "UNION ALL" in base
    assert "WHERE n > 0" in base          # no empty remainder row
    assert "FROM base g" in sql            # final SELECT drives off base, not ga_norm


def test_no_linked_google_ads_campaigns_short_circuits(monkeypatch):
    called = []
    monkeypatch.setattr(q, "_client", lambda: called.append(1))
    assert q.google_ads_roi("C1", [], ["10009431"]) == []
    assert not called


def test_no_linked_invoca_campaigns_scopes_calls_to_nothing(run):
    """Clicks and spend still report; the call columns can't (no call source),
    and there must be no remainder row to imply otherwise."""
    rows, sql = run([_row("111", campaign_name="X", clicks=10, spend=50.0)], iv=())
    assert "WHERE FALSE" in sql
    assert [r["campaign_id"] for r in rows] == ["111"]
    assert rows[0]["clicks"] == 10 and rows[0]["calls"] == 0


# ── Row shaping ──────────────────────────────────────────────────────────────

def test_remainder_row_is_labelled_and_flagged(run):
    rows, _ = run([
        _row("111", campaign_name="Hope Hearing TX PPC 8000", clicks=296,
             calls=1, booked=1, spend=2065.84),
        _row("222", campaign_name="HH YouTube :15 Unskippable 4200", clicks=71, spend=1262.45),
        _row(q.UNATTRIBUTED_CAMPAIGN_ID, calls=1),
    ])
    by_id = {r["campaign_id"]: r for r in rows}
    rem = by_id[q.UNATTRIBUTED_CAMPAIGN_ID]
    assert rem["unattributed"] is True
    assert rem["campaign_name"] == q.UNATTRIBUTED_CAMPAIGN_NAME
    assert rem["spend"] == 0 and rem["clicks"] == 0
    # Real campaigns keep their own name and are not flagged.
    assert by_id["111"]["unattributed"] is False
    assert by_id["111"]["campaign_name"] == "Hope Hearing TX PPC 8000"


def test_rows_sum_to_the_headline_population(run):
    """The whole point: with the remainder row included, calls and booked add up
    to what paid_call_revenue reports (2 paid calls, 1 booked for Hope Hearing)."""
    rows, _ = run([
        _row("111", campaign_name="A", calls=1, booked=1),
        _row("222", campaign_name="B"),
        _row(q.UNATTRIBUTED_CAMPAIGN_ID, calls=1),
    ])
    assert sum(r["calls"] for r in rows) == 2
    assert sum(r["booked"] for r in rows) == 1


def test_unnamed_real_campaign_falls_back_to_its_id(run):
    rows, _ = run([_row("111", campaign_name=None, clicks=5)])
    assert rows[0]["campaign_name"] == "111"
    assert rows[0]["unattributed"] is False
