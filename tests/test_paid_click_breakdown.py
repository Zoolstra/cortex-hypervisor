"""Ads tab geography / keyword breakdown (``queries.paid_click_breakdown``).

No BigQuery — ``_client`` is stubbed. These pin the SQL contract (same Paid
population and click match as ``ad_click_attribution``, one click row per call,
click set not window-bounded) and the row shaping: canonical geo names split
into name / region / country, NULL keys collapse into the ``unknown`` counters,
and the partition identities the pie chart depends on hold.
"""
from __future__ import annotations

from intelligence_report import queries as q

WINDOW = q.Window("2026-06-01", "2026-08-31")


class _Row:
    def __init__(self, dim, key=None, label=None, sub=None, lat=None, lon=None, calls=0):
        self.dim, self.key, self.label, self.sub = dim, key, label, sub
        self.lat, self.lon, self.calls = lat, lon, calls


class _FakeClient:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def query(self, sql, job_config=None):
        self.sql.append(sql)
        rows = self.rows

        class _Job:
            def result(self):
                return rows
        return _Job()


def _run(monkeypatch, rows, iv=("1",), ga=("2", "3")):
    client = _FakeClient(rows)
    monkeypatch.setattr(q, "_client", lambda: client)
    out = q.paid_click_breakdown("C1", list(iv), list(ga), window=WINDOW)
    return out, (client.sql[0] if client.sql else "")


def test_empty_invoca_scope_returns_zero_shell_without_querying(monkeypatch):
    out, sql = _run(monkeypatch, [], iv=())
    assert sql == ""
    assert out["paid_calls"] == 0 and out["geo"]["places"] == [] and out["keywords"]["keywords"] == []


def test_sql_contract(monkeypatch):
    _, sql = _run(monkeypatch, [])
    # Same Paid classifier + dedupe per call as ad_click_attribution.
    assert "channel = 'Paid'" in sql
    assert "PARTITION BY t.complete_call_id ORDER BY t.timestamp DESC" in sql
    # Click join scoped to the linked campaigns, one click row per call.
    assert "ac.google_ads_campaign_id IN ('2', '3')" in sql
    assert "PARTITION BY p.complete_call_id" in sql
    # The click side is NOT window-bounded (a click just before the window
    # still resolves its call) — only the transactions side carries the window.
    matched = sql.split("matched AS (", 1)[1].split("placed AS (", 1)[0]
    assert "ac.timestamp >=" not in matched and "ac.timestamp <" not in matched
    # Geo resolves city first, then region; keywords treat '' / 'nan' as absent.
    assert "geo_targets` gc ON gc.criterion_id = m.city_id" in sql
    assert "geo_targets` gr ON gr.criterion_id = m.region_id" in sql
    assert "NULLIF(NULLIF(TRIM(ac.click_view_keyword_info_text), ''), 'nan')" in sql


def test_no_linked_google_ads_campaigns_matches_nothing(monkeypatch):
    _, sql = _run(monkeypatch, [], ga=())
    assert "AND FALSE" in sql
    assert "IN ()" not in sql


def test_row_shaping_and_partition_identities(monkeypatch):
    rows = [
        _Row("total", calls=40),
        _Row("matched", calls=10),
        _Row("geo", "1001801", "Calgary,Alberta,Canada", lat=51.05, lon=-114.09, calls=6),
        _Row("geo", "9000", "Hamilton,Hamilton,Ontario,Canada", calls=2),
        _Row("geo", "20113", "Alberta,Canada", calls=1),   # region-only fallback
        _Row("geo", None, None, calls=1),                  # no geo on the click
        _Row("kw", "hearing test", "hearing test", "PHRASE", calls=7),
        _Row("kw", "audiologist", "audiologist", "BROAD", calls=2),
        _Row("kw", None, None, None, calls=1),
    ]
    out, _ = _run(monkeypatch, rows)
    assert (out["paid_calls"], out["with_click_data"], out["no_click_data"]) == (40, 10, 30)

    places = out["geo"]["places"]
    assert [p["name"] for p in places] == ["Calgary", "Hamilton", "Alberta"]  # by calls desc
    calgary, hamilton, alberta = places
    assert calgary == {"id": "1001801", "name": "Calgary", "region": "Alberta",
                       "country": "Canada", "lat": 51.05, "lon": -114.09, "calls": 6}
    # Four-part canonical names ("City,County,Province,Country") still split right.
    assert (hamilton["region"], hamilton["country"]) == ("Ontario", "Canada")
    # A region-only target has no region above it and no coordinate.
    assert alberta["region"] is None and alberta["country"] == "Canada" and alberta["lat"] is None
    assert out["geo"]["unknown"] == 1
    assert sum(p["calls"] for p in places) + out["geo"]["unknown"] == out["with_click_data"]

    kws = out["keywords"]["keywords"]
    assert kws == [{"keyword": "hearing test", "match_type": "PHRASE", "calls": 7},
                   {"keyword": "audiologist", "match_type": "BROAD", "calls": 2}]
    assert out["keywords"]["unknown"] == 1
    assert sum(k["calls"] for k in kws) + out["keywords"]["unknown"] == out["with_click_data"]
