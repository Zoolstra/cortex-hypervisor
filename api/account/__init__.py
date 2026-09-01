"""
Account domain — instance / clinic / PMS / campaign config endpoints.

Each module exports a `router` (FastAPI APIRouter). The package-level `routers`
list is what `api/__init__.py` registers with the app.
"""
from api.account.instances import router as instances_router
from api.account.clinics import router as clinics_router
from api.account.customerio_config import router as customerio_config_router
from api.account.pms_config import router as pms_config_router
from api.account.readiness import router as readiness_router
from api.account.campaigns import router as campaigns_router
from api.account.worklist_taxonomy import router as worklist_taxonomy_router

# Order matters — Starlette matches in registration order. pms_config,
# customerio_config and worklist_taxonomy must come BEFORE clinics so
# `GET /clinics/{clinic_id}/pms`, `.../customerio` and `.../worklist-taxonomy`
# resolve to their handlers, not the generic
# `GET /clinics/{instance_id}/{clinic_id}` (which would parse the literal
# segment as a clinic_id and 404).
routers = [
    instances_router,
    # Before clinics_router for the same reason as pms_config: its paths sit
    # under /instances/{id}/… and /clinics/{id}/… prefixes.
    readiness_router,
    pms_config_router,
    customerio_config_router,
    worklist_taxonomy_router,
    clinics_router,
    campaigns_router,
]
