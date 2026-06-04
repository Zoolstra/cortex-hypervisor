import logging
import os
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Application logging — configured before any router imports so
# module-level ``log = logging.getLogger(__name__)`` calls pick up the
# root handler this installs. uvicorn manages its own ``uvicorn.*``
# loggers separately; this only governs our ``api.*`` loggers.
#
# Pinned to ``sys.stdout`` so app logs land in the same Cloud Logging
# stream as uvicorn's access log (run.googleapis.com/stdout). Python's
# default for basicConfig is ``sys.stderr``, which splits app logs
# into a separate stream from the access log — annoying for debugging.
#
# Override level per-environment with the ``LOG_LEVEL`` env var (e.g.
# ``LOG_LEVEL=DEBUG`` for noisier local runs).
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger(__name__).info(
    "cortex-hypervisor logging configured (level=%s)",
    logging.getLogger().level,
)

from api.account import routers as account_routers  # noqa: E402 — after basicConfig
from api.voice_agent import routers as voice_agent_routers  # noqa: E402
from api.intelligence import router as intelligence_router  # noqa: E402

app = FastAPI()

_raw = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000")
_allowed_origins = [o.strip() for o in _raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def hello():
    return {"message": "This is the Cortex Hypervisor"}


# Voice agent routers MUST be registered before account routers. The clinics
# router (in account) declares a wildcard GET /clinics/{instance_id}/{clinic_id}
# that otherwise swallows any 2-segment voice-agent route (e.g.
# GET /clinics/{id}/voice_agent) and 404s on the literal "voice_agent" being
# looked up as a clinic_id. FastAPI tries routes in registration order;
# voice-agent's literal-segment routes are more specific, so they go first.
for r in voice_agent_routers + account_routers + [intelligence_router]:
    app.include_router(r)
