"""
`/v2` — the mart-backed serving layer.

Additive by construction: every v1 route is untouched, so the live dashboard and
the external datafeed are unaffected. The SPA can move one reader at a time from
v1 to v2, with the parity harness proving each swap returns identical numbers.

ROUTER REGISTRATION ORDER MATTERS. api/__init__.py must include this router
BEFORE the voice-agent and account routers: those declare root-level wildcards
like GET /{clinic_id}/availability which would otherwise swallow /v2/... paths
(binding clinic_id="v2"). An all-literal "/v2" prefix registered first makes that
collision class impossible — this codebase has already been bitten by it once,
which is why api/__init__.py carries a comment about registration order.
"""
from fastapi import APIRouter

from api.v2 import admin_users, auth, intelligence

router = APIRouter(prefix="/v2", tags=["v2"])
router.include_router(intelligence.router)
router.include_router(auth.router)
router.include_router(admin_users.router)
