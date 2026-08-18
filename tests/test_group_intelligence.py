"""
Tests for the multi-location "Group Intelligence" surface (Virsono).

The group route now serves the PER-CLINIC Overview aggregated across an
instance's clinics, so the tests that matter are the merge rules — the places
where the obvious implementation (sum everything) produces a plausible but wrong
number:

  * rates/averages recomputed from summed components, never averaged;
  * per-month series aligned on the month KEY, since clinics don't all cover the
    same months;
  * campaigns merged on campaign_id with every derived field recomputed;
  * the payload keeping the same SHAPE as the clinic payload, which is what lets
    the two share React components.

Plus the endpoint wiring: the multi_location_group capability gate and skip_llm.

All BigQuery / LLM work is stubbed — no external calls.
"""
import pytest
from fastapi.testclient import TestClient

from api import app
from api.deps import verify_token
from api.core.db import get_session
from intelligence_report import group_aggregate as ga
from intelligence_report import payloads
from intelligence_report.queries import Window


# ── the booking match window is one definition, in three files ───────────────

def test_booking_match_window_agrees_across_every_surface():
    """`match_days` is THE definition of "booked". It is declared three times —
    the v1 readers, the mart layer, and the client data feed — because the mart
    and feed deliberately avoid importing the BigQuery module. If they drift,
    the same clinic reports different booked counts on different surfaces and
    the parity harness diverges, which is exactly the kind of bug that gets
    argued about for a week before anyone checks the constants.
    """
    from api import datafeed
    from api.v2 import marts
    from intelligence_report import queries

    assert queries.CALL_BOOKING_MATCH_DAYS == 10
    assert marts.CALL_BOOKING_MATCH_DAYS == queries.CALL_BOOKING_MATCH_DAYS
    assert datafeed._MATCH_DAYS == queries.CALL_BOOKING_MATCH_DAYS


def test_no_reader_hardcodes_its_own_match_window():
    """Every reader defaults to the shared constant rather than a literal, so
    widening the window is a one-line change and cannot half-apply."""
    import inspect
    from intelligence_report import queries
    src = inspect.getsource(queries)
    assert "match_days: int = 3" not in src
    assert "match_days: int = 10" not in src, \
        "use CALL_BOOKING_MATCH_DAYS, not a literal"


# ── rates are recomputed, never averaged ─────────────────────────────────────

def test_booked_rate_is_volume_weighted_not_averaged():
    """The core aggregation trap.

    A 400-call clinic booking 50% and a 10-call clinic booking 10% is a group
    rate of 201/410 = 49%, NOT the 30% mean of the two rates. Averaging would
    let a tiny location drag the group's headline by as much as a huge one.
    """
    big = {"connected": 400, "booked": 200}
    small = {"connected": 10, "booked": 1}
    merged = ga._merge_call_funnel([big, small], ["counselear", "counselear"])
    assert merged["connected"] == 410
    assert merged["booked"] == 201
    assert merged["booked_rate"] == pytest.approx(201 / 410)
    assert merged["booked_rate"] != pytest.approx(((200 / 400) + (1 / 10)) / 2)


def test_capture_rate_recomputed_per_trend_month():
    t1 = [{"month": "2026-01", "calls": 100, "forms": 5, "contacts": 105,
           "connected": 80, "booked": 40, "revenue": 1000.0, "capture_rate": 0.5}]
    t2 = [{"month": "2026-01", "calls": 10, "forms": 0, "contacts": 10,
           "connected": 5, "booked": 1, "revenue": 50.0, "capture_rate": 0.2}]
    (row,) = ga._merge_trend([t1, t2])
    assert row["connected"] == 85 and row["booked"] == 41
    assert row["capture_rate"] == pytest.approx(41 / 85)
    assert row["capture_rate"] != pytest.approx(0.35)      # the naive mean


def test_front_desk_capture_rate_recomputed():
    merged = ga._merge_front_desk([
        {"total": 100, "connected": 60, "returned": 10, "captured": 70,
         "capture_rate": 0.7},
        {"total": 10, "connected": 2, "returned": 0, "captured": 2,
         "capture_rate": 0.2},
    ])
    assert merged["total"] == 110 and merged["captured"] == 72
    assert merged["capture_rate"] == pytest.approx(72 / 110)


def test_empty_denominator_yields_none_not_zero():
    """None and 0.0 mean different things: 'no data' vs 'genuinely nothing
    converted'. A 0.0 here would render as a real 0% on the page."""
    assert ga._ratio(0, 0) is None
    merged = ga._merge_call_funnel([{"connected": 0, "booked": 0}], ["none"])
    assert merged["booked_rate"] is None


# ── per-month series ─────────────────────────────────────────────────────────

def test_months_align_on_key_when_clinics_cover_different_ranges():
    """A location onboarded mid-window has no row for the earlier months.
    Grouping on the month key keeps everything aligned; zipping by index would
    shift one clinic's data into another clinic's months."""
    early = [{"month": "2026-01", "booked": 5, "connected": 8},
             {"month": "2026-02", "booked": 3, "connected": 4}]
    late = [{"month": "2026-02", "booked": 7, "connected": 9}]
    rows = ga._merge_monthly([early, late], int_fields=("booked", "connected"))
    assert [r["month"] for r in rows] == ["2026-01", "2026-02"]
    assert rows[0]["booked"] == 5                     # only the early clinic
    assert rows[1]["booked"] == 10                    # both


def test_monthly_merge_is_sorted_chronologically():
    a = [{"month": "2026-03", "booked": 1}]
    b = [{"month": "2026-01", "booked": 1}]
    assert [r["month"] for r in ga._merge_monthly([a, b], int_fields=("booked",))] \
        == ["2026-01", "2026-03"]


# ── ad campaigns ─────────────────────────────────────────────────────────────

def _campaign(cid, spend, clicks, calls, booked, revenue):
    return {"campaign_id": cid, "campaign_name": "Brand", "spend": spend,
            "clicks": clicks, "calls": calls, "booked": booked,
            "revenue": revenue, "invoice_count": 1,
            # Stale per-clinic derived values that MUST be overwritten.
            "roas": 99.0, "cpc": 99.0, "cost_per_call": 99.0,
            "cost_per_booking": 99.0, "revenue_per_booking": 99.0,
            "click_to_call_pct": 99.0, "call_to_book_pct": 99.0}


def test_shared_campaign_merges_and_recomputes_every_derived_field():
    """One Google Ads campaign can serve several locations, so rows merge on
    campaign_id rather than concatenating. Every ratio is recomputed — carrying
    one clinic's value beside the group's counts would misdescribe the row."""
    a = [_campaign("c1", 200.0, 100, 10, 2, 1000.0)]
    b = [_campaign("c1", 100.0, 50, 5, 1, 200.0)]
    (row,) = ga._merge_ad_campaigns([a, b])
    assert row["spend"] == 300.0 and row["clicks"] == 150
    assert row["calls"] == 15 and row["booked"] == 3 and row["revenue"] == 1200.0
    assert row["roas"] == pytest.approx(1200.0 / 300.0)
    assert row["cpc"] == pytest.approx(300.0 / 150)
    assert row["cost_per_call"] == pytest.approx(300.0 / 15)
    assert row["cost_per_booking"] == pytest.approx(300.0 / 3)
    assert row["revenue_per_booking"] == pytest.approx(1200.0 / 3)
    assert row["click_to_call_pct"] == pytest.approx(15 / 150)
    assert row["call_to_book_pct"] == pytest.approx(3 / 15)


def test_unattributed_remainder_rows_merge_into_one_and_sort_last():
    rows = ga._merge_ad_campaigns([
        [{"campaign_id": None, "campaign_name": "Unattributed paid calls",
          "unattributed": True, "spend": 0.0, "clicks": 0, "calls": 4,
          "booked": 1, "revenue": 0.0, "invoice_count": 0},
         _campaign("c1", 50.0, 10, 1, 0, 0.0)],
        [{"campaign_id": None, "campaign_name": "Unattributed paid calls",
          "unattributed": True, "spend": 0.0, "clicks": 0, "calls": 6,
          "booked": 2, "revenue": 0.0, "invoice_count": 0}],
    ])
    assert rows[-1]["unattributed"] is True
    assert rows[-1]["calls"] == 10 and rows[-1]["booked"] == 3


# ── funnel metadata ──────────────────────────────────────────────────────────

def test_pms_type_is_mixed_when_clinics_differ():
    """The UI names the system a booking was reconciled against; naming one of
    several would be wrong."""
    m = ga._merge_call_funnel([{"connected": 1, "booked": 1}] * 2,
                              ["counselear", "blueprint"])
    assert m["pms_type"] == "mixed"


def test_pms_type_is_the_single_system_when_uniform():
    m = ga._merge_call_funnel([{"connected": 1, "booked": 1}] * 2,
                              ["counselear", "counselear"])
    assert m["pms_type"] == "counselear"


def test_call_funnel_none_when_no_clinic_returned_one():
    assert ga._merge_call_funnel([None, None], ["none"]) is None


# ── leakage ──────────────────────────────────────────────────────────────────

def test_leakage_total_reconciles_with_the_numbers_shown_beside_it():
    """The page shows lost_contacts and avg_invoice next to the total, so a
    reader must be able to multiply them and get it back."""
    leak = ga._merge_leakage(
        [{"components": {"missed_calls": 10, "no_shows": 2,
                         "tested_not_sold": 1, "slow_form_followup": 0}},
         {"components": {"missed_calls": 3, "no_shows": 1,
                         "tested_not_sold": 0, "slow_form_followup": 2}}],
        revenue=10000.0, invoice_count=25)
    assert leak["components"]["missed_calls"] == 13
    assert leak["lost_contacts"] == 19
    assert leak["avg_invoice"] == pytest.approx(400.0)
    assert leak["estimated_leakage"] == pytest.approx(19 * 400.0)


# ── headline YoY ─────────────────────────────────────────────────────────────

def test_yoy_deltas_are_recomputed_from_group_totals():
    """Percentage changes cannot be averaged: the mean of two clinics' growth
    rates is not the group's growth rate."""
    def _y(cur_contacts, prior_contacts, cur_rev, prior_rev):
        return {"basis": "yoy",
                "current": {"contacts": cur_contacts, "calls": cur_contacts,
                            "forms": 0, "connected": cur_contacts, "booked": 0,
                            "revenue": cur_rev, "capture_rate": 0.0,
                            "form_rate": None},
                "prior": {"contacts": prior_contacts, "calls": prior_contacts,
                          "forms": 0, "connected": prior_contacts, "booked": 0,
                          "revenue": prior_rev, "capture_rate": 0.0,
                          "form_rate": None},
                "deltas": {"contacts": 9.0, "revenue": 9.0, "capture_rate": 9.0},
                "window": {"start": "2026-01-01", "end": "2026-06-30"},
                "prior_window": {"start": "2025-01-01", "end": "2025-06-30"}}

    merged = ga._merge_headline_yoy([_y(150, 100, 1500.0, 1000.0),
                                     _y(50, 100, 500.0, 1000.0)])
    assert merged["current"]["contacts"] == 200
    assert merged["prior"]["contacts"] == 200
    # Group is flat even though one clinic is +50% and the other -50%.
    assert merged["deltas"]["contacts"] == pytest.approx(0.0)
    assert merged["deltas"]["revenue"] == pytest.approx(0.0)


def test_yoy_basis_is_mixed_when_clinics_disagree():
    """One clinic with a year of history and one without aren't comparing the
    same thing; the label has to say so rather than pick a side."""
    def _y(basis):
        return {"basis": basis, "current": {}, "prior": {}, "deltas": {},
                "window": None, "prior_window": None}
    assert ga._merge_headline_yoy([_y("yoy"), _y("mom")])["basis"] == "mixed"
    assert ga._merge_headline_yoy([_y("yoy"), _y("yoy")])["basis"] == "yoy"


# ── nested list merges ───────────────────────────────────────────────────────

def test_ad_click_campaigns_merge_rather_than_concatenate():
    merged = ga._merge_by_key(
        [[{"campaign_id": "c1", "campaign_name": "Brand", "calls": 3, "clicks": 10}],
         [{"campaign_id": "c1", "campaign_name": "Brand", "calls": 2, "clicks": 5}]],
        "campaign_id", int_fields=("calls", "clicks"), carry=("campaign_name",))
    assert len(merged) == 1
    assert merged[0]["calls"] == 5 and merged[0]["clicks"] == 15


def test_channel_mix_merges_by_channel():
    merged = ga._merge_by_key(
        [[{"channel": "Paid Search", "count": 10}, {"channel": "Direct", "count": 4}],
         [{"channel": "Paid Search", "count": 5}]],
        "channel", int_fields=("count",))
    by = {r["channel"]: r["count"] for r in merged}
    assert by == {"Paid Search": 15, "Direct": 4}


# ── payload shape: the contract that lets the two surfaces share components ──

class _StubQueries:
    """Every reader returns a benign empty result, so the builders run without
    BigQuery and we can compare the SHAPE of what they produce."""
    @staticmethod
    def _empty_dict(*a, **k):
        return {}

    @staticmethod
    def _empty_list(*a, **k):
        return []

    def __getattr__(self, name):
        if name.endswith(("_by_month", "_monthly", "_trend", "_roi", "_mix")):
            return self._empty_list
        return self._empty_dict


@pytest.fixture
def stub_queries(monkeypatch):
    stub = _StubQueries()
    monkeypatch.setattr(payloads, "q", stub)
    monkeypatch.setattr(ga, "q", stub)
    monkeypatch.setattr(ga.clinic_hours, "open_hours_in_window", lambda *a, **k: 0.0)
    monkeypatch.setattr(payloads.clinic_hours, "open_hours_in_window", lambda *a, **k: 0.0)
    return stub


def _spec(cid):
    return {"clinic_id": cid, "clinic_name": f"Clinic {cid}",
            "invoca_ids": ["1"], "ga_ids": ["2"], "hours": None,
            "pms_type": "counselear"}


def test_group_payload_has_the_same_keys_as_a_clinic_payload(stub_queries):
    """This is what makes the group route able to render through OverviewView.
    If a new section is added to the clinic payload and not to the rollup, this
    fails — which is the point."""
    window = Window("2026-01-01", "2026-06-30")
    clinic = payloads.build_overview(
        clinic_id="A", clinic_name="Clinic A", invoca_campaign_ids=["1"],
        ga_campaign_ids=["2"], window=window, with_recommendations=False)
    group = payloads.build_group_overview(
        instance_id="INST", instance_name="Virsono",
        clinic_specs=[_spec("A"), _spec("B")], window=window,
        with_recommendations=False)

    missing = set(clinic) - set(group)
    assert not missing, f"group payload is missing clinic sections: {sorted(missing)}"
    # Group-only additions are expected; nothing else should differ.
    # instance_id / group_intelligence are on BOTH surfaces in production — the
    # clinic ones are attached by the endpoint after build_overview returns
    # (api/intelligence.py), so they're absent from the builder's output here.
    # pms_coverage is group-only by nature: a clinic is integrated or it isn't,
    # while a rollup can be partly measurable and has to name which locations.
    assert set(group) - set(clinic) == {
        "is_group", "clinic_count", "clinic_names", "aggregation_notes",
        "instance_id", "group_intelligence", "pms_coverage"}


def test_group_payload_carries_instance_identity_and_disclosure(stub_queries):
    group = payloads.build_group_overview(
        instance_id="INST", instance_name="Virsono",
        clinic_specs=[_spec("A"), _spec("B")],
        window=Window("2026-01-01", "2026-06-30"), with_recommendations=False)
    # The shared components read clinic_id/clinic_name.
    assert group["clinic_id"] == "INST"
    assert group["clinic_name"] == "Virsono"
    assert group["is_group"] is True
    assert group["clinic_count"] == 2
    assert group["clinic_names"] == {"A": "Clinic A", "B": "Clinic B"}
    assert group["aggregation_notes"], "rollup must disclose how it aggregates"


# ── PMS coverage across locations ────────────────────────────────────────────
#
# The rollup is the one surface where the answer can be PARTIAL: summing over
# locations without a PMS feed is exactly what makes an incomplete revenue total
# look like a complete one.

def _no_pms_spec(cid):
    return {**_spec(cid), "pms_type": "none"}


def test_group_names_the_locations_with_no_pms_integration(stub_queries):
    group = payloads.build_group_overview(
        instance_id="INST", instance_name="Virsono",
        clinic_specs=[_spec("A"), _no_pms_spec("B")],
        window=Window("2026-01-01", "2026-06-30"), with_recommendations=False)
    cov = group["pms_coverage"]
    assert cov == {"clinics_total": 2, "clinics_integrated": 1, "missing": ["Clinic B"]}
    # Partial coverage is still integrated — the totals mean something, they
    # just don't cover everyone, which the caveat has to name.
    assert group["pms_integrated"] is True
    assert "Clinic B" in group["pms_caveat"]
    assert group["pms_caveat"] in group["aggregation_notes"]


def test_group_with_no_pms_anywhere_says_revenue_is_unavailable(stub_queries):
    group = payloads.build_group_overview(
        instance_id="INST", instance_name="Virsono",
        clinic_specs=[_no_pms_spec("A"), _no_pms_spec("B")],
        window=Window("2026-01-01", "2026-06-30"), with_recommendations=False)
    assert group["pms_integrated"] is False
    assert group["pms_type"] == "none"
    assert "not zero" in group["pms_caveat"]


def test_group_pms_type_is_mixed_only_across_different_systems(stub_queries):
    def _build(specs):
        return payloads.build_group_overview(
            instance_id="INST", instance_name="Virsono", clinic_specs=specs,
            window=Window("2026-01-01", "2026-06-30"), with_recommendations=False)
    # A location with no PMS must not turn a single-system group into "mixed" —
    # it contributes no system to name.
    assert _build([_spec("A"), _no_pms_spec("B")])["pms_type"] == "counselear"
    assert _build([_spec("A"),
                   {**_spec("B"), "pms_type": "blueprint"}])["pms_type"] == "mixed"


# ── Endpoint: capability-flag gate ───────────────────────────────────────────

class _FakeInstance:
    def __init__(self, flag):
        self.instance_id = "INST"
        self.instance_name = "Virsono"
        self.multi_location_group = flag


class _FakeScalars:
    def all(self):
        return []


class _FakeResult:
    def all(self):
        return []

    def scalars(self):
        return _FakeScalars()


def _use_session(instance):
    def _override():
        yield type("S", (), {
            "get": lambda self, m, i: instance,
            "execute": lambda self, *a, **k: _FakeResult(),
            "scalars": lambda self, *a, **k: [],
        })()
    app.dependency_overrides[get_session] = _override


@pytest.fixture
def client():
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def test_group_overview_flag_off_404(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeInstance(flag=False))
    r = client.get("/intelligence/group/INST/overview?days=30")
    assert r.status_code == 404


def test_group_overview_missing_instance_404(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(None)
    r = client.get("/intelligence/group/NOPE/overview?days=30")
    assert r.status_code == 404


def test_group_overview_flag_on_ok(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeInstance(flag=True))
    monkeypatch.setattr("intelligence_report.payloads.build_group_overview",
                        lambda **k: {"clinic_id": k["instance_id"], "ok": True})
    r = client.get("/intelligence/group/INST/overview?days=30")
    assert r.status_code == 200
    assert r.json() == {"clinic_id": "INST", "ok": True}


def test_group_overview_bad_date_range_422(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeInstance(flag=True))
    monkeypatch.setattr("intelligence_report.payloads.build_group_overview", lambda **k: {"ok": True})
    r = client.get("/intelligence/group/INST/overview?start=2026-06-10&end=2026-06-01")
    assert r.status_code == 422


def _isolate_cache(monkeypatch):
    """Unit tests must not touch the shared GCS cache tier: objects written by a
    previous run would leak in as hits, making results depend on run order and
    on real cloud state. Also clears the in-process tier."""
    from api import intelligence as _I
    monkeypatch.setattr(_I, "_SHARED_CACHE_ENABLED", False)
    _I._json_cache.clear()
    _I._data_version_cache.clear()
    monkeypatch.setattr(_I, "_data_version", lambda *_a, **_k: "test-version")


def test_group_overview_skip_llm_disables_recommendations_and_bypasses_cache(client, monkeypatch):
    """?skip_llm=1 on the group overview: with_recommendations=False and the
    JSON cache is bypassed in both directions (mirrors the clinic overview)."""
    _isolate_cache(monkeypatch)
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeInstance(flag=True))
    calls = []

    def _fake_build(**k):
        calls.append(k)
        return {"clinic_id": "INST_SKIP", "with_recs": k["with_recommendations"]}

    monkeypatch.setattr("intelligence_report.payloads.build_group_overview", _fake_build)

    r = client.get("/intelligence/group/INST_SKIP/overview?days=30&skip_llm=1")
    assert r.status_code == 200
    assert calls[-1]["with_recommendations"] is False

    # Not cached: the following normal request rebuilds with recommendations.
    r = client.get("/intelligence/group/INST_SKIP/overview?days=30")
    assert r.status_code == 200
    assert len(calls) == 2
    assert calls[-1]["with_recommendations"] is True
