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
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.db import get_session
from api.core.orm import Clinic, InvocaCampaign
from api.deps import require_read_access, verify_token

router = APIRouter()


def _clinic_or_404(clinic_id: str, caller: dict, db: Session) -> Clinic:
    """Resolve the clinic and enforce read access, mirroring intelligence.py."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)
    return clinic


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
