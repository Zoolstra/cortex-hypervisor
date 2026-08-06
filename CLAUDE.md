# cortex-hypervisor — FastAPI Backend

## Overview

REST API for clinic and user management. Account/config data lives in **Cloud SQL (MySQL 8, SQLAlchemy + Alembic)** — the `clinics` table and its per-clinic config children (`clinic_blueprint_config`, `clinic_counselear_config`, `clinic_worklist_taxonomy`, voice-agent tables, …). BigQuery is used for analytics/PHI reads (ETL-written `ClinicData.*`, `Blueprint_PHI.*`) and the `Users.phi_access_log` audit trail. Firebase handles authentication. (Historical note: the config store was migrated off BigQuery to Cloud SQL — ignore older "BigQuery is the only data store" phrasing.)

## Commands

```bash
uvicorn api:app --reload                              # Dev server (port 8000)
python -m pytest test_api.py -v                      # Run all tests
python -m pytest test_api.py::ClassName::method -v -s # Run single test
```

## Stack

- FastAPI, Python 3.12, Pydantic v2
- Google BigQuery (sole data store — all reads/writes go through BQ)
- Firebase Admin SDK (token verification + custom claims for roles)

## Project Layout

```
api/
  __init__.py         # FastAPI app, CORS middleware, router registration
  models.py           # All Pydantic request/response models
  deps.py             # Shared dependencies: BQ client, auth helpers, bq_insert/update/delete
  routers/
    instance.py       # Instance provisioning and lookup
    clinics.py        # Clinic CRUD
    staff.py          # Staff CRUD
    services.py       # Service CRUD
    insurance.py      # Insurance CRUD
    users.py          # Instance user management
    appointment_types.py
    review_snapshots.py   # GBP review snapshot ingestion
    websites.py           # STUB — not implemented, remove from __init__.py (see 2-E)
    blueprint.py          # STUB — not imported yet, do not use
```

## Active Routers (registered in `__init__.py`)

| Router | Base path |
|---|---|
| instance | `/provision_account/`, `/instance/{uid}` |
| clinics | `/clinics/{instance_id}`, `/clinics/{clinic_id}` |
| staff | `/staff/{instance_id}`, `/staff/{instance_id}/{clinic_id}/{name}` |
| services | `/services/{instance_id}`, `/services/{service_id}` |
| insurance | `/insurance/{instance_id}`, `/insurance/{insurance_id}` |
| users | `/users/{instance_id}`, `/users/{uid}` |
| appointment_types | `/appointment_types/{instance_id}`, `/appointment_types/{appointment_type_id}` |
| review_snapshots | `/review_snapshots/{instance_id}` |
| worklist_taxonomy (`api/account/worklist_taxonomy.py`) | `GET/PUT /clinics/{clinic_id}/worklist-taxonomy` — per-clinic reactivation cohort config (JSON on `clinic_worklist_taxonomy`, validated by `WorklistTaxonomyConfig`). Consumed by `api/worklists.py`: `/worklists/cohorts`, `/worklists/cohort/{key}`, `/worklists/cohort/{key}/export.csv` (contact CSV, super_admin+admin, PHI-audit-logged), `/worklists/pms-taxonomy` (discovery). |

`websites.router` is imported and registered but the router is empty — remove it (see Pending Work 2-E).

## `deps.py` — Shared Utilities

All routers import from `deps.py`. Do not instantiate a BigQuery client anywhere else.

```python
bq_client          # Single BigQuery client instance
bq_table(table)    # Returns backtick-quoted `PROJECT.DATASET.table`
bq_insert(table, rows)           # Parameterized INSERT
bq_update(table, where, updates) # Parameterized UPDATE, raises 409 on streaming buffer
bq_delete(table, where)          # Parameterized DELETE, raises 409 on streaming buffer
get_instance_id_or_404(...)      # Lookup helper with 404
verify_token(token)              # Firebase ID token verification (FastAPI dependency)
require_read_access(instance_id, caller)
require_write_access(instance_id, caller)
get_instance_id_for_uid(uid)
```

**Planned addition (3-C):** `bq_select(table, where) -> list[dict]` — eliminates the identical 6-line SELECT pattern repeated across all routers.

## Auth & Roles

Firebase custom claims control access:
- `super_admin` — all instances, all operations
- `admin` — write access to their instance only
- `viewer` — read access to their instance only

Every route that touches instance data must call `require_read_access` or `require_write_access` — never skip this.

## BigQuery Tables

Managed by this service (in the `Users` dataset):
- `instances`, `clinics`, `staff`, `services`, `insurance`, `users`, `appointment_types`, `review_snapshots`

Read-only from this service (written by ETL):
- `ClinicData.transactions`, `ClinicData.ad_clicks_v2`, `Blueprint.*`

## Data Models (`models.py`)

Key models:
- `InstanceCreate` / `Instance` / `InstanceUpdate`
- `ClinicCreate` / `Clinic` / `ClinicUpdate`
- `StaffUpdate`, `ServiceUpdate`, `InsuranceUpdate`
- `AppointmentType` / `AppointmentTypeUpdate`
- `ProvisionRequest` — full instance + clinics + staff + services + insurance in one call
- `ReviewSnapshot`
- `PatientCreate`, `AppointmentCreate`, `InvoiceCreate`, `PhysicianReferralCreate` — Phase 3 stubs, not yet wired to routes

### Clinic model — fields being added

```python
# Voice agent (opt-in per clinic)
voice_agent_status: Literal["inactive", "provisioning", "active", "error"] = "inactive"
twilio_phone_number: str | None = None   # E.164 format
twilio_phone_sid: str | None = None
twilio_verified_caller_id: bool = False
vapi_assistant_id: str | None = None
vapi_phone_number_id: str | None = None

# Blueprint OMS PMS integration (opt-in per clinic)
blueprint_server: str | None = None       # e.g. "wp2.bp-solutions.net:8443"
blueprint_clinic_slug: str | None = None  # [CLINIC] path segment
blueprint_api_key: str | None = None      # never logged
blueprint_location_id: int | None = None
blueprint_user_id: int | None = None      # service account user for API writes

# PMS type (supports future PMS systems)
pms_type: Literal["none", "blueprint"] = "none"
```

### Known type bugs (4-B)
- `Service.duration_minutes` is `str` — should be `int`
- `Service.cost` is `str` — should be `float`
- `ReviewSnapshot.validate_required` passes literal `"field"` to `_require_non_empty` — use `info.field_name`

## Pending Work

### Must fix (blockers)
- **2-C** `instance.py:57–63` — provisioning writes to 5 BigQuery tables with no rollback. Extract to `services/provisioning.py`; add best-effort compensating deletes on failure. Document that BigQuery does not support multi-table transactions.
- **2-D** `deps.py` — `verify_token` has a broad `except Exception → 401` after the specific Firebase exceptions. Re-raise unexpected exceptions as 500 so real bugs aren't masked.
- **2-E** `__init__.py` — remove `websites` from imports and `app.include_router(websites.router)`. Add `review_snapshots` router if not already registered.

### Structural refactoring
- **3-C** Add `bq_select(table, where) -> list[dict]` to `deps.py`. Replace the identical 6-line SELECT boilerplate in `clinics.py`, `staff.py`, `services.py`, `insurance.py`, `users.py`.

### New routers planned

| Router file | Purpose |
|---|---|
| `voice_agent.py` | `POST /clinics/{clinic_id}/voice_agent/activate`, `DELETE`, `POST .../verify_caller_id` |
| `pms_config.py` | Blueprint OMS credentials per clinic |
| `scripts.py` | Call scripts per clinic per call type |
| `campaigns.py` | Multi-campaign ID management per clinic (new table, old column kept) |

## Cloud Run Jobs (production)

The **same image** that serves the API also runs as a job with an overridden
`--command` — `Dockerfile` ships `scripts/` for exactly this reason, so there is
no second image to keep in sync.

| Job | Command | Schedule (PT) | Purpose |
|---|---|---|---|
| `payload-prewarm` | `python scripts/prewarm_payloads.py` | hourly (`20 * * * *`, `payload-prewarm-hourly`) | Warm the intelligence JSON payload cache so the first real visitor after a data-version rotation never pays the cold cost. 13 clinics × (4 windows + biweekly) + 1 group × 4 = **69 requests**; ~15 min fully cold, ~1 min when already warm. |

Why **hourly** and not pinned to `blueprint-sync`: the cache key rotates on the
PMS snapshot date (see `_data_version` in `api/intelligence.py`), and an hourly
run is self-healing — it needs no knowledge of when any upstream sync actually
landed or retried, and a warm run is a cheap no-op because the job hits the same
data-versioned keys the SPA does.

No env vars or secrets are set on the job: it runs as the same service account as
the service (`cortex-hypervisor-sa`) and fetches secrets at runtime through
`api/core/secrets.py`, reaching Cloud SQL through the connector.

```bash
# Deploy from the current API image (get DIGEST from Artifact Registry):
gcloud run jobs deploy payload-prewarm \
  --image="us-docker.pkg.dev/$PROJECT_ID/cortex-hypervisor/cortex-hypervisor@$DIGEST" \
  --command=python --args=scripts/prewarm_payloads.py \
  --service-account=cortex-hypervisor-sa@$PROJECT_ID.iam.gserviceaccount.com \
  --region=us-central1 --task-timeout=3600 --max-retries=1 --memory=1Gi

gcloud run jobs execute payload-prewarm --region=us-central1   # run now
```

**Redeploy the job after deploying the service** if the prewarm script or the
cache-key logic changed — the job pins an image digest, so `./dev.sh` alone
leaves it on the old build.

## Environment Variables (`.env`)

```
GCP_PROJECT=
BQ_DATASET=
GCS_SERVICE_ACCOUNT=     # JSON string of GCS service account
FIREBASE_ADMIN_SERVICE_ACCOUNT=  # JSON string of Firebase admin service account
ALLOWED_ORIGINS=         # Comma-separated, e.g. "https://app.example.com,http://localhost:3000"
```
