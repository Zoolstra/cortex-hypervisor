"""
Multi-location ("Group Intelligence") readers.

This is a thin orchestration layer over the per-clinic readers in
``queries.py`` — it fans those readers across every clinic in an instance and
rolls the results up for the leaderboard / comparison UI. With one exception
(``zoolstra_attribution``) there is **no new SQL**: each metric still flows
through the single, cache-friendly per-clinic query in ``queries.py``, so the
group view reuses BigQuery's results cache already warmed by the per-clinic
Intelligence pages.

Every per-clinic read is wrapped fail-safe: a clinic with no PMS data (or a
transient query error) yields a zeroed row rather than blanking the whole
leaderboard. Group endpoints are aggregate-only (counts / sums / labels) — no
PHI, so no audit path is involved here.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from google.cloud import bigquery

from intelligence_report.queries import (
    _BP,
    Window,
    _client,
    _date_between,
    appointment_outcomes,
    blueprint_snapshot_date,
    google_ads_roi,
    invoice_revenue,
    line_item_mix,
    referral_breakdown,
    webform_revenue,
    webform_submissions,
)

log = logging.getLogger(__name__)

# Raw CounselEar PHI dataset. The Zoolstra-referral tag lives ONLY on the raw
# ``appointments`` rows (``appt_referral_type``) — the vendor-neutral
# PMS_Unified.Appointments view exposes referral *ids* which are NULL for
# CounselEar — so the attribution funnel reads the raw table directly. This is a
# Virsono/CounselEar-specific feature, gated behind the instance capability flag.
# Project is derived from the existing PMS view constant to stay in sync.
_GCP_PROJECT = _BP.split(".")[0]
_CE = f"{_GCP_PROJECT}.CounselEar_PHI"

# Free-text referral value tagging Zoolstra-driven bookings in CounselEar.
ZOOLSTRA_REFERRAL_TAG = "Referral - Zoolstra"

# Appointment statuses that count as a kept / revenue-bearing visit for the
# cross-location "booked appointments" comparison. Defined explicitly because a
# raw scheduled count is not comparable across clinics with different no-show /
# cancellation behaviour. Statuses are matched case-insensitively; any label not
# in this set stays in ``by_status`` but is excluded from ``booked_appts``.
BOOKED_STATUSES = {"completed", "arrived"}


def _safe(thunk: Callable[[], Any], default: Any) -> Any:
    """Call ``thunk``; on any failure log and return ``default``. Keeps one bad
    sub-read from sinking a whole clinic's comparison row."""
    try:
        return thunk()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("group sub-read failed: %s", exc)
        return default


def _booked_from_status(by_status: dict[str, int]) -> int:
    return sum(
        int(n) for label, n in (by_status or {}).items()
        if str(label).strip().lower() in BOOKED_STATUSES
    )


def per_clinic_summary(
    clinic_id: str,
    clinic_name: str,
    *,
    window: Window,
    pms_type: str = "none",
    ga_campaign_ids: list[str] | None = None,
) -> dict[str, Any]:
    """One clinic's PMS + non-RC marketing rollup, as one comparable row.

    Pure composition of the per-clinic ``queries.py`` readers; each sub-read is
    fail-safe. ``has_pms_data`` is derived from ``blueprint_snapshot_date`` (the
    PMS_Unified snapshot covers Blueprint *and* CounselEar). ``booked_appts``
    uses :data:`BOOKED_STATUSES`.
    """
    ga_campaign_ids = ga_campaign_ids or []

    snapshot = _safe(lambda: blueprint_snapshot_date(clinic_id), None)
    appts = _safe(lambda: appointment_outcomes(clinic_id, window=window),
                  {"total": 0, "by_status": {}, "sales_opportunities": 0})
    rev = _safe(lambda: invoice_revenue(clinic_id, window=window),
                {"invoice_count": 0, "revenue": 0.0})
    mix = _safe(lambda: line_item_mix(clinic_id, window=window), [])
    referrals = _safe(lambda: referral_breakdown(clinic_id, top_n=10, window=window), [])
    forms = _safe(lambda: webform_submissions(clinic_id, window=window),
                  {"total": 0, "new": 0, "returning": 0})
    form_rev = _safe(lambda: webform_revenue(clinic_id, window=window),
                     {"attributed_revenue": 0.0})
    gads = _safe(lambda: google_ads_roi(clinic_id, ga_campaign_ids, window=window), [])

    invoice_count = int(rev.get("invoice_count") or 0)
    revenue = float(rev.get("revenue") or 0.0)
    booked = _booked_from_status(appts.get("by_status") or {})

    return {
        "clinic_id": clinic_id,
        "clinic_name": clinic_name,
        "pms_type": pms_type,
        "has_pms_data": snapshot is not None,
        "snapshot_date": str(snapshot) if snapshot else None,
        "appointments": {
            "total": int(appts.get("total") or 0),
            "by_status": appts.get("by_status") or {},
            "sales_opportunities": int(appts.get("sales_opportunities") or 0),
        },
        "booked_appts": booked,
        "revenue": revenue,
        "invoice_count": invoice_count,
        "avg_invoice": (revenue / invoice_count) if invoice_count else None,
        "product_mix": mix,
        "referrals": referrals,
        "webform_submissions": int(forms.get("total") or 0),
        "webform_attributed_revenue": float(form_rev.get("attributed_revenue") or 0.0),
        "google_ads": gads,
    }


def group_comparison(
    clinics: list[tuple[str, str, str]],
    *,
    window: Window,
    ga_by_clinic: dict[str, list[str]] | None = None,
    max_workers: int = 8,
) -> list[dict[str, Any]]:
    """Fan :func:`per_clinic_summary` across every clinic concurrently.

    ``clinics`` is a list of ``(clinic_id, clinic_name, pms_type)``. Returns one
    row per clinic in input order; a clinic whose summary raises still yields a
    zeroed row (``has_pms_data=False``) so the leaderboard never silently drops a
    location. Wall-clock ≈ a single query (the readers run in parallel).
    """
    ga_by_clinic = ga_by_clinic or {}
    if not clinics:
        return []

    def _one(entry: tuple[str, str, str]) -> dict[str, Any]:
        clinic_id, clinic_name, pms_type = entry
        try:
            return per_clinic_summary(
                clinic_id, clinic_name, window=window, pms_type=pms_type,
                ga_campaign_ids=ga_by_clinic.get(clinic_id, []),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("per_clinic_summary failed clinic=%s: %s", clinic_id, exc)
            return {
                "clinic_id": clinic_id, "clinic_name": clinic_name,
                "pms_type": pms_type, "has_pms_data": False, "snapshot_date": None,
                "appointments": {"total": 0, "by_status": {}, "sales_opportunities": 0},
                "booked_appts": 0, "revenue": 0.0, "invoice_count": 0,
                "avg_invoice": None, "product_mix": [], "referrals": [],
                "webform_submissions": 0, "webform_attributed_revenue": 0.0,
                "google_ads": [],
            }

    with ThreadPoolExecutor(max_workers=min(max_workers, len(clinics))) as pool:
        return list(pool.map(_one, clinics))


def appointment_referral_breakdown(
    clinic_ids: list[str], *, window: Window, top_n: int = 12,
) -> list[dict[str, Any]]:
    """Group-level booking mix by ``appt_referral_type`` (the CounselEar-native
    marketing/referral source on appointments — e.g. 'Referral - Zoolstra',
    'Online', 'ENT Transfer', 'Existing patient').

    This is where Virsono's referral signal actually lives: the unified
    InvoiceMaster referral ids are NULL and InvoiceLineItems/ReferralSources are
    empty for CounselEar, so the standard invoice-based ``referral_breakdown`` is
    blank for this instance. Aggregate-only; returns [] (never raises) if absent.
    """
    if not clinic_ids:
        return []
    sql = f"""
        SELECT
          COALESCE(NULLIF(appt_referral_type, ''), 'Unspecified') AS source,
          COUNT(*)                                                AS bookings,
          COUNT(DISTINCT CAST(patient_id AS STRING))              AS patients
        FROM `{_CE}.appointments`
        WHERE _clinic_id IN UNNEST(@clinic_ids)
          AND {_date_between("appt_date", window)}
        GROUP BY source
        ORDER BY bookings DESC
        LIMIT {int(top_n)}
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("clinic_ids", "STRING", list(clinic_ids)),
    ])
    try:
        rows = list(_client().query(sql, job_config=job_config).result())
    except Exception as exc:
        log.warning("appointment_referral_breakdown query failed: %s", exc)
        return []
    return [
        {"source": r.source, "bookings": int(r.bookings or 0),
         "patients": int(r.patients or 0)}
        for r in rows
    ]


def zoolstra_attribution(clinic_ids: list[str], *, window: Window) -> dict[str, Any]:
    """Closed-loop Zoolstra marketing ROI: bookings tagged
    ``appt_referral_type = "Referral - Zoolstra"`` → CounselEar revenue.

    Per clinic: ``bookings`` (Zoolstra-tagged appointments with ``appt_date`` in
    the window), ``patients`` (distinct), ``invoiced_patients`` + ``revenue``
    (those patients' invoices with ``invoice_date`` in the window). Aggregate-
    only — no patient identifiers are returned.

    Reads the raw CounselEar tables (the Zoolstra tag is absent from the unified
    view). Returns zeros, never raises, if the dataset/columns are absent.
    """
    out: dict[str, Any] = {"per_location": [], "totals": {
        "bookings": 0, "patients": 0, "invoiced_patients": 0,
        "invoice_count": 0, "revenue": 0.0, "conversion_rate": None,
    }}
    if not clinic_ids:
        return out

    sql = f"""
        WITH zoolstra_appts AS (
            SELECT DISTINCT _clinic_id, CAST(patient_id AS STRING) AS patient_id
            FROM `{_CE}.appointments`
            WHERE _clinic_id IN UNNEST(@clinic_ids)
              AND appt_referral_type = @tag
              AND patient_id IS NOT NULL
              AND {_date_between("appt_date", window)}
        ),
        booking_counts AS (
            SELECT _clinic_id,
                   COUNT(*) AS bookings,
                   COUNT(DISTINCT CAST(patient_id AS STRING)) AS patients
            FROM `{_CE}.appointments`
            WHERE _clinic_id IN UNNEST(@clinic_ids)
              AND appt_referral_type = @tag
              AND {_date_between("appt_date", window)}
            GROUP BY _clinic_id
        ),
        patient_revenue AS (
            SELECT za._clinic_id,
                   COUNT(DISTINCT za.patient_id)    AS invoiced_patients,
                   COUNT(DISTINCT i.invoice_id)     AS invoice_count,
                   SUM(SAFE_CAST(i.total_cost AS NUMERIC)) AS revenue
            FROM zoolstra_appts za
            JOIN `{_CE}.invoices` i
              ON i._clinic_id = za._clinic_id
             AND CAST(i.patient_id AS STRING) = za.patient_id
            WHERE SAFE_CAST(i.total_cost AS NUMERIC) > 0
              AND {_date_between("i.invoice_date", window)}
            GROUP BY za._clinic_id
        )
        SELECT b._clinic_id                          AS clinic_id,
               b.bookings,
               b.patients,
               COALESCE(pr.invoiced_patients, 0)     AS invoiced_patients,
               COALESCE(pr.invoice_count, 0)         AS invoice_count,
               COALESCE(pr.revenue, 0)               AS revenue
        FROM booking_counts b
        LEFT JOIN patient_revenue pr USING (_clinic_id)
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("clinic_ids", "STRING", list(clinic_ids)),
        bigquery.ScalarQueryParameter("tag", "STRING", ZOOLSTRA_REFERRAL_TAG),
    ])
    try:
        rows = list(_client().query(sql, job_config=job_config).result())
    except Exception as exc:
        log.warning("zoolstra_attribution query failed: %s", exc)
        return out

    per_location: list[dict[str, Any]] = []
    t = out["totals"]
    for r in rows:
        bookings = int(r.bookings or 0)
        patients = int(r.patients or 0)
        inv_pts = int(r.invoiced_patients or 0)
        inv_cnt = int(r.invoice_count or 0)
        revenue = float(r.revenue or 0.0)
        per_location.append({
            "clinic_id": r.clinic_id,
            "bookings": bookings,
            "patients": patients,
            "invoiced_patients": inv_pts,
            "invoice_count": inv_cnt,
            "revenue": revenue,
            "conversion_rate": (inv_pts / patients) if patients else None,
            "revenue_per_patient": (revenue / patients) if patients else None,
        })
        t["bookings"] += bookings
        t["patients"] += patients
        t["invoiced_patients"] += inv_pts
        t["invoice_count"] += inv_cnt
        t["revenue"] += revenue

    t["conversion_rate"] = (t["invoiced_patients"] / t["patients"]) if t["patients"] else None
    out["per_location"] = per_location
    return out
