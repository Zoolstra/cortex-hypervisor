# cortex-hypervisor — FastAPI Backend

## Overview

REST API for clinic and user management, the intelligence-report engine, the voice-agent lifecycle, and the external data feed.

Account/config data lives in **Cloud SQL (MySQL 8.4, SQLAlchemy 2.0 + Alembic, database `clients`)** — `instances`, `clinics` and their 1:1/1:N children (`clinic_location_details`, `clinic_voice_agent_configuration` and the other voice-agent tables, `clinic_worklist_taxonomy`, `instance_pms_config` + `pms_clinic_locations`, `google_ads_campaigns` / `invoca_campaigns` / `jotform_forms` + `jotform_form_locations`, `clinic_admins`). Schema at alembic head `0032`.

BigQuery holds analytics and PHI — **read** for `ClinicData.*` / `Blueprint_PHI.*` / `CounselEar_PHI.*` (usually via the `PMS_Unified.*` views), and **written** for exactly five tables: `ClinicData.webforms`, `ClinicData.call_outcome_overrides`, `ClinicData.faq_embeddings`, `Users.voice_agent_tickets` and the `Users.phi_access_log` audit trail. The dashboard's `/v2` readers use a second Cloud SQL database, `marts`. Firebase handles authentication.

(Historical note: the config store was migrated off BigQuery to Cloud SQL in 2026-05. Ignore older "BigQuery is the only data store" phrasing anywhere in `resources/`.)

## Commands

```bash
venv/bin/uvicorn api:app --reload --port 8000    # Dev server — needs Cloud SQL IAM auth (root ../dev.sh auth)
venv/bin/python -m pytest tests -q               # Run all tests (33 modules, ~670 tests, ~35s)
venv/bin/python -m pytest tests/test_v2_auth.py::test_new_external_user_gets_viewer -v -s
alembic upgrade head                             # Migrate Cloud SQL (online only — see alembic/env.py)
./dev.sh                                         # ⚠️ DEPLOY: build + push + `gcloud run services update` — not a dev helper
python configure_jotform.py --discover|--with-utm|--locations [--apply]  # Jotform registry drift / hidden UTM fields / location map
python configure_promo_numbers.py                # Sync Invoca promo numbers → invoca_promo_numbers
```

The venv here is `venv/` (no dot). The ETL repo's is `.venv/` — they differ.

### Running tests without live ADC

`api/core/secrets.py` builds its client at import time and `deps.py` fetches the Firebase SA at import time, so `pytest` needs working ADC (`gcloud auth application-default login`) and hits Secret Manager on every run. **No stub is checked in** — there is no `conftest.py` and no pytest config file in this repo, so the fix below has to be written locally when you need it. When ADC is stale, a pytest plugin that replaces `secretmanager.SecretManagerServiceClient` in `pytest_configure` (plugins load before collection) unblocks the suite — the fake must return a service account whose `private_key` is a real generated RSA key, or `credentials.Certificate(...)` rejects it.

## Stack

- FastAPI, Python 3.12, Pydantic v2
- **Cloud SQL (MySQL 8.4)** — the account/config store. SQLAlchemy 2.0 ORM (`api/core/orm.py`), engine + IAM-auth connector in `api/core/db.py` (instance `project-demo-2-482101:us-central1:cortex-accounts`, database `clients`), schema managed by Alembic (`alembic/versions/`, 32 migrations, head `0032`).
- **Google BigQuery** — analytics and PHI reads plus the five application writes listed above.
- Firebase Admin SDK (token verification + custom claims for roles)
- VAPI (`vapi_server_sdk`) for the voice agent; Twilio for staff SMS alerts; Anthropic Claude for report narrative.

## Project Layout

```
api/
  __init__.py         # FastAPI app, CORS, logging; router registration — ORDER MATTERS
                      #   (v2 first, then voice_agent, worklists, account, intelligence,
                      #    webforms, datafeed; see the comment in the file)
  deps.py             # bq_client, bq_table, verify_token, require_read/write_access
  models.py           # Pydantic request bodies (config / provisioning / webform shapes)
  audit.py            # PHI access audit log -> Users.phi_access_log
  intelligence.py     # /intelligence/* dashboard payloads + the payload cache (21 routes)
  worklists.py        # /clinics/{id}/worklists/* reactivation cohorts, CSV export,
                      #   Customer.io sync (12 routes)
  webforms.py         # POST /webforms (JSON relay from our own sites), GET /webforms/coverage.
                      #   Jotform forms are ingested by the ETL's jotform-ingest job, not here.
  datafeed.py         # GET /datafeed/v1/{instance_id}/* — external client data feed
  core/
    db.py             # Cloud SQL engine/session (connector + IAM auth)
    orm.py            # SQLAlchemy models — every Cloud SQL table (24)
    secrets.py        # Secret Manager get_secret() (lru_cache'd)
    grouping.py       # is_multi_location() — the derived multi-location rule
  account/            # instances, clinics, campaigns, pms_config, customerio_config,
                      #   worklist_taxonomy, readiness  (+ provisioning.py, not a router)
  v2/                 # auth (set-claims), admin_users, provision, intelligence; marts.py (query module)
  voice_agent/        # voice_agent.py + blueprint.py routers; factory / roles / vapi /
                      #   twilio / capabilities / faq_retrieval / appointment_decision services;
                      #   pms/ (PMSAdapter + adapters); protocols/ (14 registered)
  services/           # customerio.py (Track API), notify.py (staff SMS/email on tickets)
intelligence_report/  # BigQuery readers + payload builders (queries, payloads,
                      #   group_aggregate, group_queries [unused], active_leads,
                      #   clinic_hours, report, transcripts, load_geo_targets, prewarm)
scripts/              # prewarm_payloads, parity_harness, copy_pms_secrets_to_instance,
                      #   resync_acna_assistant, …
alembic/versions/     # 32 migrations, head 0032
tests/                # 33 pytest modules
```

## Cloud SQL tables (`api/core/orm.py`, database `clients`)

| Group | Tables |
|---|---|
| Account | `instances`, `clinics`, `clinic_location_details` (1:1, holds the seven `hours_<weekday>` strings + `time_zone`), `clinic_admins` (uid × instance grants — **no clinic_id**) |
| PMS | `instance_pms_config` (account credentials-config + `primary_clinic_id`, keyed `(instance_id, pms_type)`), `pms_clinic_locations` (vendor location → clinic, `prompt_for_location`, `booking_user_id`), `clinic_blueprint_config` / `clinic_counselear_config` (**deprecated by 0030, read by nothing**) |
| Voice agent | `clinic_voice_agent_configuration`, `clinic_voice_agent_script`, `..._persona`, `..._caller_bucket`, `..._qualifying_question`, `..._faq`, `clinic_protocols` (source of truth), `voice_agent_capabilities` (legacy, dual-written for rollback), `clinic_blueprint_entity_note` |
| Campaigns / leads | `google_ads_campaigns`, `invoca_campaigns`, `invoca_promo_numbers`, `jotform_forms`, `jotform_form_locations` (0032) |
| Worklists | `clinic_worklist_taxonomy`, `customerio_enrollments` |

Schema is Alembic-managed: `alembic/versions/`, 32 migrations, head `0032`. Migrations run **online only** against the live instance with IAM auth (`alembic upgrade head`); offline mode is refused in `alembic/env.py`. `./dev.sh` ships the image only — migrate separately.

`clinics.deleted_at` is a soft delete: **every** query must filter `deleted_at IS NULL`. `clinics.pms_type` is `Enum("blueprint", "counselear", "audit_data", "none")`.

## Routers (126 application routes)

Registration order in `api/__init__.py` is load-bearing: `/v2` first (all-literal prefix), then voice-agent and worklists (literal segments), then account (whose `GET /clinics/{instance_id}/{clinic_id}` wildcard would otherwise swallow them).

| Router (module) | Paths |
|---|---|
| v2.auth | `POST /v2/auth/set-claims` |
| v2.admin_users | `GET/POST /v2/admin/users`, `PATCH /v2/admin/users/{uid}/role`, `PUT /v2/admin/users/{uid}/instances`, `DELETE /v2/admin/users/{uid}` |
| v2.provision | `POST /v2/admin/provision` |
| v2.intelligence | `GET /v2/intelligence/{clinic_id}/overview` — mart-backed (Cloud SQL `marts.*` via `api/v2/marts.py`), additive to v1 |
| voice_agent.voice_agent | `GET/POST/DELETE /clinics/{clinic_id}/voice_agent` + `/activate`, `/assistant`, `/verify_caller_id`, `/tickets`, `/capabilities[/{id}]`, `/script`, `/persona`, `/caller_buckets[/{id}]`, `/qualifying_questions[/{id}]`, `/faqs[/{id}]`, `/faqs/import`, `/faq/search` |
| voice_agent.blueprint | `/blueprint/{clinic_id}/…` — clinic-config, patient lookup/match/journal, appointment-types, availability(+find/search), appointments book/confirm/cancel/reschedule/locate, appointment-decision, locations, notes, placeholder/* (19 routes) |
| worklists | `GET /clinics/{clinic_id}/worklists/{cohorts,cohort/{key},cohort/{key}/export.csv,pms-taxonomy,callscoring-categories,fitting-no-purchase,lapsed-patients,qualified-leads,recall-due,upgrade-candidates,warranty-expiring}`, `POST /clinics/{clinic_id}/worklists/cohort/{key}/customerio-sync` |
| account.instances | `POST /provision_account/`, `GET/DELETE /instance/{uid}`, `PATCH /instance/{instance_id}`, `GET /instances`, `GET /instances/{instance_id}` |
| account.readiness | `GET /instances/{instance_id}/readiness` |
| account.pms_config | `GET/POST/DELETE /instances/{id}/pms`, `GET /instances/{id}/pms/discover`, `POST /instances/{id}/pms/locations/import`, `GET /clinics/{clinic_id}/pms` (read-only) |
| account.customerio_config | `GET/POST/DELETE /clinics/{clinic_id}/customerio` |
| account.worklist_taxonomy | `GET/PUT /clinics/{clinic_id}/worklist-taxonomy` |
| account.clinics | `GET/POST /clinics/{instance_id}`, `GET /clinics/{instance_id}/{clinic_id}`, `PATCH/DELETE /clinics/{clinic_id}`, `GET /clinics/{clinic_id}/etl_status`, `GET /clinics/{clinic_id}/instance` |
| account.campaigns | `GET /campaigns/{instance_id}[/{clinic_id}]`, `POST /campaigns/{clinic_id}`, `DELETE /campaigns/{campaign_type}/{campaign_id}`, `GET /campaigns_catalog/{campaign_type}/{instance_id}`, `GET /campaigns/{instance_id}/jotform/locations`, `GET/PUT /campaigns/jotform/{form_id}/locations` |
| intelligence | `GET /intelligence/{clinic_id}/{overview,biweekly,calls,active-leads,pipeline-revenue,leak-calls}`, `PUT /intelligence/{clinic_id}/calls/{call_id}/outcome`, `GET /intelligence/{clinic_id}/calls/{call_id}/transcript`, `POST /intelligence/{clinic_id}/patients/search`, `GET /intelligence/{clinic_id}/patients/{patient_key}/journey`, four `*.html` drill-downs + `report.html`, and `GET /intelligence/group/{instance_id}/{overview,calls,active-leads,pipeline-revenue}` |
| webforms | `POST /webforms`, `GET /webforms/coverage` (the Jotform webhook relay was retired 2026-09-04) |
| datafeed | `GET /datafeed/v1/{instance_id}/{dictionary,google-ads/campaigns,google-ads/ad-groups,google-ads/clicks,invoca/transactions,invoca/callscoring}` |

The `staff`, `services`, `insurance`, `users`, `appointment_types`, `review_snapshots` and `websites` routers were deleted along with their tables.

## `deps.py` — Shared Utilities

All routers import auth helpers from `deps.py`. Do not instantiate a BigQuery client anywhere else.

```python
PROJECT = "project-demo-2-482101"
DATASET = "Users"
bq_client          # the single BigQuery client (ADC)
bq_table(table)    # backtick-quoted `PROJECT.Users.table` — Users dataset ONLY;
                   #   ClinicData / Blueprint_PHI callers build their own refs
verify_token(token)              # Firebase ID token verification (FastAPI dependency)
require_read_access(instance_id, caller)
require_write_access(instance_id, caller)
get_instance_id_for_uid(uid)     # reads Cloud SQL
```

There are no `bq_insert` / `bq_update` / `bq_delete` helpers — config CRUD is SQLAlchemy against Cloud SQL (`api/core/db.py::get_session`, `api/core/orm.py`). The handful of BigQuery writes each build their own statement: `insert_rows_json` in `api/webforms.py` and `api/audit.py`, DML in `api/voice_agent/voice_agent.py` (tickets), `intelligence_report/queries.py::set_call_outcome_override` (the append-only override INSERT behind `PUT /intelligence/{clinic_id}/calls/{call_id}/outcome`), and `api/voice_agent/faq_retrieval.py` (a MERGE — DML deliberately, so the streaming buffer never blocks a rewrite).

## Auth & Roles

Access is **two independent things**, and conflating them causes real bugs:

1. **`role` — a Firebase custom claim.** The capability level. It is GLOBAL:
   an `admin` claim means "may write", not "may write to instance X".
2. **Instance membership — rows in Cloud SQL.** The scope. A non-super_admin
   reaches an instance only via `instances.primary_contact_uid` (ownership) or a
   `clinic_admins(uid, instance_id)` row.

So `admin` + no membership can do nothing, and that state is easy to create by
accident. Any user-facing permissions UI must show both.

| role | read | write |
|---|---|---|
| `super_admin` | all instances, unconditional | all instances, unconditional |
| `admin` | member instances only | member instances only |
| `viewer` | member instances only | denied |

Every route that touches instance data must call `require_read_access` or
`require_write_access` (`deps.py`) — never skip this.

**Scope is per-INSTANCE, never per-clinic.** `clinic_admins` has no `clinic_id`
and `deps.py` never references one; clinic routes authorise via
`clinic.instance_id`. A user granted a multi-location instance therefore sees ALL
of its clinics. Per-clinic visibility would need a schema change plus a new check
in `require_read_access` — do not assume it exists.

### Claim assignment

| Route | Who | Effect |
|---|---|---|
| `POST /v2/auth/set-claims` | any signed-in user, self only | `@zoolstra.com` → `super_admin`; others → `viewer` on first sign-in; any existing hand-assigned role (`super_admin`/`admin`/`viewer`) is preserved. Takes no body — accepting a uid or role would make it an escalation endpoint. |
| `GET /v2/admin/users` | super_admin | Every user with role + effective instance scope (union of ownership and grants). |
| `POST /v2/admin/users` | super_admin | Creates the account, sets the role, applies grants. Validates everything *before* calling Firebase — a late failure would leave an account with no role and no scope. Omit `password` to get back a `password_reset_link`; there is no SMTP here, so the admin forwards it. Grants are dropped for `super_admin`, which is not scope-limited. |
| `PATCH /v2/admin/users/{uid}/role` | super_admin | Sets the claim. Refuses self-demotion (that would lock you out of the endpoint that undoes it). |
| `PUT /v2/admin/users/{uid}/instances` | super_admin | Replaces the `clinic_admins` grant set. Does NOT touch `primary_contact_uid` — ownership is a different relationship. |
| `DELETE /v2/admin/users/{uid}` | super_admin | Removes grants, then the Firebase user (that order leaves recoverable orphan rows on failure rather than grants pointing at a reusable uid). Refuses self-deletion, and refuses any user who is an instance's `primary_contact_uid` — reassign ownership first. |
| `POST /v2/admin/provision` | super_admin | Stands up a client: get-or-create the primary contact's Firebase user, give them `admin` **only if they have no claim**, then `provision_full_account` (instance + clinics + their 1:1 sub-tables) in one transaction. Refuses a contact who already owns an instance (409) — `GET /instance/{uid}` returns one row per uid and cannot represent two. Returns a `password_reset_link` when it created the account; there is no SMTP, so the admin forwards it. |

The SPA surfaces all of this at `/admin/users` (`cortex-spa/src/routes/AdminUsers.tsx`),
reachable from a super-admin-only button on `/`. The client-side role check is
convenience; every call is gated server-side.

`set_custom_user_claims` REPLACES the whole custom-claims object, so `role` must
remain the only custom claim; adding a second without merging silently drops it.

A claim change only takes effect when the user's **ID token refreshes** (≤1h, or
immediately on sign-out/in) — role changes are not instant.

> **KNOWN GAP.** The `@zoolstra.com` → `super_admin` rule reads `email` without
> checking `email_verified`, and the project has `disabledUserSignup: false` with
> no blocking functions. Anyone can self-register an unowned `@zoolstra.com`
> address and be granted super_admin over every instance and all PHI. Requiring
> `email_verified` is NOT a safe unilateral fix: 3 of the 4 current Zoolstra
> accounts are unverified `password` accounts and would drop to `viewer`. Close
> it by disabling self-signup, or verify those 3 accounts first.

### Provisioning (`api/v2/provision.py`)

Re-homed from the deleted Next app's `/api/admin/provision` server route, which needed
the Firebase Admin SDK and so could not move to a static SPA — provisioning was
simply missing from cortex-spa until this landed.

The Next route ran outside the hypervisor, so to call `POST /provision_account/`
it had to *become* the target user: create/lookup the Firebase user, mint a
custom token, exchange it at the Identity Toolkit for an ID token using
`firebase-web-api-key`, then send that as the Bearer. In-process there is no HTTP
hop, so the custom token, the exchange, and that secret dependency are all gone;
the handler calls `provision_full_account` directly with the verified super_admin
caller. What the exchange implied is kept explicitly (existing role claims are
never overwritten; a uid that already owns an instance is refused).

Firebase and Cloud SQL cannot share a transaction, so **every refusal is checked
before Firebase is touched** — the common failure (already provisioned) creates
nothing. After that point a DB failure leaves an account with no instance, which
is recoverable and harmless: `admin` with no membership reaches nothing.

`ProvisionRequestV2.instance` also accepts the two **upstream account ids**
(`google_ads_customer_id`, `invoca_profile_id`). They are INSTANCE columns — one
ads account and one Invoca profile serve every location of a business — and the
ETL reads them to decide what to pull, so setting them at provisioning is what
lets a new client's data start flowing without a follow-up config pass. Both are
optional, and blank normalises to **NULL, never `""`**: the ETL tests these for
presence and an empty string is truthy in a SQL join. Edited afterwards through
the existing `PATCH /instance/{instance_id}`.

Two creation paths, deliberately separate endpoints:

| Need | Endpoint | Creates |
|---|---|---|
| New business | `POST /v2/admin/provision` | Firebase user + instance + its first clinics |
| New location for an existing client | `POST /clinics/{instance_id}` | clinic only |

Frontend: both live on the **clinic picker** (`cortex-spa/src/routes/ClinicPicker.tsx`,
at `/intelligence`), which already lists every business and its locations —
`ProvisionModal` in the page header, `AddClinicForm` under each business. They
share one `ClinicFields` form because both build the same `ClinicCreate` body.
A separate `/admin/instances` directory existed briefly and was removed: it
listed the same businesses and clinics, differing only in the chrome hung off
them. Note that the picker's single-clinic auto-redirect is suppressed for
super_admins, or a one-clinic tenant would strand them away from these buttons.

Both are edited after the fact under settings, and settings is **split by blast
radius** — which is the organising principle, not a detail:

| Scope | Route | Holds |
|---|---|---|
| Business | `/settings/instance/$instanceId` (`InstanceSettingsShell.tsx`) | Overview (config status + the location list), Details (`instance_name`, `google_ads_customer_id`, `invoca_profile_id`; Group Intelligence shown read-only since it is derived), PMS (account config + location map), Lead forms (Jotform location map) |
| Location | `/settings/$clinicId` (`ClinicSettingsShell.tsx`) | Overview (cards, read-only PMS summary, ETL status), Details (`clinic_name` / address / phone / time zone / **hours**), Customer.io, Campaigns, Worklists, Voice Agent |

The business fields used to be a panel on the *clinic* Details page, which meant
a save on what read as one location's settings changed every location. They are
now only reachable where they apply. The clinic shell's business name is a link
up; `ClinicPicker` carries a **Business settings** link per business, since
reaching business-wide config by first picking a location has the hierarchy
backwards.

`instance_name` and `clinic_name` are mutable — neither was in its update model
originally, so a typo from an onboarding call could only be fixed in Cloud SQL.
Renaming is safe: **nothing joins on a name.** Scope, FKs, mart keys
and the PMS secrets (`instance_{instance_id}_{pms}_{key}` — id-keyed) all key on
ids, and every other reader treats the name as a display label. Two spots are
*not* retroactive: the live VAPI assistant keeps the old name in its prompt until
republished, and `ClinicData.webforms` rows already written keep the name they
were stamped with (a point-in-time record, not a stale copy). Note
`_reject_empty_string` on `ClinicUpdate` / the None-drop in `update_instance`: a
value can be corrected but not blanked, which is why the UI only ever sends
fields that actually changed.

> **Clinic hours are load-bearing — do not "simplify" them out of the model.**
> `clinic_location_details.hours_<weekday>` has three live consumers:
> `_hours_block` in the voice-agent prompt (`api/voice_agent/factory.py`, also
> used by `roles.py`), the **Revenue-per-clinic-hour** KPI
> (`intelligence_report/payloads.py` + the group rollup, parsed by
> `intelligence_report/clinic_hours.py`), and the "is this clinic open right
> now" gate on active leads (`active_leads.py`). The KPI is inside the
> methodology parity freeze (dashboard-rework-plan §1.2).

### Onboarding readiness (`api/account/readiness.py`)

`GET /instances/{instance_id}/readiness` composes the checks that must pass for a business to actually ingest, in the order they must pass, and says what breaks when each does not. Every field can be individually valid while the business as a whole ingests nothing — onboarding one four-location client produced five failures of exactly that shape (locations unmapped, ETL off, a rollup gated on a flag nobody flipped, a sync never re-run after a mapping change, an account's config wiped by a blank save).

It is deliberately a **reader**: every rule it reports already exists somewhere that enforces it (`core.grouping` for the rollup, the PMS validators for the map, the sync's unassigned counting for attribution). Restating a rule here would create a second definition of healthy that could drift from the one the pipeline applies.

Statuses: `ok` | `warn` (works, but something downstream will be wrong or invisible) | `blocked` (nothing flows) | `unknown` (BigQuery unreachable — never a silent pass) | `skipped`.

## PMS configuration — account-level, for every vendor

A PMS login belongs to the **business**, not to a location: one account covers
however many sites the client has, and each site is a clinic here. So config is
instance-scoped for Blueprint and CounselEar alike (alembic 0030), and the only
per-clinic PMS config is which vendor location feeds the clinic.

| Table | Holds |
|---|---|
| `instance_pms_config` | One row per (instance, pms_type). Blueprint: `clinic_code`, `api_url`, `aws_url`. CounselEar: `counselear_location_code`, `counselear_sftp_username`. Plus `primary_clinic_id`. |
| `pms_clinic_locations` | Vendor location → clinic, plus the two settings that really are per-clinic: `prompt_for_location`, `booking_user_id`. |

Before 0030 the same thing could be configured in two places with the clinic
winning, which made the losing one invisible — a clinic wired directly kept
claiming its whole account's feed while the account config sat there looking
correct. Calgary is the worked example: one clinic reported five locations'
appointments and revenue as its own.

**There is no per-clinic PMS editor.** `GET /clinics/{clinic_id}/pms` is read-only
and answers "where does this clinic's data come from"; the writes are all
instance-scoped.

| Endpoint | Does |
|---|---|
| `GET /instances/{id}/pms?pms_type=` | Account config + location map + the instance's clinics |
| `POST /instances/{id}/pms` | Set config, secrets, fallback clinic, and the whole map |
| `DELETE /instances/{id}/pms?pms_type=` | Remove the account config and its map (clinics keep `pms_type`) |
| `GET /instances/{id}/pms/discover?pms_type=` | Ask the PMS which locations exist |
| `POST /instances/{id}/pms/locations/import` | Create a clinic per location and map it |
| `GET /clinics/{clinic_id}/pms` | Read-only: how this clinic is fed |

### The catch-all key

A single-location account still needs a map row, or "configured" and "ingesting"
come apart. `vendor_location_key = '*'` means *every row of this feed belongs to
this clinic*. It must be the **only** row for its account — a catch-all beside
specific ids has no defined meaning, so the API rejects the combination and the
ETL raises rather than double-loading every routable row. Importing real
locations removes it. Migration 0030 gave every pre-existing Blueprint clinic a
catch-all, which is deliberately behaviour-preserving: splitting an account is a
config change, not something a schema migration should do silently.

### Retired locations

`active=0` with `clinic_id` NULL records a site we have stopped ingesting. A
closed location's rows arrive in the shared feed indefinitely, and the ETL has to
tell them from a site nobody mapped — that one is a wiring gap it must report.
Deleting the row instead of retiring it collapses that distinction and leaves the
sync permanently `partial`.

### Write-time validation

`POST /instances/{id}/pms` rejects what the ETL would otherwise get wrong
*silently*: a location mapped twice (double ingest), an active location with no
clinic (routes nowhere), a retired location naming a clinic (records a mapping a
reader would act on), a clinic from another instance, a catch-all beside real
keys, a retired catch-all. It also sets `pms_type` on each mapped clinic —
the ETL scopes an account to clinics of the matching PMS, so a mapping to a
`pms_type='none'` clinic stores fine and ingests nothing. It deliberately does
**not** touch `etl_enabled` (that gates the analyses and marts too) and reports
`mapped_not_etl_enabled` instead.

`locations` replaces the whole map when present and is left alone when omitted,
so credentials can be saved without resending the map, and clearing it is an
explicit `[]`.

### Secrets

`instance_{instance_id}_{pms_type}_{key}`. Readers try that first and fall back
to a mapped clinic's `clinic_{clinic_id}_…` with a loud warning, because 0030
could not write Secret Manager — which is what let the migration land without
being sequenced against a secret copy.
`scripts/copy_pms_secrets_to_instance.py` (dry-run by default) retires the
fallback.

**CounselEar is deliberately exempt.** Its secrets are named after the SFTP login
(`{Username}_COUNSELEAR_SFTP_password`, from `provision_sftp.sh`), and that
username is now account config — so the account row already locates them and no
live credential was renamed. The endpoint accepts no CounselEar secrets.

### Onboarding: clinics come from the PMS

`POST /v2/admin/provision` takes the contact, the instance name, the Google Ads
and Invoca ids, and the **PMS account** — and normally **no clinics**. The
clinics come from the PMS afterwards:

1. `GET /instances/{id}/pms/discover` — ask the PMS what sites exist.
2. `POST /instances/{id}/pms/locations/import` — create a clinic per site (via
   `provision_clinic`, so each gets its 1:1 sub-tables and seeded voice-agent
   defaults) and map it.

The response from provision carries `next_step` naming exactly that.

> **Discovery is not guaranteed complete.** Blueprint's `clinicConfiguration`
> returns only locations enabled for online booking and its `name` can be unset,
> so a site that exists, bills and books by phone can be missing. The complete
> list is the `Location` table in the S3 data feed
> (`pms.blueprint.sync --discover`), which needs one sync to have run. The
> response states this rather than presenting a partial list as the whole truth.
> CounselEar has no locations endpoint at all — its per-row clinic ids come from
> a landed feed (`pms.counselear.api_backfill --verify-clinics`).

Pass `clinics` at provision time only for a business with no PMS, or to add a
location the PMS does not report.

### Deprecated

`clinic_blueprint_config` and `clinic_counselear_config` are read by nothing and
carry a table COMMENT saying so. Their rows are kept as 0030's rollback path and
are dropped in a follow-up. **Do not add readers.** (The ETL's
`count_legacy_pms_config` compares their row counts to the new tables purely to
tell "nothing configured" from "migration not run yet".) `configure_blueprint.py`
was removed with them — it wrapped the deleted per-clinic endpoint, and the
dashboard does the job.

Frontend: `cortex-spa/src/components/manage/InstancePmsSection.tsx` on
`/settings/instance/$instanceId/pms`. The map lists the locations
`clinicConfiguration` reports (fetched on load, not behind a button), so
assigning them to clinics does not depend on remembering to look. There is no
clinic-level PMS route — `ClinicPmsSummary.tsx` renders the read-only "how this
clinic is fed" panel on the clinic Overview instead, because a tab implies an
editor.

## Jotform lead forms — one form, many clinics

> **Ingestion lives in the ETL (since 2026-09-04).** `cortex-data-ingestion/app/jotform/`
> polls the Jotform API every 15 min (`jotform-ingest`) and owns the schema of
> record for `ClinicData.webforms`; `api/webforms.py::WEBFORMS_SCHEMA` is a
> mirror pinned to `tests/fixtures/webforms_schema.json` (byte-identical copy in
> the ETL repo — `tests/test_webforms.py` checks both). Rows carry
> `submission_id` / `ingest_source` / `ingested_at`. The webhook relay, its parser
> and the location resolver were deleted from this repo; their ETL versions are
> `jotform/parse.py` and `jotform/locations.py`. **This repo owns the registry,
> the location map and their routes; the ETL owns the rows.** History:
> `resources/jotform-api-polling-plan.md`.

A `jotform_forms` row names exactly one clinic (historically because the
webhook URL carried one `clinic_id`; now simply the form's DEFAULT clinic). That is wrong for a group running
every site off a single lead form: Sense of Hearing's appointment-request form
(262174010008038) serves all 14 Ontario locations, and of its first 78
submissions only 11 chose Burlington — wiring it to the group's one existing
clinic would have misattributed 86% of the leads.

`jotform_form_locations` (alembic 0032) maps each "choose your location" answer
to a clinic; the ETL's `jotform/locations.py::resolve_clinic` applies it and the
registry's default clinic becomes the fallback. Three things about it are deliberate:

- **The answer is matched verbatim, not parsed.** Option strings are marketing
  copy maintained in the Jotform builder and already disagree with our clinic
  names on 3 of those 14 ("Limestone Hearing Care Centre (Kingston)" vs. clinic
  *Kingston*, "Mississauga (Eglinton)" vs. *Mississauga Central*, "St Catharines
  West" vs. *St. Catharines West*). A name-matching resolver breaks silently on
  the next copy edit.
- **Matching is by value, across all fields.** Forms reveal several location
  dropdowns by condition — that form has four (adult, 6-17, APD,
  10-months-up) — and naming the fields means reconfiguring when a fifth
  appears.
- **`active=True` with a NULL `clinic_id` is legal here**, unlike
  `pms_clinic_locations` where an active row must route somewhere. A group's
  form lists every site from day one while the clinics are created over days;
  recording the option with no clinic is what makes that gap visible. Such a
  submission falls back to the form's clinic and the resolver logs it.

Maintain the map with `configure_jotform.py --locations` (dry-run by
default; `--apply` records unmapped options with no clinic, `--link-by-name`
also links options whose leading label is exactly a clinic name of the same
instance) or through `GET`/`PUT /campaigns/jotform/{form_id}/locations`.

Register a group's form only after its map exists — the leads that arrive in
between all land on the default clinic (recoverable: the verbatim answer is in
`raw_fields`).

**In the dashboard.** *Business settings → Lead forms* (`/settings/instance/{id}/lead-forms`,
`InstanceJotformSection.tsx`) is the editor; the per-clinic *Campaigns* tab keeps
the registry. Scope is shown, never set: a form is "Business-wide" when it has a
map and "One clinic" when it does not, computed by the API rather than stored, so
a flag can never disagree with the rows that actually route. The clinic holding a
group form's webhook sees it badged business-wide with the fallback explained;
the other clinics get a read-only "Lead forms shared with this clinic" panel
naming the exact answers that route to them — without which 13 of a 14-site
group look like they have no lead form at all. (Copy in these components used to
say "the clinic the webhook names"; it now says "the clinic the form is
registered under".)

## Intelligence payloads (`api/intelligence.py`) — the v1 read path

The dashboard's read path: 21 routes, 16 per-clinic and 5 group. Per clinic: `overview`, `biweekly`, `calls`, `webform-submissions` (the Web forms tab's per-submission list — every submission in the window with PMS-or-form name, email, clinic-local time, derived medium/source + raw UTM, and the first appointment created on/after it; `queries.webform_submission_detail`, same reconciliation CTEs as the `webforms` tiles; admin-only, audited as `webform_submissions`), `active-leads`, `pipeline-revenue`, `leak-calls`, `PUT calls/{call_id}/outcome` (manual relabel → append-only `ClinicData.call_outcome_overrides` via `intelligence_report/queries.py::set_call_outcome_override`, then a JSON-cache clear), `GET calls/{call_id}/transcript`, `POST patients/search`, `GET patients/{patient_key}/journey`, `report.html` and four `*.html` drill-downs. Group: `overview`, `calls`, `webform-submissions`, `active-leads`, `pipeline-revenue`, each gated on `is_multi_location` and 404 (not 403) for a single-location instance.

Heavy readers live in `intelligence_report/` (`queries.py` ≈ 7.9k lines for BigQuery, `payloads.py` for assembly, `group_aggregate.py` for the rollup, `active_leads.py`, `clinic_hours.py`). **`_call_tagging_cte` is the single shared per-call tagging CTE** — per-reader copies of tagging logic drift, which is why every funnel/table consumer composes this one. `MIN_WINDOW_DATE` and **`CALL_BOOKING_MATCH_DAYS = 10`** (widened from 3 in 2026-08 — bookings were being entered days after the call) are duplicated into `api/v2/marts.py` and `api/datafeed.py` (`_MATCH_DAYS`) rather than imported (importing pulls the BigQuery client into a SQLAlchemy-only layer); `tests/test_group_intelligence.py` pins the three together.

**Cache keys.** Cached payloads key on `(clinic scope, window, pms_type, data_version, _METHODOLOGY_VERSION)`. `_data_version(clinic_id)` is the PMS snapshot date; `_group_data_version(clinic_ids)` is the `|`-joined composite, so a rollup invalidates when ANY member's data lands. `pms_type` is in the key because connecting a PMS rotates no data version and the disclosure would otherwise persist for the life of the entry. A GCS-backed shared layer (`PAYLOAD_SHARED_CACHE=0` to disable) exists because Cloud Run runs `--workers 1` and scales to zero.

**Not cached:** `/calls`, `/webform-submissions` and every `*.html` route. Adding a cache tier to them is the prerequisite for warming them, not adding them to `scripts/prewarm_payloads.py`.

When changing a metric definition, change it here **and** in `cortex-data-ingestion/app/marts/` — `resources/methodology-contract.md` governs parity and `scripts/parity_harness.py` proves it.

### `/v2/intelligence` — the mart-backed reader

`GET /v2/intelligence/{clinic_id}/overview` returns the v1 overview shape with named sections served from the **Cloud SQL `marts` database** instead of BigQuery; `_mart_backed` in the response says which sections came from the marts (`MART_SECTIONS = ("call_funnel", "call_outcomes_monthly", "call_funnel.matthew_split")`), and everything else falls through to v1. Additive by construction, so the SPA can move one reader at a time with `scripts/parity_harness.py` proving each swap. It is the only `/v2` intelligence route today.

`api/v2/marts.py` holds the SQL. No second SQLAlchemy engine — the existing `clients` connection is reused and mart tables are schema-qualified (`marts.call_facts`), so the SA needs SELECT on `marts.*` (`cortex-data-ingestion/app/marts/README_GRANTS.sql`). Two invariants carried from the methodology contract: booking credit is window-relative and post-override, ranked per appointment `event_id`; and that rank is a SEMI-join, never a LEFT JOIN (a join fans out and inflates every count — this bug corrupted an impact report once). The `/v2` funnel does **not** implement `unconfirmed_booking`; it computes `qualified_not_booked` instead.

The marts themselves are built by the ETL (`marts-build` / `marts-rc-build`) and pushed to Cloud SQL by `marts-sync-cloudsql` — a BigQuery build is invisible to `/v2` until that sync runs. The hypervisor is SELECT-only on `marts.*`; the dual-write of `call_outcome_overrides_current` that `README_GRANTS.sql` grants for was never built, so a relabel reaches `/v2` at the next nightly sync.

**Registered FIRST** in `api/__init__.py`. The all-literal `/v2` prefix cannot swallow anything, and putting it ahead of the wildcard routers stops any future `/v2/...` path being captured by a root-level `GET /{clinic_id}/...` pattern.

## Group Intelligence (multi-location rollup)

`GET /intelligence/group/{instance_id}/overview` returns the **per-clinic
Overview payload aggregated across the instance's clinics** — the same shape as
`GET /intelligence/{clinic_id}/overview`, so the SPA renders both through the
same `OverviewView` and every clinic-page section exists on the group page by
construction. Gated on the instance having **two or more clinics** — 404 (not
403) for a single location, so the section is invisible rather than empty.

**Derived, not a setting** (alembic 0031, `api/core/grouping.py::is_multi_location`, threshold `>= 2`). It was a stored `instances.multi_location_group` flag a super_admin toggled, which restated what the data already said and could disagree with it: an instance provisioned with four locations kept 404ing its rollup because nobody flipped the switch.

Ten readers now use the derived rule — four rollup gates plus one payload field in `api/intelligence.py`, one in `api/v2/intelligence.py`, two in `api/account/readiness.py`, one in `api/account/clinics.py`, two in `api/account/instances.py`.

> ⚠️ **The stored column has two readers left, and they are wrong.**
> `scripts/prewarm_payloads.py` and `scripts/parity_harness.py` still
> select group instances with `Instance.multi_location_group.is_(True)`.
> Today three instances are multi-location by the derived rule (Calgary 4
> clinics, Virsono 7, Sense of Hearing 14) but only Virsono carries the flag,
> so **prewarm warms one group scope out of three** — Calgary's and Sense of
> Hearing's rollup pages stay permanently cold, exactly the failure this file
> describes below. Fix both call sites to use `is_multi_location` /
> `is_multi_location_for_count`. (`cortex-data-ingestion/app/db.py::get_matthew_clinics`
> reads the raw column on purpose — it scopes the Matthew transcript analysis,
> not the rollup. Clearing the flag would silently stop that analysis.)

Deleted clinics don't count. `etl_enabled` deliberately does **not** enter into
it — whether a business *has* several locations is a different question from
whether we're currently ingesting for them, and folding the second in would make
the rollup vanish mid-onboarding while clinics are switched on one at a time.

The column survives with a deprecating COMMENT rather than being dropped: its
values are the only record of which instances had the rollup deliberately OFF
while having several locations, which is the seed for a nullable override column
if that distinction is ever wanted back.

Merge rules live in `intelligence_report/group_aggregate.py`. Three kinds, and
picking the wrong one yields a plausible wrong number:

| Kind | Examples | Rule |
|---|---|---|
| Additive | calls, submissions, clicks, spend, revenue | sum |
| Derived | `booked_rate`, `capture_rate`, `avg_invoice`, `revenue_per_clinic_hour`, `roas`, every `*_delta` | **recompute from the summed components** — never average per-clinic rates, which weights a 12-call clinic like a 400-call one |
| Distinct people | `paid_attribution.matched_patients` / `invoiced_patients`, `webforms.appt_patients` | dedicated multi-clinic SQL (`queries.paid_call_revenue_group`, `webform_appointments_group`) |

**`client_id` is scoped to a clinic.** The same person at two locations has two
ids, and two different people can share an id across clinics. So every
cross-clinic PMS join keys on the composite `(_clinic_id, client_id)`, and
group-wide distinct-people counts key on the CALLER'S PHONE/EMAIL instead —
the only identity comparable across locations. Joining on `client_id` alone
cross-matches one clinic's patient to another clinic's appointments and
invoices. Same rule the older `group_queries.zoolstra_attribution` follows.

The one metric that is NOT deduplicated: `form_submissions.patients` /
`converted_patients` (CounselEar portal bookings) — that table carries no
cross-clinic key, so those are patient RECORDS and are summed. Disclosed to the
reader via `aggregation_notes` on the payload.

Cost: fans the per-clinic readers across every clinic, so ~N× a clinic page.
Cached on `_group_data_version` (the composite of its member clinics' data
versions, so the rollup invalidates when ANY member's data lands) and warmed by
`payload-prewarm`.

> Superseded the per-location leaderboard payload (revenue / avg-invoice /
> booked rankings, Zoolstra attribution, product + referral rollups, PMS
> coverage). `intelligence_report/group_queries.py` still exists but has **no
> production consumer** — safe to delete.

## PMS coverage on intelligence payloads

Clinics with `pms_type` outside `{blueprint, counselear}` (today: `none`, and
`audit_data` until that feed lands) have **no PMS integration**. Every
PMS-derived reader returns **zeros** for them rather than erroring — deliberate,
so a missing integration never blanks a page — which means an unlabelled report
shows "0 booked appointments" and "$0 revenue" for something nobody measured.

Two things are simply unknowable without the feed, and the reports must say so
rather than print a zero:

* **revenue** — invoices live in the PMS; there is no second source
* **traffic → bookings** — a call, ad click or form submission is tied to an
  appointment only by reconciling it against PMS appointment records

`payloads.pms_integrated()` is the single test (`PMS_WITH_DATA`). Every payload
carries `pms_type` / `pms_integrated` / `pms_caveat`; the group rollup adds
`pms_coverage` (`{clinics_total, clinics_integrated, missing}`) because a rollup
can be **partly** measurable — summing over the locations without a feed is
exactly what makes an incomplete revenue total look complete. The caveat is also
injected into both LLM prompts (`one_thing`, `forward_recommendations`), which
otherwise narrate the zeros as a revenue collapse.

`pms_type` is part of the overview / biweekly / group **cache keys**: it changes
what the report may claim, and connecting a clinic's PMS rotates no data version,
so without it the disclosure would persist for the life of the cache entry.

Frontend: `cortex-spa/src/components/intelligence/PmsCoverage.tsx` — a page-level
banner plus the inline replacements used wherever a suppressed figure sat. The
funnel stops at "connected", ROAS/revenue columns are dropped rather than
dashed, and the qualified-no-conversion leak is restated as an unverified
follow-up list (nothing can confirm those callers didn't book later).

## Client data feed (`api/datafeed.py`)

Read-only REST feed of one instance's own Google Ads and Invoca data for external clients, mounted at `GET /datafeed/v1/{instance_id}/…`: `dictionary`, `google-ads/{campaigns,ad-groups,clicks}`, `invoca/{transactions,callscoring}`.

**Auth is not Firebase.** A per-instance API key in `X-API-Key`, checked against the Secret Manager secret `datafeed-api-key-<instance_id>` — creating the secret IS enabling the feed, and rotating it needs a service restart (secrets are cached at import). Every query is additionally WHERE-scoped to the instance's `google_ads_customer_id` / `invoca_profile_id`, so a leaked key still cannot cross tenants. Anyone changing `verify_token` or CORS must remember this second, header-keyed auth path exists.

`/invoca/callscoring` is the settled compute-on layer (classification + override-applied `verified_outcome` + PMS-reconciled `booked_verified`, same 10-day rule as `_call_tagging_cte`); the google-ads and transactions endpoints are the operational mirror. Transactions are deduped to one row per `complete_call_id` (the table is event-grained). `SCHEMA_VERSION = "1.4"` (bumped when the booking window widened 3 → 10). `calling_phone_number` is withheld unless the BAA gate secret `datafeed-caller-number-enabled-<instance_id>` exists. Client-facing docs: `resources/datafeed-api.md`. First consumer: Virsono Hearing Centres.

## Customer.io — database-reactivation outbound

`GET/POST/DELETE /clinics/{clinic_id}/customerio` (write-only config; status shows configured / not, DELETE disables the sync) and `POST /clinics/{clinic_id}/worklists/cohort/{cohort_key}/customerio-sync`.

**One Customer.io workspace per clinic**, so credentials are per-clinic secrets — `customerio-site-id-<clinic_id>`, `customerio-track-api-key-<clinic_id>`, optional `customerio-region-<clinic_id>` (`eu`). Creating the pair IS enabling the sync; a clinic without them 409s before any patient is touched. Workspace-per-clinic also gives hard tenant isolation on the Customer.io side.

CORTEX owns the audience, Customer.io owns the campaign: per enrolled patient the client (`api/services/customerio.py`) does exactly two things — `identify` (person id `{clinic_id}:{client_id}`, contact attributes + per-channel consent flags) and `track` (the enrollment event, default `tested_not_sold_lead`). `consent_blocks_send` is the authoritative gate: a patient opted out of every channel is never identified or evented at all.

Sync is **dry-run by default**; live sends need `dry_run=false`. Send-once idempotency lives in Cloud SQL `customerio_enrollments` (alembic 0022). Cloud Scheduler authenticates with the shared `customerio-sync-secret` in an `X-CIO-Sync-Secret` header; human admins use their Firebase token.

## Worklists (`api/worklists.py`, `api/account/worklist_taxonomy.py`)

Per-clinic reactivation cohorts (tested-not-sold, fitted-not-sold, no-show, …) configured in the dashboard as validated JSON on `clinic_worklist_taxonomy` (alembic 0015; `WorklistTaxonomyConfig`). A clinic with no config row gets the single built-in default cohort (`_DEFAULT_TAXONOMY`). Read from the `PMS_Unified` views. Contact CSV export (`…/cohort/{key}/export.csv`) is super_admin/admin only and PHI-audit-logged. `/worklists/pms-taxonomy` is discovery (which appointment types / statuses the clinic's PMS actually uses).

## PHI access audit log (`api/audit.py`)

HIPAA §164.312(b) requires record-level audit controls. GCP Cloud Audit Logs capture table-level access; this records application-level intent — who looked up which patient record, for which clinic, and what happened.

`log_phi_access(clinic_id=…, action=…, actor=…, patient_id=…, outcome=…, detail=…)` appends to `Users.phi_access_log` (partitioned by `DATE(accessed_at)`, created lazily on first use). The row carries **no direct PHI** — only the opaque PMS `client_id`, the clinic, action, actor, outcome and a small non-PHI note.

Writes are best-effort: an audit failure must never break a live patient call, so insert errors are logged and swallowed. **Call it from every route that reads patient records** — the voice-agent Blueprint proxy (patient match / journal / appointment locate) and the worklist contact CSV export already do. PHI isolation is regression-tested by `tests/test_phi_isolation.py`.

## BigQuery Tables

Config lives in Cloud SQL, **not** BigQuery. What this service touches in BigQuery:

Written by this service:
- `Users.voice_agent_tickets` — after-hours "take a message" tickets (DML INSERT, `api/voice_agent/voice_agent.py::submit_ticket`). Carries caller name, callback number, free-text summary, `blueprint_patient_id` — PHI, no retention policy.
- `Users.phi_access_log` — PHI access audit trail, table created lazily (`api/audit.py`)
- `ClinicData.webforms` — lead-form submissions from our own site backends, streamed (`api/webforms.py`, `POST /webforms`, `ingest_source = json_relay`). The ETL's `jotform-ingest` is the other writer and owns the schema.
- `ClinicData.call_outcome_overrides` — manual call relabels, append-only (`api/intelligence.py` route → `intelligence_report/queries.py::set_call_outcome_override`). **The table must pre-exist or every call query returns blank.**
- `ClinicData.faq_embeddings` — voice-agent FAQ serving layer, MERGE on approval (`api/voice_agent/faq_retrieval.py`); searched with `VECTOR_SEARCH` mid-call by the `faq_lookup` protocol

Read-only (written by the ETL in `cortex-data-ingestion/`):
- `ClinicData.{transactions, ad_clicks_v2, ad_groups, callscoring, matthew_calls, faq, geo_targets, google_ads_campaigns_catalog, invoca_campaigns_catalog}` (`geo_targets` is loaded by this repo's one-off `intelligence_report/load_geo_targets.py`, which since 2026-09-04 also attaches GeoNames `latitude`/`longitude` to city-level targets; `queries.paid_click_breakdown` selects those columns, so the table must carry them. The SPA's map that used them was removed 2026-09-08 — the coordinates ride along in the payload unused)
- `Blueprint_PHI.*` and `CounselEar_PHI.*`, normally reached through the `PMS_Unified.*` views (`intelligence_report/queries.py::_BP`, `api/datafeed.py::_PMS_UNIFIED`)
- Cloud SQL `marts.*` (not BigQuery) is the `/v2` serving layer — see above

## Data Models (`api/models.py`)

Request bodies only — responses are plain dicts assembled from the ORM. The 14 models:

- `InstanceCreate` / `InstanceUpdate` — `instance_name`, `primary_contact_{name,email}`, `google_ads_customer_id`, `invoca_profile_id`. `InstanceUpdate` drops `None` so a field can be corrected but not blanked.
- `ClinicCreate` / `ClinicUpdate` — name, address, place_id, seven `hours_<weekday>` strings, phone, time_zone, country, plus `gbp_location_id`, `etl_enabled`, `tier` on update. `_reject_empty_string` on `ClinicUpdate`.
- `ProvisionRequest` — v1 `/provision_account/` body (`uid` + instance + clinics). The v2 shape is `ProvisionRequestV2` in `api/v2/provision.py`, which also carries the PMS account and defaults `clinics` to empty.
- `ClinicCampaignCreate` — `campaign_type ∈ {google_ads, invoca, jotform}`.
- `InstancePmsConfigSet`, `PmsLocationEntry`, `PmsLocationImport(Entry)` — account-level PMS config and its location map.
- `JotformLocationEntry`, `JotformLocationMapSet` — the shared-form location map.
- `CustomerIOConfigSet` — `site_id` / `track_api_key` / `region`.
- `WebformSubmission` — the JSON relay body for `POST /webforms`. (`_store_submission` also takes a keyword-only `source` → `ingest_source`.)

**Voice-agent and PMS fields are Cloud SQL columns, not Pydantic model fields.** `voice_agent_status` (enum `inactive|provisioning|active|error`), `twilio_phone_number`, `twilio_phone_sid`, `twilio_verified_caller_id`, `vapi_assistant_id`, `vapi_phone_number_id`, `alert_sms_to`, `alert_email_to`, `agent_role` all live on `clinic_voice_agent_configuration`. Blueprint connection details live on `instance_pms_config` + `pms_clinic_locations` (alembic 0030); the API key and AWS credentials are Secret Manager only and never touch the DB.

## Voice agent (`api/voice_agent/`)

The deployed VAPI assistant is built here — `factory.py::build_agent_config(db, clinic)` assembles the system prompt (script, persona, caller buckets, qualifying questions, hours from `clinic_location_details`, approved FAQs) and the tool list from the enabled protocols, then `vapi.py` pushes it. `POST /clinics/{clinic_id}/voice_agent/activate` is the destructive re-provision (delete + recreate); `POST …/voice_agent/assistant` is the idempotent rebuild. Twilio number purchase is **out of scope** of activate — numbers are attached in the VAPI dashboard; `twilio.py`'s purchase/verify helpers currently have no callers (the live Twilio path is `api/services/notify.py`'s staff SMS on ticket submit).

Protocols (`protocols/`, `PROTOCOL_REGISTRY`, 14 registered, 13 toggleable — `submit_ticket` is always on) each contribute a prompt fragment + VAPI tool definitions; per-clinic toggles and validated `config` JSON live in `clinic_protocols` (alembic 0004/0005). `roles.py` holds single-purpose role compilers (`ROLE_ANNUAL_BOOKING` for ACNA). `pms/` holds the `PMSAdapter` ABC + `adapter_for(clinic)`. Config tables: alembic 0002 (script), 0003 (persona + caller buckets), 0006 (qualifying questions), 0007 (script field rework), 0023 (FAQ). Design record: `resources/protocols-design.md` (shipped). **`voice_agent_builder/` at the repo root is the retired predecessor — do not edit it.**

⚠️ `cortex-spa/src/lib/voice_agent_capabilities.ts` is a hand-maintained mirror of the registry and is missing three toggleable protocols (`retrieve_patient_context`, `confirm_appointment`, `faq_lookup`), so they cannot be switched on from the dashboard. Adding a protocol on the backend means editing that file too.

## Known residue

- `clinic_blueprint_config` / `clinic_counselear_config` still exist as 0030's rollback path, marked deprecated by table COMMENT. Drop in a follow-up; do not add readers.
- `intelligence_report/group_queries.py` has no production consumer.
- `instances.multi_location_group` is deprecated but still read by `scripts/prewarm_payloads.py` and `scripts/parity_harness.py` — see Group Intelligence.
- `api/voice_agent/twilio.py` has no callers.
- `ringcentral_numbers` (the RingCentral clinic-attribution table the ETL expects) has no migration and no ORM model; `_CAMPAIGN_TYPES` has no `ringcentral` member.

## Cloud Run Jobs (production)

The **same image** that serves the API also runs as a job with an overridden
`--command` — `Dockerfile` ships `scripts/` for exactly this reason, so there is
no second image to keep in sync.

| Job | Command | Schedule (PT) | Purpose |
|---|---|---|---|
| `payload-prewarm` | `python scripts/prewarm_payloads.py` | hourly, as the **last step of the `etl-hourly-chain` workflow** (no longer its own `payload-prewarm-hourly` cron) | Warm the intelligence JSON payload cache so the first real visitor after a data-version rotation never pays the cold cost. Covers every **cached** endpoint the SPA requests — `overview`, `pipeline-revenue` and `active-leads` per scope × 4 windows, plus per-clinic `biweekly`. The count scales with the tenant list: **(clinics + group instances) × 4 × 3 + clinics**, run 4 at a time (`--concurrency`). At 31 clinics that is ~415 requests; run `scripts/prewarm_payloads.py --dry-run` for the current plan rather than trusting a number written here. (The 365d preset clamps to `MIN_DATE` and dedupes into the default window, which is why it is 4 windows and not 5.) The group requests are the expensive ones — each fans the per-clinic readers across every clinic in the instance, so they cost roughly N clinic pages apiece. |

**Coverage is defined by what the SPA fetches AND what the backend caches** —
both halves, or the job silently warms nothing useful. `scripts/prewarm_payloads.py`
carries the page-by-page table; the rules that keep it honest:

- It calls the HTTP endpoints, never the payload builders, so the cache key is
  always built by the code that serves it.
- It sends the *exact* query string the SPA sends, **including sending none where
  the SPA sends none**. `biweekly` is the cautionary case: the SPA passes no
  dates, so the window comes from the endpoint's `days=14` default
  (`Window.from_days(14)` = today−14 … today, a 15-day span). The job used to
  compute `today−13 … today` itself and warmed a key no page ever asked for.
- `/calls` and the four `*.html` drill-downs are **not cached**, so they are not
  warmed — a request would do the work and keep nothing. Adding a cache tier to
  them is the prerequisite, not adding them here.
- Work is ordered in tiers (every scope's landing Dashboard at the default window
  first, presets and tabs after) so a run cut short by the task timeout has
  warmed the pages people actually land on. It is not serial: when
  `blueprint-sync` lands, every clinic's `data_version` rotates at once and the
  summed cold cost of a serial run runs past the hour-long task timeout.

Anything cached that the SPA requests but this job skips shows up as a page that
is permanently cold — `pipeline-revenue` (fired by the landing Dashboard on both
the clinic and the rollup) and `active-leads` (the Leads tab) were exactly that
until 2026-08-21. **Today the group scopes for Calgary and Sense of Hearing are
in that state** because the job selects on the deprecated flag (see Group
Intelligence).

Why **hourly** and not pinned to `blueprint-sync`: the cache key rotates on the
PMS snapshot date (see `_data_version` in `api/intelligence.py`), and an hourly
run is self-healing — it needs no knowledge of when any upstream sync actually
landed or retried, and a warm run is a cheap no-op because the job hits the same
data-versioned keys the SPA does.

It now runs as the tail of `etl-hourly-chain` (defined in
`cortex-data-ingestion/deploy/etl-hourly-chain.yaml`) so it warms the cache
*after* the hour's ingest has landed rather than racing it. It sits deliberately
outside that workflow's fail-fast section: if an upstream ingest breaks, the
cache is still warmed, because otherwise every dashboard visitor would pay the
cold cost until someone fixed the ingest.

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

# REQUIRED on first creation. The etl-hourly-chain workflow invokes this job as
# the SA below; without the binding the step 403s. This exact grant was missed
# when the job was created (2026-08-07), and back then the symptom was silent:
# the old payload-prewarm-hourly schedule showed ENABLED with a fresh
# lastAttemptTime while no Cloud Run execution was ever created. Now that it is
# chained, a missing grant instead surfaces as a failed workflow execution.
# `deploy_docker_image.sh chain` (in cortex-data-ingestion) re-applies this idempotently.
gcloud run jobs add-iam-policy-binding payload-prewarm --region=us-central1 \
  --member="serviceAccount:cortex-accounts-cloudsql-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

gcloud run jobs execute payload-prewarm --region=us-central1   # run now
```

**Redeploy the job after deploying the service** if the prewarm script or the
cache-key logic changed — the job pins an image digest, so `./dev.sh` alone
leaves it on the old build.

## Environment Variables

No `.env` is required — project id and dataset are hardcoded (`api/deps.py`, `api/core/secrets.py`) and every secret comes from Secret Manager via ADC. The repo's `.env` holds only an ngrok token for local voice-agent tunnelling. The six vars the code actually reads, all optional:

| Var | Default | Effect |
|---|---|---|
| `ALLOWED_ORIGINS` | `http://localhost:3000` | Comma-separated CORS origins (`api/__init__.py`). The SPA proxies same-origin, so this rarely matters. |
| `LOG_LEVEL` | `INFO` | Root log level; logs go to stdout |
| `CLOUD_SQL_IAM_USER` | resolved from ADC / gcloud config | Override the IAM DB username (`api/core/db.py`) |
| `CLOUD_SQL_USE_PRIVATE_IP` | unset (public IP) | Use the private-IP connector path |
| `PAYLOAD_SHARED_CACHE` | `1` | Set `0` to disable the GCS-backed payload cache (`api/intelligence.py`) |
| `CORTEX_API_BASE_URL` | `http://localhost:8000` | Base URL baked into VAPI tool definitions — prepend the ngrok URL per run, don't export it (`api/voice_agent/protocols/*.py`) |
