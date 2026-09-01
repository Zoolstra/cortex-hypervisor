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
| pms_config (`api/account/pms_config.py`) | **Two scopes.** Per clinic: `GET/POST/DELETE /clinics/{clinic_id}/pms` (→ `clinic_blueprint_config` + `clinic_{clinic_id}_blueprint_*` secrets). Per **account**, for one PMS login serving several locations: `GET/POST/DELETE /instances/{instance_id}/pms` (→ `instance_pms_config` + `pms_clinic_locations` + `instance_{instance_id}_blueprint_*`). See PMS config scopes below. |
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

### Provisioning (`api/v2/provision.py`)

Re-homed from the Next app's `/api/admin/provision` server route, which needed
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
| Business | `/settings/instance/$instanceId` (`InstanceSettingsShell.tsx`) | Overview (config status + the location list), Details (`instance_name`, `google_ads_customer_id`, `invoca_profile_id`; Group Intelligence shown read-only since it is derived), PMS (account config + location map) |
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
and the PMS secrets (`instance_{instance_id}_{pms}_{key}` — id-keyed, not
the name-derived layout the root CLAUDE.md documented) all key on ids, and
every other reader treats the name as a display label. Two spots are *not*
retroactive: the live VAPI assistant keeps the old name in its prompt until
republished, and `ClinicData.webforms` rows already written keep the name they
were stamped with (a point-in-time record, not a stale copy). Neither had
any home in the SPA before — the ids were Cloud-SQL-only, and the clinic fields
were write-once at provisioning. Note `_reject_empty_string` on `ClinicUpdate` /
the None-drop in `update_instance`: a value can be corrected but not blanked,
which is why the UI only ever sends fields that actually changed.

> **Clinic hours are load-bearing — do not "simplify" them out of the model.**
> `clinic_location_details.hours_<weekday>` has three live consumers:
> `_hours_block` in the voice-agent prompt (`api/voice_agent/factory.py`, also
> used by `roles.py`), the **Revenue-per-clinic-hour** KPI
> (`intelligence_report/payloads.py` + the group rollup, parsed by
> `intelligence_report/clinic_hours.py`), and the "is this clinic open right
> now" gate on active leads (`active_leads.py`). The KPI is inside the
> methodology parity freeze (dashboard-rework-plan §1.2).

### Running tests without live ADC

`api/core/secrets.py` builds its client at import time and `deps.py` fetches the
Firebase SA at import time, so `pytest` normally needs working ADC
(`gcloud auth application-default login`) and hits Secret Manager on every run.
When ADC is stale, a pytest plugin that replaces
`secretmanager.SecretManagerServiceClient` in `pytest_configure` (plugins load
before collection) unblocks the suite — the fake must return a service account
whose `private_key` is a real generated RSA key, or
`credentials.Certificate(...)` rejects it.

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
**not** touch `etl_enabled` (that gates Google Ads and Invoca too) and reports
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
are dropped in a follow-up. **Do not add readers.** `configure_blueprint.py` was
removed with them — it wrapped the deleted per-clinic endpoint, and the dashboard
does the job.

Frontend: `cortex-spa/src/components/manage/InstancePmsSection.tsx` on
`/settings/instance/$instanceId/pms`. The map lists the locations
`clinicConfiguration` reports (fetched on load, not behind a button), so
assigning them to clinics does not depend on remembering to look. There is no
clinic-level PMS route — `ClinicPmsSummary.tsx` renders the read-only "how this
clinic is fed" panel on the clinic Overview instead, because a tab implies an
editor.

## Jotform lead forms — one form, many clinics

A Jotform webhook URL carries exactly one `clinic_id` in its path, so
`jotform_forms` maps a form to one clinic. That is wrong for a group running
every site off a single lead form: Sense of Hearing's appointment-request form
(262174010008038) serves all 14 Ontario locations, and of its first 78
submissions only 11 chose Burlington — wiring it to the group's one existing
clinic would have misattributed 86% of the leads.

`jotform_form_locations` (alembic 0032) maps each "choose your location" answer
to a clinic; `api/webforms.py::_resolve_location_clinic` applies it and the path
clinic becomes the fallback. Three things about it are deliberate:

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

Maintain the map with `configure_jotform_webhooks.py --locations` (dry-run by
default; `--apply` records unmapped options with no clinic, `--link-by-name`
also links options whose leading label is exactly a clinic name of the same
instance) or through `GET`/`PUT /campaigns/jotform/{form_id}/locations`.

Wire a group's webhook only after its map exists — the leads that arrive in
between all land on the default clinic.

## Group Intelligence (multi-location rollup)

`GET /intelligence/group/{instance_id}/overview` returns the **per-clinic
Overview payload aggregated across the instance's clinics** — the same shape as
`GET /intelligence/{clinic_id}/overview`, so the SPA renders both through the
same `OverviewView` and every clinic-page section exists on the group page by
construction. Gated on the instance having **two or more clinics** — 404 (not
403) for a single location, so the section is invisible rather than empty.

**Derived, not a setting** (alembic 0031, `api/core/grouping.py`). It was a stored
`instances.multi_location_group` flag a super_admin toggled, which restated what
the data already said and could disagree with it: an instance provisioned with
four locations kept 404ing its rollup because nobody flipped the switch. The rule
now lives in one place because there are six readers (four rollup gates, two
payload fields) and a derivation copied six times will eventually differ in one.

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

Frontend: `components/intelligence/PmsCoverage.tsx` (both apps) — a page-level
banner plus the inline replacements used wherever a suppressed figure sat. The
funnel stops at "connected", ROAS/revenue columns are dropped rather than
dashed, and the qualified-no-conversion leak is restated as an unverified
follow-up list (nothing can confirm those callers didn't book later).

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
| `scripts.py` | Call scripts per clinic per call type |
| `campaigns.py` | Multi-campaign ID management per clinic (new table, old column kept) |

## Cloud Run Jobs (production)

The **same image** that serves the API also runs as a job with an overridden
`--command` — `Dockerfile` ships `scripts/` for exactly this reason, so there is
no second image to keep in sync.

| Job | Command | Schedule (PT) | Purpose |
|---|---|---|---|
| `payload-prewarm` | `python scripts/prewarm_payloads.py` | hourly, as the **last step of the `etl-hourly-chain` workflow** (no longer its own `payload-prewarm-hourly` cron) | Warm the intelligence JSON payload cache so the first real visitor after a data-version rotation never pays the cold cost. Covers every **cached** endpoint the SPA requests — `overview`, `pipeline-revenue` and `active-leads` per scope × 4 windows, plus per-clinic `biweekly`: 14 scopes × 4 × 3 + 13 = **181 requests**, run 4 at a time (`--concurrency`). The group requests are the expensive ones — each fans the per-clinic readers across every clinic in the instance (see Group Intelligence below), so they cost roughly N clinic pages apiece rather than one. Warming them matters more than it used to: a cold group page is minutes. |

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
until 2026-08-21.

Why **hourly** and not pinned to `blueprint-sync`: the cache key rotates on the
PMS snapshot date (see `_data_version` in `api/intelligence.py`), and an hourly
run is self-healing — it needs no knowledge of when any upstream sync actually
landed or retried, and a warm run is a cheap no-op because the job hits the same
data-versioned keys the SPA does.

It now runs as the tail of `etl-hourly-chain` (defined in
`big-query-ingestion/deploy/etl-hourly-chain.yaml`) so it warms the cache
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
# `deploy_docker_image.sh chain` re-applies this idempotently.
gcloud run jobs add-iam-policy-binding payload-prewarm --region=us-central1 \
  --member="serviceAccount:cortex-accounts-cloudsql-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

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
