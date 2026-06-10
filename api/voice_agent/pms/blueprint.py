"""
Blueprint OMS adapter.

Owns the PMS-call plumbing for Blueprint: BigQuery patient match against
`Blueprint_PHI.ClientDemographics`, HTTP calls to Blueprint REST for
appointment types and bookable availability.

Construction:
- `BlueprintAdapter(clinic_id=...)` — sufficient for `find_patient`,
  which only needs `bq_client` and the path-level `clinic_id`. No
  Cloud SQL or Secret Manager round trip.
- For HTTP-backed methods (`list_appointment_types`,
  `find_available_slots`), the caller must call `load_http_config(db)`
  first — that's where the Cloud SQL config row + Secret Manager API key
  + timezone get resolved. We keep the load explicit so the patient
  match endpoint isn't paying a Cloud SQL hit it never needed before.

The `_get_blueprint_config` / `_blueprint_base` / `_int_field` helpers
live here so the router file is a pure HTTP boundary. The router still
re-exports `_get_blueprint_config` for any callers that need the full
config dict directly (e.g. the admin `/clinic-config` endpoint that
isn't part of the protocol surface).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from google.cloud import bigquery
from sqlalchemy.orm import Session

from api.core.orm import Clinic, ClinicBlueprintConfig
from api.core.secrets import get_secret
from api.deps import PROJECT, bq_client
from api.voice_agent.pms import (
    Appointment,
    AppointmentType,
    AvailabilityDay,
    AvailabilityResult,
    BookingResult,
    JournalEntry,
    Location,
    PatientMatchResult,
    PMSAdapter,
    Provider,
)


log = logging.getLogger(__name__)


# Blueprint appointment status codes (see API docs):
#   0=Confirmed, 1=No show, 2=Tentative, 3=Cancelled, 4=Left Message,
#   5=Arrived, 6=In Progress, 7=Completed, 8=No answer, 9=Ready
_STATUS_CANCELLED = 3
_STATUS_NAMES = {
    0: "confirmed", 1: "no_show", 2: "tentative", 3: "cancelled",
    4: "left_message", 5: "arrived", 6: "in_progress", 7: "completed",
    8: "no_answer", 9: "ready",
}


# ── Config helpers ────────────────────────────────────────────────────────────


def _get_blueprint_config(db: Session, clinic_id: str) -> dict:
    """
    Resolve Blueprint config + API key + timezone for a clinic.

    Reads:
      - clinics + clinic_blueprint_config + clinic_location_details (Cloud SQL)
      - clinic_{clinic_id}_blueprint_api_key (Secret Manager)

    Returns dict with: clinic_name, api_url, clinic_code, api_key, timezone,
    instance_id (for the admin access check).
    """
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")

    if clinic.pms_type != "blueprint":
        raise HTTPException(status_code=400, detail="Clinic is not configured for Blueprint OMS")

    bp = db.get(ClinicBlueprintConfig, clinic_id)
    if not bp or not bp.api_url:
        raise HTTPException(status_code=400, detail="Blueprint config incomplete: api_url is missing")

    try:
        api_key = get_secret(f"clinic_{clinic_id}_blueprint_api_key")
    except Exception:
        raise HTTPException(status_code=400, detail="Blueprint API key not found in Secret Manager")

    location = clinic.location  # 1:1
    return {
        "clinic_name": clinic.clinic_name,
        "api_url": bp.api_url,
        "clinic_code": bp.clinic_code,
        "api_key": api_key,
        "timezone": location.time_zone if location else None,
        "instance_id": clinic.instance_id,
        "prompt_for_location": bool(bp.prompt_for_location),
        "user_id": bp.user_id,
    }


def _blueprint_base(config: dict) -> str:
    """
    Derive the REST base URL from api_url.

    api_url is the full URL as stored by configure_blueprint.py (e.g.
    "https://ca-alb1.aws.bp-solutions.net:8443/ca_mst1/AB/acn/?rest/hello").
    We strip the trailing rest/... path and return the base.
    """
    url = config["api_url"].replace("​", "").strip()
    url = re.split(r"[?]", url, maxsplit=1)[0].rstrip("/")
    url = re.sub(r"/rest(/.*)?$", "", url)
    return f"{url}/rest"


def _int_field(config: dict, key: str, default: int = 0) -> int:
    val = config.get(key)
    return int(val) if val else default


def _redact_apikey(params: dict) -> dict:
    """Return a copy of ``params`` with the apiKey value masked for logging."""
    if "apiKey" not in params:
        return params
    return {**params, "apiKey": "<redacted>"}


# ── Hardcoded defaults ────────────────────────────────────────────────────────

# Use "DURATION" rather than a fixed-minutes interval. Blueprint then
# spaces slots by the event type's natural duration — e.g. a 45-min
# Hearing Test produces 45-min slots, a 30-min Fitting produces 30-min
# slots. Fixed intervals (e.g. "30") miss slots when an event type's
# duration doesn't evenly divide into the interval, which was the
# leading suspect when no clinic could find availability under v1.
# Allowed values per Blueprint docs: "60" / "30" / "15" / "DURATION".
_DEFAULT_BOOKING_TIME_SLOT_INTERVAL = "DURATION"
_DEFAULT_MINIMUM_ADVANCE_BOOKING_TIME = 30  # minutes


# ── Adapter ───────────────────────────────────────────────────────────────────


class BlueprintAdapter(PMSAdapter):
    """Blueprint OMS adapter.

    Construct with `BlueprintAdapter(clinic_id=...)` for `find_patient`-only
    flows (no Cloud SQL hit). Call `load_http_config(db)` before any
    HTTP-backed method.
    """

    pms_type = "blueprint"

    def __init__(self, clinic_id: str, http_config: dict | None = None):
        self.clinic_id = clinic_id
        self._http_config = http_config

    # ── HTTP config loading (lazy) ────────────────────────────────────────────

    def load_http_config(self, db: Session) -> None:
        """Fetch Blueprint API + timezone config for HTTP-backed methods.

        Safe to call multiple times; only the first call hits Cloud SQL +
        Secret Manager.
        """
        if self._http_config is None:
            self._http_config = _get_blueprint_config(db, self.clinic_id)

    def _require_http_config(self) -> dict:
        if self._http_config is None:
            raise RuntimeError(
                "BlueprintAdapter HTTP method called before load_http_config(db)"
            )
        return self._http_config

    # ── Patient match (BQ — no HTTP / Cloud SQL needed) ───────────────────────

    def find_patient(
        self,
        *,
        first_name: str,
        last_name: str,
        last4_phone: str,
        dob: str | None = None,
    ) -> PatientMatchResult:
        """
        Server-side patient match against `Blueprint_PHI.ClientDemographics`.

        The `_clinic_id` filter is **mandatory and non-negotiable** —
        cross-clinic PHI must never be returnable. Matches on first + last
        name (case-insensitive), then filters by any phone field
        (mobile/home/work) ending in `last4_phone`. If `>1` candidates remain
        and dob is provided, adds dob as a tie-breaker.
        """
        last4 = "".join(c for c in last4_phone if c.isdigit())
        if len(last4) != 4:
            raise HTTPException(status_code=400, detail="last4_phone must be exactly 4 digits")

        params = [
            bigquery.ScalarQueryParameter("clinic_id", "STRING", self.clinic_id),
            bigquery.ScalarQueryParameter("first_name", "STRING", first_name.strip()),
            bigquery.ScalarQueryParameter("last_name", "STRING", last_name.strip()),
            bigquery.ScalarQueryParameter("last4", "STRING", last4),
        ]
        dob_clause = ""
        if dob:
            dob_clause = "AND birthdate = @dob"
            params.append(bigquery.ScalarQueryParameter("dob", "STRING", dob))

        sql = f"""
        SELECT client_id
        FROM `{PROJECT}.Blueprint_PHI.ClientDemographics`
        WHERE _clinic_id = @clinic_id
          AND LOWER(given_name) = LOWER(@first_name)
          AND LOWER(surname) = LOWER(@last_name)
          AND (
            ENDS_WITH(IFNULL(mobile_telephone_no, ''), @last4)
            OR ENDS_WITH(IFNULL(home_telephone_no, ''), @last4)
            OR ENDS_WITH(IFNULL(work_telephone_no, ''), @last4)
          )
          {dob_clause}
    """
        rows = list(bq_client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        ).result())

        count = len(rows)
        if count == 0:
            return PatientMatchResult(status="unmatched", patient_id=None, candidates_count=0)
        if count == 1:
            return PatientMatchResult(
                status="matched",
                patient_id=rows[0]["client_id"],
                candidates_count=1,
            )
        return PatientMatchResult(status="ambiguous", patient_id=None, candidates_count=count)

    # ── Patient journal / history (BQ — PHI) ──────────────────────────────────

    def get_patient_journal(self, *, patient_id: str) -> list[JournalEntry]:
        """Recent journal entries for an identified patient, for agent context.

        The ``_clinic_id`` filter is **mandatory and non-negotiable** — same
        PHI-isolation rule as ``find_patient``: clinic A's journal must never
        be returnable from clinic B's endpoint. Bounds: non-deleted rows only,
        last 24 months, 10 most recent. ``entry_time`` is STRING in the feed,
        so we SAFE_CAST it for the window + ordering (rows that don't parse
        drop out rather than erroring).
        """
        sql = f"""
        SELECT entry_time, entry_type, user_text, generated_text
        FROM `{PROJECT}.Blueprint_PHI.ClientJournal`
        WHERE _clinic_id = @clinic_id
          AND client_id = @patient_id
          AND (deleted_time IS NULL OR deleted_time = '')
          AND SAFE_CAST(entry_time AS TIMESTAMP)
              >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 MONTH)
        ORDER BY SAFE_CAST(entry_time AS TIMESTAMP) DESC
        LIMIT 10
        """
        params = [
            bigquery.ScalarQueryParameter("clinic_id", "STRING", self.clinic_id),
            bigquery.ScalarQueryParameter("patient_id", "STRING", str(patient_id)),
        ]
        rows = bq_client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params),
        ).result()

        entries: list[JournalEntry] = []
        for r in rows:
            text = (r["user_text"] or r["generated_text"] or "").strip()
            if not text:
                continue
            entries.append(JournalEntry(
                entry_time=r["entry_time"],
                entry_type=r["entry_type"],
                text=text,
            ))
        return entries

    # ── Appointment types ─────────────────────────────────────────────────────

    def list_appointment_types(self) -> list[AppointmentType]:
        """Pull bookable appointment types from Blueprint clinicConfiguration.

        Filters out null-named placeholder rows. Blueprint returns the full
        appointmentTypes pool (active + inactive + deleted) with `name=null`
        on anything not currently in use; the agent can't reason about
        anonymous IDs, so they're dropped.
        """
        config = self._require_http_config()
        base = _blueprint_base(config)

        resp = httpx.get(
            f"{base}/clinicConfiguration/",
            params={"apiKey": config["api_key"]},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        return [
            AppointmentType(
                id=t["id"],
                name=t["name"],
                duration_minutes=t.get("duration"),
            )
            for t in data.get("appointmentTypes", [])
            if t.get("name")
        ]

    def list_locations(self) -> list[Location]:
        """Pull bookable locations from Blueprint clinicConfiguration.

        clinicConfiguration only returns locations enabled for online
        booking (lat/long set, availability enabled), so this is the set
        the agent can actually offer. Null-named placeholders are dropped.
        """
        config = self._require_http_config()
        base = _blueprint_base(config)

        resp = httpx.get(
            f"{base}/clinicConfiguration/",
            params={"apiKey": config["api_key"]},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        return [
            Location(
                id=loc["id"],
                name=loc.get("name"),
                address=loc.get("formattedAddress") or loc.get("street"),
            )
            for loc in data.get("locations", [])
            if loc.get("name")
        ]

    def list_providers(self) -> list[Provider]:
        """Pull bookable providers (online-booking-enabled) from
        clinicConfiguration as a name ↔ id mapping for the system prompt.
        """
        config = self._require_http_config()
        base = _blueprint_base(config)

        resp = httpx.get(
            f"{base}/clinicConfiguration/",
            params={"apiKey": config["api_key"]},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        providers: list[Provider] = []
        for p in data.get("providers", []):
            name = (p.get("onlineDisplayName")
                    or f"{p.get('firstName', '')} {p.get('lastName', '')}".strip())
            if not name:
                continue
            providers.append(Provider(id=p["id"], name=name))
        return providers

    def _resolve_location_id(self, location_id: int | None) -> int:
        """Resolve the location to book into.

        Caller-chosen ``location_id`` wins. Otherwise fall back to the
        clinic's sole location. A multi-location clinic with no explicit
        choice is an error — we won't guess which one.
        """
        if location_id:
            return location_id
        locations = self.list_locations()
        if len(locations) == 1:
            return locations[0].id
        if not locations:
            raise HTTPException(
                status_code=400,
                detail="Clinic has no bookable locations configured in Blueprint",
            )
        raise HTTPException(
            status_code=400,
            detail=(
                "Clinic has multiple locations — a location_id is required "
                "to book"
            ),
        )

    # ── Bookable slot search ──────────────────────────────────────────────────

    def find_available_slots(
        self,
        *,
        event_type_id: int,
        start_date: str,
        end_date: str,
        providers: list[int] | None = None,
        locations: list[int] | None = None,
    ) -> AvailabilityResult:
        """
        Find concrete bookable time slots for one appointment type.

        Proxies Blueprint's GET `/rest/availability/?...` over the date/time
        window. Blueprint only generates slots from availability blocks that
        are **enabled for online booking** and whose provider is mapped to
        the event type — it is NOT "every scheduled block is bookable". A
        clinic whose availability isn't flagged for online booking returns
        nothing here even though staff see a full calendar. Hardcodes the
        booking interval and minimum-advance-booking values; the agent
        doesn't need to care about those.

        Response is aggressively stripped — only date + genuinely-open times
        survive. A slot counts as open only when it has an available provider
        (`available` non-empty) and no existing appointment
        (`appointmentId` is null); booked slots still appear in Blueprint's
        grid and must be filtered out. Provider IDs, location IDs, and
        resource info never reach the agent; the provider is re-derived
        server-side at booking time.
        """
        config = self._require_http_config()
        base = _blueprint_base(config)

        tz = ZoneInfo(config.get("timezone") or "America/Vancouver")
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz)
        end_dt = (
            datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        ).replace(tzinfo=tz)

        params: dict = {
            "apiKey": config["api_key"],
            "startTime": int(start_dt.timestamp()),
            "endTime": int(end_dt.timestamp()),
            "eventTypeId": event_type_id,
            "bookingTimeSlotInterval": _DEFAULT_BOOKING_TIME_SLOT_INTERVAL,
            "minimumAdvanceBookingTime": _DEFAULT_MINIMUM_ADVANCE_BOOKING_TIME,
        }

        if providers is not None:
            params["providers"] = ",".join(str(p) for p in providers)
        # Caller-chosen locations narrow the search; when omitted Blueprint
        # includes all locations (per the API docs), which is what we want
        # for single-location clinics and the "any location" case.
        if locations is not None:
            params["locations"] = ",".join(str(loc) for loc in locations)

        # Log the outbound request so we can diagnose empty-response cases
        # without replaying against Blueprint. apiKey is a credential; we
        # redact it. ``days_in_window`` is the rough sanity check on the
        # window we computed.
        log.info(
            "find_available_slots GET %s/availability/ clinic_id=%s "
            "params=%s",
            base, self.clinic_id, _redact_apikey(params),
        )

        resp = httpx.get(f"{base}/availability/", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        days: list[AvailabilityDay] = []
        n_total_days = len(data) if isinstance(data, list) else 0
        n_available_days = 0
        n_total_slots = 0
        for day in data:
            if not day.get("available"):
                continue
            n_available_days += 1
            times: list[str] = []
            for slot in day.get("availabilityTimes", []) or []:
                t = slot.get("time")
                # Keep only genuinely-open slots: an available provider and
                # no existing appointment. Blueprint lists booked times in
                # the grid (available=[], appointmentId set) — advertising
                # those would offer the caller an already-taken slot.
                if not t or not slot.get("available") or slot.get("appointmentId") is not None:
                    continue
                # Trim "08:00:00-0600" → "08:00"
                hhmm = t.split(":")
                if len(hhmm) >= 2:
                    times.append(f"{hhmm[0]}:{hhmm[1]}")
                else:
                    times.append(t)
            if not times:
                continue
            n_total_slots += len(times)
            days.append(AvailabilityDay(date=day.get("date"), available_times=times))

        log.info(
            "find_available_slots response clinic_id=%s status=%d "
            "blueprint_days=%d available_days=%d kept_days=%d total_slots=%d",
            self.clinic_id, resp.status_code,
            n_total_days, n_available_days, len(days), n_total_slots,
        )

        return AvailabilityResult(days=days)

    # ── Patient appointment lookup ────────────────────────────────────────────

    def _search_appointments_raw(
        self, *, start_dt: datetime, end_dt: datetime,
    ) -> list[dict]:
        """Lower-level Search Appointments call — returns the raw rows.

        Blueprint's Search returns ALL appointments in the date range across
        the entire clinic (optionally filtered by location/event-type).
        Patient filtering happens client-side.
        """
        config = self._require_http_config()
        base = _blueprint_base(config)
        payload = {
            "apiKey": config["api_key"],
            "startTime": int(start_dt.timestamp()),
            "endTime": int(end_dt.timestamp()),
        }
        location_id = _int_field(config, "location_id")
        if location_id:
            payload["locations"] = [location_id]

        resp = httpx.post(f"{base}/appointments/search", json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json() or []

    def _appointment_type_name_map(self) -> dict[int, str]:
        """Build {event_type_id → name} so the agent gets a name in
        ``list_patient_appointments`` results without making the agent's
        flow depend on a separate `list_appointment_types` call.
        """
        try:
            types = self.list_appointment_types()
        except Exception:
            return {}
        return {t.id: t.name for t in types if t.name}

    @staticmethod
    def _parse_blueprint_time(s: str | None) -> str | None:
        """Blueprint returns times like "2026-05-30 14:30:00 GMT" or
        "2026-05-30 14:30:00 +0000". Normalize to ISO-8601 (drop the
        suffix; we keep clinic-local wall-clock semantics, matching how
        the agent quoted slots earlier in the call).
        """
        if not s:
            return None
        s = s.strip()
        for suffix in (" GMT", " UTC"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
        # Convert "YYYY-MM-DD HH:MM:SS" → "YYYY-MM-DDTHH:MM"
        if " " in s and "T" not in s:
            date_part, _, time_part = s.partition(" ")
            time_part = time_part.split("+")[0].split("-")[0].strip()
            hhmm = ":".join(time_part.split(":")[:2])
            return f"{date_part}T{hhmm}"
        return s

    def list_patient_appointments(
        self,
        *,
        patient_id: str,
        days_back: int = 0,
        days_ahead: int = 60,
    ) -> list[Appointment]:
        config = self._require_http_config()
        tz = ZoneInfo(config.get("timezone") or "America/Vancouver")
        now = datetime.now(tz)
        start_dt = now - timedelta(days=days_back)
        end_dt = now + timedelta(days=days_ahead)

        rows = self._search_appointments_raw(start_dt=start_dt, end_dt=end_dt)
        type_names = self._appointment_type_name_map()

        # Normalize the path-level patient_id to the int that Blueprint's
        # Search response uses. The verify_caller_identification BQ row
        # stores client_id as a string; Blueprint returns it as int.
        try:
            wanted = int(patient_id)
        except (TypeError, ValueError):
            return []

        appts: list[Appointment] = []
        for r in rows:
            if r.get("patient_id") != wanted:
                continue
            status_code = r.get("status")
            event_type_id = r.get("eventTypeId")
            appts.append(Appointment(
                appointment_id=str(r.get("appointment_id") or ""),
                event_type_id=event_type_id,
                event_type_name=type_names.get(event_type_id) if event_type_id else None,
                summary=r.get("summary"),
                start_time=self._parse_blueprint_time(r.get("start_time")) or "",
                end_time=self._parse_blueprint_time(r.get("end_time")) or "",
                provider_name=r.get("provider"),
                location_name=r.get("location"),
                status=_STATUS_NAMES.get(status_code, "unknown") if status_code is not None else "unknown",
            ))
        # Sort by start_time ascending. ISO-8601 strings sort
        # lexicographically as datetimes — good enough.
        appts.sort(key=lambda a: a.start_time)
        return appts

    # ── Internal helper: look up an appointment by id ─────────────────────────

    def _find_raw_appointment(
        self, appointment_id: str, days_back: int = 7, days_ahead: int = 180,
    ) -> dict:
        """Find one appointment's raw Blueprint row by appointment_id.

        Used internally before cancel/reschedule to recover the
        ``onlineBookingSecret`` (which the agent never sees). Searches a
        wider window than ``list_patient_appointments`` because we don't
        know how far out the appointment was booked.

        Raises HTTPException(404) if no match in the window.
        """
        config = self._require_http_config()
        tz = ZoneInfo(config.get("timezone") or "America/Vancouver")
        now = datetime.now(tz)
        rows = self._search_appointments_raw(
            start_dt=now - timedelta(days=days_back),
            end_dt=now + timedelta(days=days_ahead),
        )
        for r in rows:
            if str(r.get("appointment_id")) == str(appointment_id):
                return r
        raise HTTPException(
            status_code=404,
            detail=f"Appointment {appointment_id!r} not found in the lookup window.",
        )

    # ── Book ──────────────────────────────────────────────────────────────────

    def _find_open_provider(
        self, *, event_type_id: int, start_date: str, start_time: str, location_id: int,
    ) -> int:
        """Re-check availability and return the provider open at this exact slot.

        Blueprint's create-appointment requires a concrete ``providerId``;
        availability is provider-specific, so we query GET ``/availability/``
        for that single day + location and read back the provider whose slot
        at ``start_time`` is genuinely open (``available`` non-empty,
        ``appointmentId`` null). This also self-validates the slot is still
        free at book time — a no-op double-booking guard.
        """
        config = self._require_http_config()
        base = _blueprint_base(config)
        tz = ZoneInfo(config.get("timezone") or "America/Vancouver")
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz)
        end_dt = start_dt + timedelta(days=1)

        params = {
            "apiKey": config["api_key"],
            "startTime": int(start_dt.timestamp()),
            "endTime": int(end_dt.timestamp()),
            "eventTypeId": event_type_id,
            "bookingTimeSlotInterval": _DEFAULT_BOOKING_TIME_SLOT_INTERVAL,
            "minimumAdvanceBookingTime": _DEFAULT_MINIMUM_ADVANCE_BOOKING_TIME,
            "locations": location_id,
        }
        resp = httpx.get(f"{base}/availability/", params=params, timeout=15)
        resp.raise_for_status()

        for day in resp.json():
            if day.get("date") != start_date or not day.get("available"):
                continue
            for slot in day.get("availabilityTimes", []) or []:
                # Blueprint slot times look like "08:00:00-0600"; match HH:MM.
                if not (slot.get("time") or "").startswith(start_time):
                    continue
                if slot.get("appointmentId") is not None:
                    break  # the slot is taken
                for entry in slot.get("available", []) or []:
                    if entry.get("locationId") == location_id:
                        return entry["providerId"]
                break
        raise HTTPException(
            status_code=409,
            detail=(
                f"No provider is available for event type {event_type_id} at "
                f"{start_date} {start_time} (location {location_id}) — the slot "
                "may have just been taken"
            ),
        )

    def book(
        self,
        *,
        event_type_id: int,
        start_date: str,
        start_time: str,
        location_id: int | None = None,
        patient_id: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        phone: str | None = None,
        notes: str | None = None,
    ) -> BookingResult:
        config = self._require_http_config()
        base = _blueprint_base(config)
        location_id = self._resolve_location_id(location_id)

        # Derive end_time from the appointment type's duration.
        types = self.list_appointment_types()
        matching = next((t for t in types if t.id == event_type_id), None)
        if matching is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown event_type_id={event_type_id} for this clinic",
            )
        if not matching.duration_minutes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Event type {event_type_id} ({matching.name}) has no duration "
                    "configured in Blueprint — cannot derive end time"
                ),
            )
        end_time = self._add_minutes(start_time, matching.duration_minutes)

        # Availability is provider-specific and Blueprint requires a concrete
        # providerId on create — resolve the provider open at this exact slot.
        provider_id = self._find_open_provider(
            event_type_id=event_type_id,
            start_date=start_date,
            start_time=start_time,
            location_id=location_id,
        )

        # "User creating the appointment" — a configured service-account user
        # if set, else the booking provider (always a valid Blueprint user).
        # Never default to a hardcoded id: Blueprint 400s an unknown userId.
        user_id = _int_field(config, "user_id") or provider_id

        tz = ZoneInfo(config.get("timezone") or "America/Vancouver")
        start_dt = self._combine_local(start_date, start_time, tz)
        end_dt = self._combine_local(start_date, end_time, tz)

        # Caller-facing summary line. Patient name preferred; falls back
        # to event-type-id (Blueprint uses the summary in their UI).
        summary = " ".join(
            p for p in [first_name, last_name] if p
        ).strip() or f"Appointment (eventType={event_type_id})"

        payload: dict = {
            "apiKey": config["api_key"],
            "userId": user_id,
            "eventTypeId": event_type_id,
            "startTime": int(start_dt.timestamp()),
            "endTime": int(end_dt.timestamp()),
            "summary": summary,
            "status": 2,  # Tentative by default; staff confirms.
            "locationId": location_id,
            "providerId": provider_id,
        }
        # Always stamp the booking as voice-agent-originated with the key facts,
        # then append whatever the agent passed (e.g. new-patient screening
        # answers). Gives clinic staff immediate provenance + context.
        booked_on = datetime.now(tz).strftime("%Y-%m-%d")
        patient_kind = "Existing patient" if patient_id else "New patient"
        note_lines = [
            f"Booked via CORTEX voice agent on {booked_on}.",
            f"{patient_kind}. {matching.name} on {start_date} at {start_time}.",
        ]
        if notes and notes.strip():
            note_lines.append(notes.strip())
        payload["notes"] = "\n".join(note_lines)

        if patient_id:
            try:
                payload["patientId"] = int(patient_id)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="patient_id must be numeric")
        elif first_name and last_name:
            phone_digits = "".join(c for c in (phone or "") if c.isdigit())
            payload["patient"] = {
                "quickAdd": True,
                "firstName": first_name,
                "lastName": last_name,
                "locationId": location_id,
                **({"mobilePhoneNumber": phone_digits} if phone_digits else {}),
            }
        else:
            raise HTTPException(
                status_code=400,
                detail="Provide either patient_id or first_name + last_name",
            )

        resp = httpx.post(f"{base}/appointments/", json=payload, timeout=15)
        if resp.status_code >= 400:
            # Surface Blueprint's actual rejection instead of an opaque 500.
            # Log only non-PHI booking fields (NEVER the patient name/phone)
            # plus Blueprint's error body, which is what we need to diagnose.
            body = (resp.text or "").strip()[:500]
            log.error(
                "Blueprint create-appointment rejected clinic_id=%s status=%d "
                "eventTypeId=%s locationId=%s providerId=%s userId=%s "
                "appt_status=%s has_patientId=%s has_quickadd=%s body=%r",
                self.clinic_id, resp.status_code, event_type_id, location_id,
                provider_id, user_id, payload.get("status"),
                "patientId" in payload, "patient" in payload, body,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Blueprint rejected the booking ({resp.status_code}): {body}",
            )

        # Blueprint's POST returns 201 with empty body — no appointment_id.
        # We can't echo a server-issued id here without doing a follow-up
        # Search by patient+time; skipping that round-trip for now.
        return BookingResult(
            status="booked",
            appointment_id=None,
            summary=summary,
            start_time=f"{start_date}T{start_time}",
            end_time=f"{start_date}T{end_time}",
        )

    @staticmethod
    def _combine_local(date_str: str, hhmm: str, tz: ZoneInfo) -> datetime:
        """Combine a clinic-local YYYY-MM-DD + HH:MM into a tz-aware datetime."""
        return datetime.strptime(f"{date_str} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)

    # ── Cancel ────────────────────────────────────────────────────────────────

    def cancel(self, *, appointment_id: str) -> BookingResult:
        config = self._require_http_config()
        base = _blueprint_base(config)

        # Recover the onlineBookingSecret (never exposed to the agent).
        raw = self._find_raw_appointment(appointment_id)
        secret = raw.get("onlineBookingSecret")

        # "User" performing the cancel: configured service-account user if
        # set, else the appointment's own provider (a valid Blueprint user).
        user_id = _int_field(config, "user_id") or raw.get("provider_id")
        if not secret:
            raise HTTPException(
                status_code=502,
                detail=f"Appointment {appointment_id!r} has no onlineBookingSecret — cannot cancel via API",
            )

        # If it's already cancelled, no-op echo so the agent can confirm
        # naturally without surfacing the duplicate-action error.
        if raw.get("status") == _STATUS_CANCELLED:
            return BookingResult(
                status="cancelled",
                appointment_id=appointment_id,
                summary=raw.get("summary"),
                start_time=self._parse_blueprint_time(raw.get("start_time")),
                end_time=self._parse_blueprint_time(raw.get("end_time")),
            )

        payload = {
            "apiKey": config["api_key"],
            "onlineBookingSecret": secret,
            "userId": user_id,
            "status": _STATUS_CANCELLED,
        }
        resp = httpx.put(f"{base}/appointments/{appointment_id}", json=payload, timeout=15)
        resp.raise_for_status()

        return BookingResult(
            status="cancelled",
            appointment_id=appointment_id,
            summary=raw.get("summary"),
            start_time=self._parse_blueprint_time(raw.get("start_time")),
            end_time=self._parse_blueprint_time(raw.get("end_time")),
        )

    # ── Reschedule (cancel-then-book) ─────────────────────────────────────────

    def reschedule(
        self,
        *,
        appointment_id: str,
        new_start_date: str,
        new_start_time: str,
    ) -> BookingResult:
        """Blueprint's PUT can't change start/end time — only status. So
        a true reschedule is book-new-then-cancel-old. Order matters:

        1. Look up the old appointment to recover event_type_id +
           duration + patient_id (needed to recreate at the new slot).
        2. Compute the new end_time from the old appointment's duration.
        3. Book the new slot. If this fails, abort cleanly — the old
           appointment is untouched.
        4. Cancel the old appointment. If THIS fails, return
           ``status="partial"`` + warning; staff must clean up the
           leftover old booking.
        """
        raw = self._find_raw_appointment(appointment_id)
        event_type_id = raw.get("eventTypeId")
        if event_type_id is None:
            raise HTTPException(
                status_code=502,
                detail=f"Appointment {appointment_id!r} has no eventTypeId — cannot reschedule",
            )

        # Pull patient_id from the existing record. `book` derives end_time
        # from the event type's duration server-side — we don't need to
        # recompute it here.
        patient_id = raw.get("patient_id")
        if not patient_id:
            raise HTTPException(
                status_code=400,
                detail=f"Appointment {appointment_id!r} has no patient_id — cannot reschedule for a QuickAdd booking",
            )

        # 1. Book the new slot first. ``book`` derives end_time internally
        # from the event type's duration, which equals the old appointment's
        # duration (same event_type_id).
        booked = self.book(
            event_type_id=event_type_id,
            start_date=new_start_date,
            start_time=new_start_time,
            patient_id=str(patient_id),
        )

        # 2. Cancel the old one. If this fails, we surface a partial
        # success so the agent can warn the caller + the ticket can
        # capture the cleanup need.
        try:
            self.cancel(appointment_id=appointment_id)
        except Exception as e:  # noqa: BLE001 — we deliberately catch broadly
            return BookingResult(
                status="partial",
                appointment_id=None,
                summary=booked.summary,
                start_time=booked.start_time,
                end_time=booked.end_time,
                warning=(
                    f"Booked the new appointment at {new_start_date} {new_start_time}, "
                    f"but the old appointment {appointment_id!r} could not be cancelled "
                    f"({type(e).__name__}). Clinic staff must cancel the old booking manually."
                ),
            )

        return BookingResult(
            status="rescheduled",
            appointment_id=None,
            summary=booked.summary,
            start_time=booked.start_time,
            end_time=booked.end_time,
        )

    @staticmethod
    def _add_minutes(hhmm: str, minutes: int) -> str:
        """Add minutes to a HH:MM string. Wraps if it crosses midnight,
        which would mean a same-day-end constraint violation — we let
        the caller deal with that (Blueprint will reject the booking).
        """
        h, m = (int(x) for x in hhmm.split(":")[:2])
        total = h * 60 + m + minutes
        nh, nm = divmod(total, 60)
        nh %= 24
        return f"{nh:02d}:{nm:02d}"
