"""
Per-clinic actionable client worklists (JSON).

Unlike ``api/intelligence.py`` (which renders HTML reports for an iframe),
these endpoints return JSON row arrays so the frontend can render interactive,
searchable/sortable React tables. All PHI/Blueprint_PHI access stays here in
the hypervisor (proper IAM/BAA + ``require_read_access`` gate); the frontend
reaches them through ``/api/proxy/*`` with a Firebase Bearer token.

Access: any instance member (super_admin, admin, or viewer scoped to their
instance) — ``require_read_access`` enforces per-clinic scoping. These power
the front-desk "who can we call today" lists, so clinic staff (viewer) are
intended consumers.
"""
import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from api import audit
from api.account.worklist_taxonomy import (
    WorklistCohort, WorklistTaxonomyConfig, resolve_taxonomy,
)
from api.core.db import get_session
from api.core.orm import Clinic, InvocaCampaign
from api.deps import require_read_access, require_write_access, verify_token

router = APIRouter()


def _clinic_or_404(clinic_id: str, caller: dict, db: Session) -> Clinic:
    """Resolve the clinic and enforce read access, mirroring intelligence.py."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)
    return clinic


def _resolve_cohort_or_404(
    clinic: Clinic, cohort_key: str,
) -> "tuple[WorklistCohort, WorklistTaxonomyConfig]":
    """Find an enabled cohort in the clinic's taxonomy, else 404."""
    tax = resolve_taxonomy(clinic)
    cohort = tax.cohort(cohort_key)
    if cohort is None or not cohort.enabled:
        raise HTTPException(
            status_code=404,
            detail=f"Cohort '{cohort_key}' is not configured for this clinic",
        )
    return cohort, tax


def _run_cohort(clinic: Clinic, cohort_key: str, *, days: int, limit: int | None,
                include_contact: bool) -> list[dict]:
    cohort, tax = _resolve_cohort_or_404(clinic, cohort_key)
    from intelligence_report import queries
    return queries.cohort_detail(
        clinic.clinic_id,
        event_types=cohort.event_types or None,
        event_like=cohort.event_like,
        statuses=cohort.statuses,
        require_no_sale=cohort.require_no_sale,
        ha_item_types=tax.ha_item_types,
        days=max(7, min(int(days), 1825)),
        limit=limit,
        include_contact=include_contact,
    )


# ── Configurable reactivation cohorts (tested-not-sold, fitted-not-sold, …) ──

@router.get("/clinics/{clinic_id}/worklists/cohorts")
def worklist_cohorts(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
) -> list[dict]:
    """The clinic's enabled worklist cohorts (from config or the built-in default)."""
    clinic = _clinic_or_404(clinic_id, caller, db)
    tax = resolve_taxonomy(clinic)
    return [
        {"key": c.key, "label": c.label, "require_no_sale": c.require_no_sale}
        for c in tax.cohorts if c.enabled
    ]


@router.get("/clinics/{clinic_id}/worklists/pms-taxonomy")
def worklist_pms_taxonomy(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
) -> dict:
    """Distinct PMS event/item types (with counts) for the config UI to pick from."""
    _clinic_or_404(clinic_id, caller, db)
    from intelligence_report import queries
    return queries.pms_taxonomy_options(clinic_id)


@router.get("/clinics/{clinic_id}/worklists/cohort/{cohort_key}")
def worklist_cohort(
    clinic_id: str,
    cohort_key: str,
    days: int = 365,
    limit: int = 500,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
) -> list[dict]:
    """On-screen rows for one configured cohort (no contact PHI)."""
    clinic = _clinic_or_404(clinic_id, caller, db)
    return _run_cohort(
        clinic, cohort_key,
        days=days, limit=max(1, min(int(limit), 2000)), include_contact=False,
    )


_EXPORT_COLUMNS = [
    "client_id", "given_name", "surname", "email",
    "primary_phone", "mobile_phone", "home_phone", "work_phone",
    "tested_appt_date", "tested_appt_type", "tested_appt_status",
    "patient_status", "do_not_email", "do_not_text",
]


@router.get("/clinics/{clinic_id}/worklists/cohort/{cohort_key}/export.csv")
def worklist_cohort_export(
    clinic_id: str,
    cohort_key: str,
    days: int = 365,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """CSV of a cohort with patient contact info, for a Customer.io campaign.

    Marketing opt-outs (``do_not_send_commercial_messages``) are excluded.
    Gated to super_admin + instance admins (``require_write_access`` — viewers
    are rejected) and written to the PHI access audit log.
    """
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_write_access(clinic.instance_id, caller)

    rows = _run_cohort(
        clinic, cohort_key, days=days, limit=5000, include_contact=True,
    )
    kept = [r for r in rows if not r["do_not_send_commercial_messages"]]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in kept:
        writer.writerow({
            "client_id":          r["client_id"],
            "given_name":         r["given_name"],
            "surname":            r["surname"],
            "email":              r.get("email", ""),
            "primary_phone":      r.get("primary_phone", ""),
            "mobile_phone":       r.get("mobile_phone", ""),
            "home_phone":         r.get("home_phone", ""),
            "work_phone":         r.get("work_phone", ""),
            "tested_appt_date":   r["appt_start_time"] or "",
            "tested_appt_type":   r["appt_event_type"],
            "tested_appt_status": r["appt_status"],
            "patient_status":     r["patient_status"],
            "do_not_email":       r.get("do_not_email", False),
            "do_not_text":        r["do_not_text"],
        })

    audit.log_phi_access(
        clinic_id=clinic_id,
        actor=caller.get("email") or caller.get("uid") or "unknown",
        action="worklist_export",
        outcome="ok",
        detail=f"cohort={cohort_key} days={int(days)} rows={len(kept)}",
    )

    buf.seek(0)
    filename = f"{cohort_key}_{int(days)}d.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/clinics/{clinic_id}/worklists/qualified-leads")
def qualified_leads_worklist(
    clinic_id: str,
    days: int = 90,
    limit: int = 500,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
) -> list[dict]:
    """Callers who were qualified but didn't book — per-call rows."""
    _clinic_or_404(clinic_id, caller, db)
    invoca_campaign_ids = [
        str(c) for c in db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    ]
    from intelligence_report import queries
    return queries.qualified_lead_no_conv_detail(
        clinic_id, invoca_campaign_ids, days=max(7, min(int(days), 1825)),
        limit=max(1, min(int(limit), 2000)),
    )


@router.get("/clinics/{clinic_id}/worklists/fitting-no-purchase")
def fitting_no_purchase_worklist(
    clinic_id: str,
    days: int = 365,
    limit: int = 500,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
) -> list[dict]:
    """Patients who had a fitting appointment but bought no hearing aid."""
    _clinic_or_404(clinic_id, caller, db)
    from intelligence_report import queries
    return queries.fitting_no_purchase_detail(
        clinic_id, days=max(7, min(int(days), 1825)),
        limit=max(1, min(int(limit), 2000)),
    )


@router.get("/clinics/{clinic_id}/worklists/warranty-expiring")
def warranty_expiring_worklist(
    clinic_id: str,
    days_ahead: int = 90,
    limit: int = 500,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
) -> list[dict]:
    """Patients whose device warranty / service plan lapses within the window."""
    _clinic_or_404(clinic_id, caller, db)
    from intelligence_report import queries
    return queries.warranty_expiring_detail(
        clinic_id, days_ahead=max(1, min(int(days_ahead), 730)),
        limit=max(1, min(int(limit), 2000)),
    )


# ── Database-reactivation segments (dormant patients) ────────────────────────

@router.get("/clinics/{clinic_id}/worklists/lapsed-patients")
def lapsed_patients_worklist(
    clinic_id: str,
    years: int = 3,
    limit: int = 500,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
) -> list[dict]:
    """Patients with no appointment AND no invoice in the last `years`."""
    _clinic_or_404(clinic_id, caller, db)
    from intelligence_report import queries
    return queries.lapsed_patients_detail(
        clinic_id, years=max(1, min(int(years), 20)),
        limit=max(1, min(int(limit), 2000)),
    )


@router.get("/clinics/{clinic_id}/worklists/recall-due")
def recall_due_worklist(
    clinic_id: str,
    overdue_days: int = 365,
    days_ahead: int = 30,
    limit: int = 500,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
) -> list[dict]:
    """Missed / due follow-ups (Blueprint recalls with no appointment since)."""
    _clinic_or_404(clinic_id, caller, db)
    from intelligence_report import queries
    return queries.recall_due_detail(
        clinic_id,
        overdue_days=max(1, min(int(overdue_days), 1825)),
        days_ahead=max(0, min(int(days_ahead), 365)),
        limit=max(1, min(int(limit), 2000)),
    )


@router.get("/clinics/{clinic_id}/worklists/upgrade-candidates")
def upgrade_candidates_worklist(
    clinic_id: str,
    min_age_years: int = 4,
    limit: int = 500,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
) -> list[dict]:
    """Patients on a hearing aid older than `min_age_years` (upgrade target)."""
    _clinic_or_404(clinic_id, caller, db)
    from intelligence_report import queries
    return queries.upgrade_candidates_detail(
        clinic_id, min_age_years=max(1, min(int(min_age_years), 20)),
        limit=max(1, min(int(limit), 2000)),
    )


# ── Call analysis: callscoring category breakdown (JSON for the Leads page) ──

@router.get("/clinics/{clinic_id}/worklists/callscoring-categories")
def callscoring_categories(
    clinic_id: str,
    days: int = 90,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
) -> dict:
    """Per-flag call-category counts (appointment_booked, no_conversation, …)."""
    _clinic_or_404(clinic_id, caller, db)
    invoca_campaign_ids = [
        str(c) for c in db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    ]
    from intelligence_report import queries
    return queries.callscoring_flag_summary(
        clinic_id, invoca_campaign_ids, days=max(7, min(int(days), 1825)),
    )
