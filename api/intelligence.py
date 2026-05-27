"""
Per-clinic intelligence report endpoint.

``GET /intelligence/{clinic_id}/report.html`` — admin / super_admin only —
returns a fully-rendered HTML document built from Blueprint_PHI + ClinicData.
The report module is stdlib-only (no pandas/plotly) so it can live inside the
hypervisor container without bloating the image.
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.db import get_session
from api.core.orm import Clinic, GoogleAdsCampaign, InvocaCampaign
from api.deps import require_read_access, verify_token

router = APIRouter()


@router.get("/intelligence/{clinic_id}/report.html")
def get_intelligence_report(
    clinic_id: str,
    days: int = 365,
    nocache: bool = False,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Generate and return the clinic intelligence report as HTML.

    On-demand generation with an in-process TTL cache (5 minutes) so repeated
    reloads of the same ``(clinic_id, days)`` don't re-run the BigQuery
    pipeline. Pass ``?nocache=1`` to bypass the cache for a fresh render.
    """
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    invoca_campaign_ids = list(
        db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    )
    google_ads_campaign_ids = list(
        db.scalars(
            select(GoogleAdsCampaign.google_ads_campaign_id).where(
                GoogleAdsCampaign.clinic_id == clinic_id,
                GoogleAdsCampaign.active.is_(True),
            )
        )
    )

    # Lazy import — keeps the report module out of the hot start path for any
    # other endpoint that doesn't need it.
    from intelligence_report.report import generate_report_with_campaigns

    html = generate_report_with_campaigns(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=[str(c) for c in invoca_campaign_ids],
        google_ads_campaign_ids=[str(c) for c in google_ads_campaign_ids],
        days=max(7, min(int(days), 1825)),
        use_cache=not nocache,
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


@router.get("/intelligence/{clinic_id}/spam-calls.html")
def get_spam_calls_report(
    clinic_id: str,
    days: int = 90,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Drill-down detail: every inbound call flagged as spam by the four
    heuristics shared with the main funnel.

    Gated by the same read access check as the main report.
    """
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    invoca_campaign_ids = list(
        db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    )

    from intelligence_report.report import generate_spam_calls_report

    html = generate_spam_calls_report(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=[str(c) for c in invoca_campaign_ids],
        days=max(7, min(int(days), 1825)),
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


@router.get("/intelligence/{clinic_id}/no-conversation-calls.html")
def get_no_conversation_report(
    clinic_id: str,
    days: int = 90,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Drill-down detail: per-call rows where the funnel ended at
    'No Conversation' (voicemail / hangup / silent autodial)."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    invoca_campaign_ids = list(
        db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    )

    from intelligence_report.report import generate_no_conversation_report

    html = generate_no_conversation_report(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=[str(c) for c in invoca_campaign_ids],
        days=max(7, min(int(days), 1825)),
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


@router.get("/intelligence/{clinic_id}/qualified-no-conv-calls.html")
def get_qualified_no_conv_report(
    clinic_id: str,
    days: int = 90,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Drill-down detail: per-call rows where a qualified lead engaged but
    didn't book. The main conversion-leak surface."""
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    invoca_campaign_ids = list(
        db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    )

    from intelligence_report.report import generate_qualified_no_conv_report

    html = generate_qualified_no_conv_report(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=[str(c) for c in invoca_campaign_ids],
        days=max(7, min(int(days), 1825)),
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


@router.get("/intelligence/{clinic_id}/attributed-invoices.html")
def get_attributed_invoices_report(
    clinic_id: str,
    days: int = 365,
    booking_window_hours: int = 24,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Drill-down detail: every invoice attributable to a tracked phone call,
    one row per (call × invoice).

    PHI-heavy (patient name, client_id, phone). Gated by the same read access
    check as the main report.
    """
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    require_read_access(clinic.instance_id, caller)

    invoca_campaign_ids = list(
        db.scalars(
            select(InvocaCampaign.invoca_campaign_id).where(
                InvocaCampaign.clinic_id == clinic_id,
                InvocaCampaign.active.is_(True),
            )
        )
    )

    from intelligence_report.report import generate_attributed_invoices_report

    html = generate_attributed_invoices_report(
        clinic_id=clinic_id,
        clinic_name=clinic.clinic_name,
        invoca_campaign_ids=[str(c) for c in invoca_campaign_ids],
        days=max(7, min(int(days), 1825)),
        booking_window_hours=max(1, min(int(booking_window_hours), 720)),
    )
    return Response(content=html, media_type="text/html; charset=utf-8")
