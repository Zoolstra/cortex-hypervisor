import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.account import routers as account_routers
from api.voice_agent import routers as voice_agent_routers
from api.intelligence import router as intelligence_router

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
