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
import hmac
import io
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from api import audit
from api.account.worklist_taxonomy import (
    WorklistCohort, WorklistTaxonomyConfig, resolve_taxonomy,
)
from api.core.db import get_session
from api.core.orm import Clinic, CustomerIOEnrollment, InvocaCampaign
from api.core.secrets import get_secret
from api.deps import require_read_access, require_write_access, verify_token

log = logging.getLogger(__name__)

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


# ── Customer.io reactivation sync (Alto pilot) ───────────────────────────────

def _sync_actor(request: Request, clinic: Clinic) -> str:
    """Authenticate the customerio-sync caller; return an actor label.

    Two callers exist: Cloud Scheduler (daily job — ``X-CIO-Sync-Secret``
    header against the ``customerio-sync-secret`` SM secret) and a human
    admin (Firebase bearer token + write access, for the initial push and
    ad-hoc reruns). Secret path is checked first so the scheduler never needs
    a Firebase identity.
    """
    supplied = (request.headers.get("X-CIO-Sync-Secret") or "").strip()
    if supplied:
        expected = (get_secret("customerio-sync-secret") or "").strip()
        if expected and hmac.compare_digest(supplied, expected):
            return "scheduler"
        raise HTTPException(status_code=401, detail="Bad sync secret")

    authz = request.headers.get("Authorization") or ""
    if not authz.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    from firebase_admin import auth as fb_auth
    try:
        caller = fb_auth.verify_id_token(authz.removeprefix("Bearer "))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    require_write_access(clinic.instance_id, caller)
    return caller.get("email") or caller.get("uid") or "unknown"


@router.post("/clinics/{clinic_id}/worklists/cohort/{cohort_key}/customerio-sync")
def worklist_cohort_customerio_sync(
    clinic_id: str,
    cohort_key: str,
    request: Request,
    days: int = 365,
    dry_run: bool = True,
    event_name: str | None = None,
    db: Session = Depends(get_session),
) -> dict:
    """Enroll NEW cohort entrants into the Customer.io reactivation campaign.

    Idempotent: every run evaluates the full current cohort but only patients
    with no ``customerio_enrollments`` row are acted on, so the same endpoint
    serves the one-time initial push AND the daily incremental (Cloud
    Scheduler). ``dry_run`` (the DEFAULT — go-live requires an explicit
    ``dry_run=false``) reports what would happen and writes nothing anywhere.

    Per new patient: consent gate first (fully-opted-out patients never reach
    Customer.io), then contact check, then identify + campaign-trigger event
    via ``api/services/customerio.py``. ``event_name`` defaults to
    ``<cohort_key>_lead`` with dashes underscored — e.g. ``tested_not_sold_lead``
    — and MUST match the trigger configured on the Customer.io campaign.
    Send failures are skipped (no log row) so the next run retries them.
    """
    from api.services import customerio as cio

    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    actor = _sync_actor(request, clinic)

    event = (event_name or f"{cohort_key.replace('-', '_')}_lead").strip()
    rows = _run_cohort(clinic, cohort_key, days=days, limit=5000,
                       include_contact=True)

    enrolled = set(db.scalars(
        select(CustomerIOEnrollment.client_id).where(
            CustomerIOEnrollment.clinic_id == clinic_id,
            CustomerIOEnrollment.cohort_key == cohort_key,
        )
    ))
    new_rows = [r for r in rows if str(r["client_id"]) not in enrolled]

    summary = {"clinic_id": clinic_id, "cohort_key": cohort_key, "days": int(days),
               "event_name": event, "dry_run": dry_run,
               "cohort_size": len(rows), "already_enrolled": len(rows) - len(new_rows),
               "sent": 0, "blocked_consent": 0, "no_contact": 0, "send_errors": 0}
    client: cio.CustomerIOClient | None = None

    for r in new_rows:
        consent = cio.Consent(
            do_not_send_commercial_messages=bool(r["do_not_send_commercial_messages"]),
            do_not_text=bool(r["do_not_text"]),
            do_not_email=bool(r.get("do_not_email", False)),
        )
        email = (r.get("email") or "").strip()
        phone = next((p for p in (r.get("mobile_phone"), r.get("primary_phone"),
                                  r.get("home_phone"), r.get("work_phone"))
                      if p and str(p).strip()), None)

        if cio.fully_opted_out(consent):
            status = "blocked_consent"
        elif not email and not phone:
            status = "no_contact"
        else:
            status = "sent"
            if not dry_run:
                if client is None:
                    # Per-clinic workspace: raises CustomerIONotConfigured (→ 500
                    # with a clear message) before any patient is touched when the
                    # clinic's secrets don't exist yet.
                    try:
                        client = cio.CustomerIOClient(clinic_id)
                    except cio.CustomerIONotConfigured as exc:
                        raise HTTPException(status_code=409, detail=str(exc))
                patient = cio.PatientRef(clinic_id=clinic_id,
                                         client_id=str(r["client_id"]))
                attrs = {
                    "first_name": r.get("given_name") or "",
                    "last_name": r.get("surname") or "",
                    "clinic_id": clinic_id,
                    "clinic_name": clinic.clinic_name,
                    "cohort": cohort_key,
                    "tested_appt_date": r.get("appt_start_time") or "",
                    "tested_appt_type": r.get("appt_event_type") or "",
                    "source": "cortex-reactivation",
                }
                if email:
                    attrs["email"] = email
                if phone:
                    attrs["phone"] = str(phone).strip()
                try:
                    sent = cio.enroll_patient(
                        client, patient, consent, event_name=event,
                        attributes=attrs,
                        event_data={"cohort": cohort_key, "clinic_id": clinic_id,
                                    "window_days": int(days)},
                    )
                    status = "sent" if sent else "blocked_consent"
                except cio.CustomerIOError as exc:
                    # No log row → retried on the next run.
                    log.warning("customerio enroll failed clinic=%s client=%s: %s",
                                clinic_id, r["client_id"], exc)
                    summary["send_errors"] += 1
                    continue

        summary[status] += 1
        if not dry_run:
            db.add(CustomerIOEnrollment(
                clinic_id=clinic_id, client_id=str(r["client_id"]),
                cohort_key=cohort_key, event_name=event, status=status,
                enrolled_by=actor,
            ))

    if not dry_run:
        db.commit()

    audit.log_phi_access(
        clinic_id=clinic_id, actor=actor,
        action="customerio_sync",
        outcome="dry_run" if dry_run else "ok",
        detail=(f"cohort={cohort_key} days={int(days)} event={event} "
                f"sent={summary['sent']} blocked={summary['blocked_consent']} "
                f"no_contact={summary['no_contact']} errors={summary['send_errors']}"),
    )
    return summary


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
