"""`/v2/admin/provision` — create a client account (Firebase user + instance + clinics).

Re-homed from the Next app's `src/app/api/admin/provision/route.ts`, which was
the only place this existed. That route needed the Firebase Admin SDK to mint a
user, so it could not move to a static SPA — provisioning was simply unavailable
in cortex-spa until this landed.

WHAT CHANGED IN THE PORT, and why it is safe:

The Next route ran OUTSIDE the hypervisor, so to call `POST /provision_account/`
it had to become the target user: create/lookup the Firebase user, ensure a role
claim, mint a custom token, exchange it at the Identity Toolkit for an ID token,
then send that as the Bearer. Every one of those steps existed only to satisfy
the HTTP hop. In-process there is no hop — this handler already holds a verified
super_admin caller and can call `provision_full_account` directly. So the custom
token, the token exchange, and the `firebase-web-api-key` dependency are gone.
What the exchange *implied* is preserved explicitly:

  * the new user is created if absent, and given a role claim if they have none
    (`admin`, as before — the level that can write to instances they belong to);
  * an existing role claim is never overwritten, so provisioning for an existing
    super_admin or viewer does not silently re-level them;
  * a uid that already owns an instance is refused. The Next route got this for
    free — it acted as the target user, who is never super_admin-bypassing —
    and dropping it here would let one contact silently accumulate instances,
    which `GET /instance/{uid}` (single row per uid) cannot represent.

ORDERING. Everything that can be refused is checked BEFORE Firebase is touched,
so the common failure (already provisioned) creates nothing. After that point a
Cloud SQL failure can leave a Firebase user with no instance — recoverable, and
harmless: `admin` with no membership reaches nothing (see api/deps.py). The
reverse order cannot be made safe, since the uid is required to write the rows.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import auth as fb_auth
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.account.provisioning import provision_full_account
from api.core.db import get_session
from api.core.orm import Instance
from api.models import ClinicCreate
from api.v2.admin_users import require_super_admin

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# Role given to a brand-new primary contact. Matches the Next route: enough to
# manage the instance they own, never global. Users who already have a claim
# keep it.
DEFAULT_ROLE = "admin"


class ProvisionInstanceFields(BaseModel):
    instance_name: str
    # Upstream account ids, set here so a new client's ETL can start on day one
    # instead of after a follow-up config pass. Both are instance-wide (one ads
    # account / one Invoca profile per business) and both are optional — they
    # are often not known during the onboarding call, and the settings Details
    # tab edits them later via PATCH /instance/{instance_id}.
    google_ads_customer_id: str | None = None
    invoca_profile_id: str | None = None


class ProvisionRequestV2(BaseModel):
    primary_contact_email: str
    primary_contact_name: str
    instance: ProvisionInstanceFields
    clinics: list[ClinicCreate] = Field(
        default_factory=list,
        description="Clinics to create with the instance. Each may carry a "
                    "`ref_id`, echoed back in `clinic_ids` so the caller can "
                    "match its own draft rows to the generated clinic ids.")


@router.post("/provision", status_code=201)
def provision(body: ProvisionRequestV2,
              caller: dict = Depends(require_super_admin),
              db: Session = Depends(get_session)) -> dict:
    """Provision a new client account: Firebase user + instance + its clinics."""
    email = body.primary_contact_email.strip()
    contact_name = body.primary_contact_name.strip()
    instance_name = body.instance.instance_name.strip()
    if not email or not contact_name or not instance_name:
        raise HTTPException(
            status_code=422,
            detail="primary_contact_email, primary_contact_name and "
                   "instance_name are all required")

    # ── Checks that can refuse, before anything is written ──────────────────
    try:
        user = fb_auth.get_user_by_email(email)
    except fb_auth.UserNotFoundError:
        user = None
    except ValueError as exc:  # malformed address
        raise HTTPException(status_code=422, detail=str(exc))

    if user is not None:
        owned = db.scalar(
            select(Instance.instance_name)
            .where(Instance.primary_contact_uid == user.uid))
        if owned:
            raise HTTPException(
                status_code=409,
                detail=(f"{email} is already the primary contact for "
                        f"“{owned}”. Add a clinic to that instance instead, or "
                        f"use a different contact for the new business."))

    # ── Writes ─────────────────────────────────────────────────────────────
    created_user = user is None
    if created_user:
        try:
            user = fb_auth.create_user(email=email, display_name=contact_name,
                                       email_verified=False)
        except fb_auth.EmailAlreadyExistsError:
            # Raced with another create between the lookup and here.
            raise HTTPException(status_code=409,
                                detail=f"{email} already has an account")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    if not (user.custom_claims or {}).get("role"):
        # Replaces the whole claims object, matching set-claims and the admin
        # user manager. `role` must stay the only custom claim.
        fb_auth.set_custom_user_claims(user.uid, {"role": DEFAULT_ROLE})

    result = provision_full_account(
        db,
        instance_create={
            "instance_name": instance_name,
            "primary_contact_name": contact_name,
            "primary_contact_email": email,
            # Trimmed, and blank normalises to NULL rather than "" — see
            # provision_instance.
            "google_ads_customer_id":
                (body.instance.google_ads_customer_id or "").strip() or None,
            "invoca_profile_id":
                (body.instance.invoca_profile_id or "").strip() or None,
        },
        clinics_create=[c.model_dump() for c in body.clinics],
        primary_contact_uid=user.uid,
    )

    # No SMTP here — the Next route created accounts with no password and no way
    # in, which meant a manual Firebase-console trip per client. Hand the admin a
    # link to forward instead, exactly as POST /v2/admin/users does.
    reset_link = None
    if created_user:
        try:
            reset_link = fb_auth.generate_password_reset_link(email)
        except Exception as exc:  # noqa: BLE001 — the account exists either way
            log.warning("reset link generation failed for %s: %s", email, exc)

    log.info("instance provisioned by=%s uid=%s email=%s instance=%s clinics=%d",
             caller["uid"], user.uid, email, result["instance_id"],
             len(body.clinics))
    return {
        "status": "success",
        "uid": user.uid,
        "created_user": created_user,
        "instance_id": result["instance_id"],
        "clinic_ids": result["clinic_id_map"],
        "password_reset_link": reset_link,
    }
