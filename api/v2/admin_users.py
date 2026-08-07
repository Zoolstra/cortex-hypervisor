"""`/v2/admin/users` — super-admin user + role administration.

Until now there was no way to see who has access to what, or to change it,
without opening the Firebase console and editing custom claims by hand. This
exposes both as API surface so the SPA can own it.

TWO SEPARATE THINGS make up a user's access, and the UI has to show both or it
will mislead:

  1. `role` — a Firebase custom claim. The CAPABILITY level (super_admin /
     admin / viewer). It is GLOBAL: an `admin` claim says "may write", not "may
     write to instance X".
  2. instance membership — rows in Cloud SQL. The SCOPE. A non-super_admin
     reaches an instance only via `instances.primary_contact_uid` (ownership) or
     a `clinic_admins(uid, instance_id)` row.

So `admin` + no membership can do nothing at all, and that combination is easy
to create by accident. `GET /users` therefore always returns both.

SCOPE IS PER-INSTANCE, NOT PER-CLINIC. `clinic_admins` is unique on
(uid, instance_id) and has no clinic_id, and every clinic route authorises via
`clinic.instance_id`. A user granted a multi-location instance sees ALL of its
clinics. Narrowing that needs a schema change plus a new check in
`require_read_access`; it is deliberately not faked here.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import auth as fb_auth
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.db import get_session
from api.core.orm import ClinicAdmin, Instance
from api.deps import verify_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/users", tags=["admin"])

ROLES = ("super_admin", "admin", "viewer")

# Firebase pages at 1000; this is a safety bound, not a real page size. Well
# above the current population (13) — if it is ever hit the response says so
# rather than silently listing a prefix of the users.
MAX_USERS = 2000


def require_super_admin(caller: dict = Depends(verify_token)) -> dict:
    if caller.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="super_admin required")
    return caller


class RoleUpdate(BaseModel):
    role: str = Field(description="one of super_admin | admin | viewer")


class UserCreate(BaseModel):
    email: str
    role: str = Field(default="viewer",
                      description="one of super_admin | admin | viewer")
    display_name: str | None = None
    instance_ids: list[str] = Field(
        default_factory=list,
        description="Instance grants to apply immediately. Ignored for "
                    "super_admin, which is not scope-limited.")
    password: str | None = Field(
        default=None,
        description="Optional. Omit to create the account without one and get "
                    "back a password-reset link to send the user.")


class InstanceScope(BaseModel):
    instance_ids: list[str] = Field(
        default_factory=list,
        description="Full replacement set of instance memberships for this user.")


def _memberships(db: Session) -> tuple[dict[str, list[str]], dict[str, str]]:
    """(uid -> instance_ids granted via clinic_admins, instance_id -> name)."""
    names = {i.instance_id: i.instance_name
             for i in db.scalars(select(Instance))}
    granted: dict[str, list[str]] = {}
    for row in db.scalars(select(ClinicAdmin)):
        granted.setdefault(row.uid, []).append(row.instance_id)
    return granted, names


@router.get("")
def list_users(caller: dict = Depends(require_super_admin),
               db: Session = Depends(get_session)) -> dict:
    """Every Firebase user with their role claim and effective instance scope."""
    granted, names = _memberships(db)
    owned: dict[str, list[str]] = {}
    for i in db.scalars(select(Instance).where(
            Instance.primary_contact_uid.is_not(None))):
        owned.setdefault(i.primary_contact_uid, []).append(i.instance_id)

    users = []
    truncated = False
    for u in fb_auth.list_users().iterate_all():
        if len(users) >= MAX_USERS:
            truncated = True
            break
        role = (u.custom_claims or {}).get("role")
        # Ownership and explicit grants both confer access, so the UI must show
        # the union — showing only clinic_admins would hide a primary contact's
        # access and invite someone to "fix" it by adding a duplicate grant.
        scope = sorted(set(granted.get(u.uid, [])) | set(owned.get(u.uid, [])))
        users.append({
            "uid": u.uid,
            "email": u.email,
            "display_name": u.display_name,
            "disabled": u.disabled,
            "email_verified": u.email_verified,
            "providers": [p.provider_id for p in u.provider_data],
            "role": role,
            "instances": [{"instance_id": iid,
                           "instance_name": names.get(iid),
                           "via": ("owner" if iid in owned.get(u.uid, [])
                                   else "grant")}
                          for iid in scope],
            # super_admin ignores scope entirely; flagged so the UI can explain
            # why an empty instance list still sees everything.
            "scope_applies": role != "super_admin",
        })
    users.sort(key=lambda x: (x["role"] or "zzz", x["email"] or ""))
    return {"users": users, "count": len(users), "truncated": truncated}


@router.post("", status_code=201)
def create_user(body: UserCreate,
                caller: dict = Depends(require_super_admin),
                db: Session = Depends(get_session)) -> dict:
    """Create a user, set their role, and optionally grant instances.

    Ordering is deliberate: validate everything BEFORE calling Firebase. A
    Firebase user cannot be created transactionally with the Cloud SQL grants, so
    a late validation failure would leave an account with no role and no scope —
    exactly the broken state this endpoint exists to prevent.
    """
    if body.role not in ROLES:
        raise HTTPException(status_code=422,
                            detail=f"role must be one of {', '.join(ROLES)}")
    valid = {i.instance_id for i in db.scalars(select(Instance))}
    unknown = [i for i in body.instance_ids if i not in valid]
    if unknown:
        raise HTTPException(status_code=422,
                            detail=f"unknown instance_id(s): {', '.join(unknown)}")

    kwargs: dict = {"email": body.email, "email_verified": False}
    if body.display_name:
        kwargs["display_name"] = body.display_name
    if body.password:
        kwargs["password"] = body.password
    try:
        user = fb_auth.create_user(**kwargs)
    except fb_auth.EmailAlreadyExistsError:
        raise HTTPException(status_code=409,
                            detail=f"{body.email} already has an account")
    except ValueError as exc:  # malformed email / too-short password
        raise HTTPException(status_code=422, detail=str(exc))

    fb_auth.set_custom_user_claims(user.uid, {"role": body.role})

    # super_admin ignores scope, so writing grants for one would be misleading
    # state that a later demotion would silently activate.
    granted: list[str] = []
    if body.role != "super_admin" and body.instance_ids:
        for iid in sorted(set(body.instance_ids)):
            db.add(ClinicAdmin(uid=user.uid, instance_id=iid))
            granted.append(iid)
        db.commit()

    # No SMTP here: hand the admin a link to pass on rather than pretending an
    # invite email was sent.
    reset_link = None
    if not body.password:
        try:
            reset_link = fb_auth.generate_password_reset_link(body.email)
        except Exception as exc:  # noqa: BLE001 — account exists either way
            log.warning("reset link generation failed for %s: %s", body.email, exc)

    log.info("user created by=%s uid=%s email=%s role=%s instances=%s",
             caller["uid"], user.uid, body.email, body.role, granted)
    return {"uid": user.uid, "email": body.email, "role": body.role,
            "instances": granted, "password_reset_link": reset_link}


@router.delete("/{uid}")
def delete_user(uid: str,
                caller: dict = Depends(require_super_admin),
                db: Session = Depends(get_session)) -> dict:
    """Delete a user and their instance grants.

    Refuses two cases outright:
      * deleting yourself — self-inflicted lockout;
      * deleting an instance's `primary_contact_uid` — ownership is what
        `_is_instance_member` resolves access through, so removing the owner
        without reassigning it silently strips access for that instance.
    """
    if uid == caller["uid"]:
        raise HTTPException(status_code=400, detail="Refusing to delete yourself")

    owned = [i.instance_name or i.instance_id for i in db.scalars(
        select(Instance).where(Instance.primary_contact_uid == uid))]
    if owned:
        raise HTTPException(
            status_code=409,
            detail=("This user is the primary contact for "
                    f"{', '.join(owned)}. Reassign ownership first."))

    try:
        user = fb_auth.get_user(uid)
    except fb_auth.UserNotFoundError:
        raise HTTPException(status_code=404, detail="No such user")

    # Grants first: if Firebase deletion fails we are left with orphan rows for a
    # uid that still exists, which is recoverable. The reverse leaves grants
    # pointing at a deleted uid, which silently re-authorises a recreated uid.
    removed = list(db.scalars(select(ClinicAdmin).where(ClinicAdmin.uid == uid)))
    for row in removed:
        db.delete(row)
    db.commit()
    fb_auth.delete_user(uid)

    log.info("user deleted by=%s uid=%s email=%s grants_removed=%d",
             caller["uid"], uid, user.email, len(removed))
    return {"uid": uid, "email": user.email, "grants_removed": len(removed)}


@router.patch("/{uid}/role")
def set_role(uid: str, body: RoleUpdate,
             caller: dict = Depends(require_super_admin)) -> dict:
    """Set a user's capability level.

    Refuses self-demotion: a super_admin removing their own claim would lock
    themselves out of this very endpoint, and only another super_admin (or the
    Firebase console) could undo it.
    """
    if body.role not in ROLES:
        raise HTTPException(status_code=422,
                            detail=f"role must be one of {', '.join(ROLES)}")
    if uid == caller["uid"] and body.role != "super_admin":
        raise HTTPException(
            status_code=400,
            detail="Refusing to demote yourself — ask another super_admin.")
    try:
        user = fb_auth.get_user(uid)
    except fb_auth.UserNotFoundError:
        raise HTTPException(status_code=404, detail="No such user")

    previous = (user.custom_claims or {}).get("role")
    # Replaces the whole claims object, matching set-claims (api/v2/auth.py).
    # `role` must stay the only custom claim or this drops the others.
    fb_auth.set_custom_user_claims(uid, {"role": body.role})
    log.info("role change by=%s target=%s %s -> %s",
             caller["uid"], uid, previous, body.role)
    return {"uid": uid, "role": body.role, "previous": previous,
            # The claim lives in the ID token, so it does not take effect until
            # that token is refreshed — up to an hour, or immediately if the
            # user signs out and back in.
            "takes_effect": "on the user's next ID-token refresh (<=1h)"}


@router.put("/{uid}/instances")
def set_instances(uid: str, body: InstanceScope,
                  caller: dict = Depends(require_super_admin),
                  db: Session = Depends(get_session)) -> dict:
    """Replace a user's instance grants (`clinic_admins`).

    Ownership via `instances.primary_contact_uid` is NOT touched — that is a
    different relationship, and silently reassigning it here would change who
    owns a business as a side effect of a permissions edit.
    """
    valid = {i.instance_id for i in db.scalars(select(Instance))}
    unknown = [i for i in body.instance_ids if i not in valid]
    if unknown:
        raise HTTPException(status_code=422,
                            detail=f"unknown instance_id(s): {', '.join(unknown)}")

    existing = {r.instance_id: r for r in db.scalars(
        select(ClinicAdmin).where(ClinicAdmin.uid == uid))}
    wanted = set(body.instance_ids)

    for iid in wanted - existing.keys():
        db.add(ClinicAdmin(uid=uid, instance_id=iid))
    for iid in existing.keys() - wanted:
        db.delete(existing[iid])
    db.commit()

    log.info("instance scope change by=%s target=%s %s -> %s",
             caller["uid"], uid, sorted(existing.keys()), sorted(wanted))
    return {"uid": uid, "instances": sorted(wanted),
            "added": sorted(wanted - existing.keys()),
            "removed": sorted(existing.keys() - wanted)}
