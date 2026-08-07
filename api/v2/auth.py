"""`/v2/auth` — Firebase custom-claim assignment on sign-in.

Re-homed from the Next app's `src/app/api/auth/set-claims/route.ts`, which was
the SOLE writer of Firebase role claims. That made it a hard blocker for the Vite
SPA: existing users already carry a claim and work fine, but a brand-new user
would sign in successfully and then 403 on every data route, because nothing
assigned them a role. See `cortex-spa/src/context/AuthContext.tsx`.

Behaviour is a deliberate byte-for-byte port of the Next route so that the two
frontends can run in parallel and assign identical roles. In particular:

  * `@zoolstra.com` -> `super_admin`.
  * ANY existing role claim is PRESERVED, never downgraded — those are assigned
    by hand and must survive every sign-in.
  * Everyone else -> `viewer`.
  * `updated` reports whether a write actually happened, so the client knows
    whether it must force-refresh its ID token to see the new claim.

`set_custom_user_claims` REPLACES the whole custom-claims object rather than
merging. That is the Next route's behaviour and is kept, so `role` must remain
the only custom claim; adding a second one here without merging would silently
drop it on the next sign-in.
"""
from fastapi import APIRouter, Depends
from firebase_admin import auth as fb_auth

from api.deps import verify_token

router = APIRouter(prefix="/auth", tags=["auth"])

SUPER_ADMIN_DOMAIN = "zoolstra.com"

# Every role a super_admin can hand out via PATCH /v2/admin/users/{uid}/role.
# Kept in sync with admin_users.ROLES by the test suite rather than imported, so
# an auth-critical constant does not depend on an admin router's import graph.
ASSIGNABLE_ROLES = ("super_admin", "admin", "viewer")


@router.post("/set-claims")
def set_claims(caller: dict = Depends(verify_token)) -> dict:
    """Assign the caller's role claim, idempotently.

    Authorisation is the token itself: a caller can only ever act on their own
    uid, and the role is derived from the verified email rather than from
    anything the client sends. There is no request body on purpose — accepting a
    uid or a role would make this an escalation endpoint.
    """
    # verify_token already raised 401/500 as appropriate; a decoded token here is
    # trustworthy, and `email`/`role` come from Firebase, not from the client.
    uid = caller["uid"]
    email = caller.get("email") or ""
    current = caller.get("role")

    if email.endswith(f"@{SUPER_ADMIN_DOMAIN}"):
        if current == "super_admin":
            return {"role": "super_admin", "updated": False}
        fb_auth.set_custom_user_claims(uid, {"role": "super_admin"})
        return {"role": "super_admin", "updated": True}

    # Non-Zoolstra users: preserve ANY manually-assigned role, super_admin
    # included. Listing only admin/viewer here silently demoted every external
    # super_admin to viewer on their next sign-in — the claim was written
    # correctly by the admin UI and then clobbered before the dashboard loaded.
    # Only set viewer for brand-new users with no role yet.
    if current in ASSIGNABLE_ROLES:
        return {"role": current, "updated": False}

    fb_auth.set_custom_user_claims(uid, {"role": "viewer"})
    return {"role": "viewer", "updated": True}
