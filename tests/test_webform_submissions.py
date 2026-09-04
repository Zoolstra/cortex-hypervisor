"""
Tests for the Web forms tab's per-submission list:
``GET /intelligence/{clinic_id}/webform-submissions`` and its group twin.

  * PHI isolation invariants on the SQL (every PHI table clinic-filtered; the
    free-text Appointments columns never selected).
  * Row shaping: PMS name preferred over the form name, matched-on key,
    first-appointment lag vs the shared match window, landing-page reduction.
  * Endpoint gates: viewer 403, admin OK + audited, unknown clinic 404, group
    404 for a single location.

BigQuery is stubbed throughout.
"""
from collections import namedtuple
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api.deps as deps
from api import app
from api.core.db import get_session
from api.deps import verify_token
from intelligence_report import queries as q
from intelligence_report.queries import CALL_BOOKING_MATCH_DAYS, Window


# ── SQL capture ───────────────────────────────────────────────────────────────

class _Job:
    def __init__(self, rows):
        self._rows = rows
    def result(self):
        return self._rows


class _Client:
    def __init__(self, rows=None):
        self.sql = None
        self.params = None
        self._rows = rows or []
    def query(self, sql, job_config=None):
        self.sql = sql
        self.params = {p.name: p.value for p in (job_config.query_parameters if job_config else [])}
        return _Job(self._rows)


def _row(**over):
    base = dict(
        submitted_at="2026-08-01 14:00:00+00:00", submitted_local="2026-08-01 08:00",
        first_name="Form", last_name="Name", email="  Lead@X.com ", phone_number="(613) 555-0001",
        customer_type="New Customer", form_title="Contact",
        utm_source=None, utm_medium=None, utm_campaign=None, utm_term=None, utm_content=None,
        referrer_host="google.com", landing_page="https://site.ca/contact?utm_source=x",
        medium="Organic", source="via google.com", ad_campaign_name=None, has_click_id=False,
        n_clients=None, by_phone=None, by_email=None,
        client_id=None, given_name=None, surname=None,
        event_type=None, start_time=None, status_2=None, location_name=None, practitioner=None,
        created_ts=None, lag_days=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def bq(monkeypatch):
    c = _Client()
    monkeypatch.setattr(q, "_client", lambda: c)
    return c


# ── PHI isolation ─────────────────────────────────────────────────────────────

def test_every_phi_table_is_clinic_filtered(bq):
    q.webform_submission_detail("C1", window=Window("2026-08-01", "2026-08-31"))
    sql = bq.sql
    assert "clinic_id = @clinic_id" in sql                       # webforms
    # patient_contacts, Appointments, ClientDemographics — each scoped
    assert sql.count("_clinic_id = @clinic_id") >= 3
    assert bq.params["clinic_id"] == "C1"


def test_free_text_appointment_columns_are_not_selected(bq):
    q.webform_submission_detail("C1", window=Window("2026-08-01", "2026-08-31"))
    # `title` / `notes` may carry staff-entered clinical text.
    assert "a.title" not in bq.sql
    assert "a.notes" not in bq.sql
    # nor the form's free-text message
    assert "message" not in bq.sql.lower()


def test_defaults_to_shared_match_window_and_utc_tz(bq):
    import inspect
    sig = inspect.signature(q.webform_submission_detail)
    assert sig.parameters["match_days"].default == CALL_BOOKING_MATCH_DAYS
    q.webform_submission_detail("C1", window=Window("2026-08-01", "2026-08-31"))
    assert bq.params["clinic_tz"] == "UTC"
    q.webform_submission_detail("C1", window=Window("2026-08-01", "2026-08-31"),
                                clinic_tz="America/Edmonton")
    assert bq.params["clinic_tz"] == "America/Edmonton"


def test_window_bounds_are_two_sided(bq):
    q.webform_submission_detail("C1", window=Window("2026-08-01", "2026-08-31"))
    assert "submitted_at >= TIMESTAMP('2026-08-01 00:00:00+00:00')" in bq.sql
    assert "submitted_at < TIMESTAMP('2026-09-01 00:00:00+00:00')" in bq.sql


def test_fails_safe_to_empty_list(monkeypatch):
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("bq down")
    monkeypatch.setattr(q, "_client", lambda: _Boom())
    assert q.webform_submission_detail("C1", days=30) == []


# ── Row shaping ───────────────────────────────────────────────────────────────

def test_unmatched_row(bq):
    bq._rows = [_row()]
    [r] = q.webform_submission_detail("C1", days=30)
    assert r["matched"] is False
    assert r["client_id"] is None
    assert r["matched_on"] is None
    assert r["display_name"] == "Form Name" and r["name_source"] == "form"
    assert r["email"] == "Lead@X.com"                # trimmed, case kept for display
    assert r["phone_masked"] == "•••-0001"
    assert r["first_appointment"] is None
    assert r["landing_page"] == "/contact"           # host + query dropped
    assert r["medium"] == "Organic" and r["source"] == "via google.com"
    assert r["utm"] == {"source": None, "medium": None, "campaign": None,
                        "term": None, "content": None}
    assert r["submitted_at"] == "2026-08-01 08:00"   # clinic-local


def test_matched_row_prefers_pms_name_and_reports_lag(bq):
    bq._rows = [_row(
        client_id="P9", given_name="Pat", surname="Ient",
        n_clients=2, by_phone=True, by_email=False,
        event_type="Hearing Test", start_time="2026-08-12 10:00:00", status_2="Completed",
        location_name="Main", practitioner="Dr A",
        created_ts="2026-08-04 09:00:00+00:00", lag_days=3,
    )]
    [r] = q.webform_submission_detail("C1", days=30)
    assert r["matched"] is True and r["client_id"] == "P9"
    assert r["display_name"] == "Pat Ient" and r["name_source"] == "pms"
    assert r["form_name"] == "Form Name" and r["pms_name"] == "Pat Ient"
    assert r["matched_on"] == "phone" and r["matched_patients"] == 2
    fa = r["first_appointment"]
    assert fa["event_type"] == "Hearing Test"
    assert fa["status"] == "Completed"
    assert fa["lag_days"] == 3 and fa["within_match_window"] is True


def test_matched_without_pms_name_falls_back_to_form_name(bq):
    bq._rows = [_row(client_id="P1", given_name=None, surname=None, by_email=True)]
    [r] = q.webform_submission_detail("C1", days=30)
    assert r["matched"] is True
    assert r["display_name"] == "Form Name" and r["name_source"] == "form"
    assert r["matched_on"] == "email"


def test_late_appointment_is_outside_match_window(bq):
    bq._rows = [_row(client_id="P1", by_phone=True, by_email=True,
                     created_ts="2026-09-01 09:00:00+00:00", lag_days=CALL_BOOKING_MATCH_DAYS + 1)]
    [r] = q.webform_submission_detail("C1", days=30)
    assert r["matched_on"] == "phone+email"
    assert r["first_appointment"]["within_match_window"] is False


def test_landing_path_reduction():
    lp = q._landing_path
    assert lp(None) is None and lp("") is None and lp("nan") is None
    assert lp("/") == "/"
    assert lp("/contact") == "/contact"
    assert lp("/?utm_source=chatgpt.com") == "/"
    assert lp("https://www.senseofhearing.ca/contact") == "/contact"
    assert lp("https://www.senseofhearing.ca") == "/"
    assert lp("/book/?gclid=abc#top") == "/book/"


# ── Endpoint gates ────────────────────────────────────────────────────────────

class _FakeClinic:
    def __init__(self, instance_id="INST_A"):
        self.instance_id = instance_id
        self.clinic_name = "Test Clinic"
        self.deleted_at = None
        self.pms_type = "blueprint"
        self.location = SimpleNamespace(time_zone="America/Edmonton")


def _use_session(clinic, n_clinics=1):
    def _override():
        yield type("S", (), {"get": lambda self, m, i: clinic,
                             "scalar": lambda self, *a, **k: n_clinics,
                             "scalars": lambda self, *a, **k: [],
                             "execute": lambda self, *a, **k: SimpleNamespace(all=lambda: [])})()
    app.dependency_overrides[get_session] = _override


@pytest.fixture
def client():
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def test_viewer_denied(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "viewer", "uid": "v1"}
    monkeypatch.setattr(deps, "_is_instance_member", lambda inst, uid: True)
    _use_session(_FakeClinic())
    assert client.get("/intelligence/C1/webform-submissions").status_code == 403


def test_unknown_clinic_404(client):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(None)
    assert client.get("/intelligence/NOPE/webform-submissions").status_code == 404


def test_bad_range_422(client):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    _use_session(_FakeClinic())
    r = client.get("/intelligence/C1/webform-submissions?start=2026-06-10&end=2026-06-01")
    assert r.status_code == 422


def test_admin_ok_audited_and_tz_threaded(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa", "email": "a@b.com"}
    _use_session(_FakeClinic())
    seen = {}
    def _fake(clinic_id, **kw):
        seen.update(kw, clinic_id=clinic_id)
        return [{"display_name": "Pat", "matched": True}]
    monkeypatch.setattr("intelligence_report.queries.webform_submission_detail", _fake)
    audited = {}
    monkeypatch.setattr("api.intelligence.log_phi_access", lambda **kw: audited.update(kw))
    r = client.get("/intelligence/C1/webform-submissions?start=2026-08-01&end=2026-08-31")
    assert r.status_code == 200
    body = r.json()
    assert body["submissions"][0]["display_name"] == "Pat"
    assert body["pms_integrated"] is True
    assert body["match_days"] == CALL_BOOKING_MATCH_DAYS
    assert body["window"] == {"start": "2026-08-01", "end": "2026-09-01"}
    assert seen["clinic_tz"] == "America/Edmonton"
    assert audited["action"] == "webform_submissions"
    assert audited["clinic_id"] == "C1"
    assert audited["detail"] == "n=1"


def test_group_single_location_404(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa"}
    monkeypatch.setattr("api.intelligence.is_multi_location", lambda db, iid: False)
    _use_session(SimpleNamespace(instance_id="I1", instance_name="Inst"))
    assert client.get("/intelligence/group/I1/webform-submissions").status_code == 404


def test_group_viewer_denied(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "viewer", "uid": "v1"}
    monkeypatch.setattr(deps, "_is_instance_member", lambda inst, uid: True)
    _use_session(SimpleNamespace(instance_id="I1", instance_name="Inst"))
    assert client.get("/intelligence/group/I1/webform-submissions").status_code == 403


def test_group_fans_out_tags_clinic_and_audits(client, monkeypatch):
    app.dependency_overrides[verify_token] = lambda: {"role": "super_admin", "uid": "sa", "email": "a@b.com"}
    monkeypatch.setattr("api.intelligence.is_multi_location", lambda db, iid: True)
    # SQLAlchemy Rows unpack like tuples AND expose columns as attributes; the
    # endpoint relies on both, so the fake must too.
    _Row = namedtuple("_Row", "clinic_id clinic_name pms_type time_zone")
    clinics = [_Row("C1", "One", "counselear", "America/Toronto"),
               _Row("C2", "Two", None, None)]
    def _override():
        yield type("S", (), {
            "get": lambda self, m, i: SimpleNamespace(instance_id="I1", instance_name="Inst"),
            "execute": lambda self, *a, **k: SimpleNamespace(all=lambda: list(clinics)),
        })()
    app.dependency_overrides[get_session] = _override
    def _fake(clinic_id, **kw):
        return [{"submitted_at_utc": f"2026-08-0{2 if clinic_id == 'C1' else 5} 00:00:00", "clinic": clinic_id}]
    monkeypatch.setattr("intelligence_report.queries.webform_submission_detail", _fake)
    audited = {}
    monkeypatch.setattr("api.intelligence.log_phi_access", lambda **kw: audited.update(kw))
    r = client.get("/intelligence/group/I1/webform-submissions?days=30")
    assert r.status_code == 200
    body = r.json()
    rows = body["submissions"]
    assert [x["clinic_name"] for x in rows] == ["Two", "One"]      # newest first across clinics
    assert rows[0]["clinic_id"] == "C2"
    assert body["pms_integrated"] is True                          # any location with a feed
    assert audited["action"] == "group_webform_submissions"
    assert audited["detail"] == "clinics=2 n=2"
