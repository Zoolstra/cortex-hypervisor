"""
PHI access audit log.

HIPAA Security Rule §164.312(b) requires audit controls: a record of who
accessed which patient record, and when. GCP Cloud Audit Logs capture
table-level access ("someone queried Blueprint_PHI.ClientDemographics") but
not record-level intent ("the voice agent looked up patient 12345 for clinic
X"). This module records that application-level trail.

The log itself contains NO direct PHI — no patient name, phone, DOB, or
free-text. It stores the opaque Blueprint client_id (the record identifier,
needed so an auditor can answer "who accessed patient X's record"), the
clinic, the action, the actor, and the outcome.

Writes are best-effort: an audit-log failure must never break a live patient
call, so the table is created lazily and insert errors are logged and
swallowed. The table lives in the `Users` dataset (hypervisor-owned,
operational) rather than the dashboard `ClinicData` analytics dataset; its
IAM should be restricted to compliance/security readers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from api.deps import DATASET, PROJECT, bq_client

log = logging.getLogger(__name__)

_TABLE_REF = f"{PROJECT}.{DATASET}.phi_access_log"

_DDL = f"""
CREATE TABLE IF NOT EXISTS `{_TABLE_REF}` (
  accessed_at TIMESTAMP NOT NULL,
  actor       STRING    NOT NULL,  -- who performed the access (e.g. "voice_agent")
  action      STRING    NOT NULL,  -- patient_match | patient_journal | appointment_locate
  clinic_id   STRING    NOT NULL,  -- clinic whose PHI scope was queried
  patient_id  STRING,              -- Blueprint client_id of the record (null when unmatched)
  outcome     STRING,              -- matched | ambiguous | unmatched | ok | error
  detail      STRING               -- small non-PHI note, e.g. "candidates=2", "entries=5"
)
PARTITION BY DATE(accessed_at)
"""

_table_ready = False


def _ensure_table() -> None:
    """Create the audit table on first use (idempotent, cached per process)."""
    global _table_ready
    if _table_ready:
        return
    bq_client.query(_DDL).result()
    _table_ready = True


def log_phi_access(
    *,
    clinic_id: str,
    action: str,
    actor: str = "voice_agent",
    patient_id: str | None = None,
    outcome: str | None = None,
    detail: str | None = None,
) -> None:
    """Append one row to the PHI access log. Best-effort — never raises.

    Call this immediately after a successful PHI read. Pass only non-PHI
    values: ``patient_id`` is the opaque Blueprint client_id, never the
    patient's name/phone/DOB; ``detail`` is a short structured note.
    """
    try:
        _ensure_table()
        row = {
            "accessed_at": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "action": action,
            "clinic_id": clinic_id,
            "patient_id": str(patient_id) if patient_id is not None else None,
            "outcome": outcome,
            "detail": detail,
        }
        errors = bq_client.insert_rows_json(_TABLE_REF, [row])
        if errors:
            log.error(
                "phi_access_log insert errors clinic_id=%s action=%s errors=%s",
                clinic_id, action, errors,
            )
    except Exception:  # noqa: BLE001 — audit logging must never break a live call
        log.exception(
            "phi_access_log write failed clinic_id=%s action=%s", clinic_id, action,
        )
