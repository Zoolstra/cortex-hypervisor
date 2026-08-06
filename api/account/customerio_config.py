"""
Customer.io workspace configuration — per-clinic, dashboard-managed.

One Customer.io workspace per clinic; each workspace's Track API credentials
live in Google Secret Manager under the names the reactivation sync reads
(``api/services/customerio.py``):

    customerio-site-id-<clinic_id>
    customerio-track-api-key-<clinic_id>
    customerio-region-<clinic_id>      (optional; "us" default, "eu")

Nothing is stored in Cloud SQL — secret existence IS the enablement flag for
the clinic's Customer.io sync, exactly like ``datafeed-api-key-<instance_id>``.
Secrets are write-only from the dashboard: GET reports configured/not, never
values. DELETE removes the secrets, which disables the sync (live runs 409).

Mirrors the ``pms_config.py`` pattern (validated body, SM create-or-add-version,
``get_secret.cache_clear()`` so the running service picks up rotations).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.core.db import get_session
from api.core.orm import Clinic
from api.core.secrets import get_secret
from api.deps import require_read_access, require_write_access, verify_token
from api.models import CustomerIOConfigSet

router = APIRouter()


def _clinic_or_404(db: Session, clinic_id: str) -> Clinic:
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return clinic


def _secret_names(clinic_id: str) -> dict[str, str]:
    return {
        "site_id": f"customerio-site-id-{clinic_id}",
        "track_api_key": f"customerio-track-api-key-{clinic_id}",
        "region": f"customerio-region-{clinic_id}",
    }


def _write_secret(secret_id: str, value: str) -> None:
    """Create the secret or add a new version (rotation-safe)."""
    from google.cloud import secretmanager

    sm = secretmanager.SecretManagerServiceClient()
    project = "project-demo-2-482101"  # mirrors core/secrets.py
    parent = f"projects/{project}"
    secret_path = f"{parent}/secrets/{secret_id}"
    payload = {"data": value.encode("utf-8")}
    try:
        sm.get_secret(request={"name": secret_path})
        sm.add_secret_version(request={"parent": secret_path, "payload": payload})
    except Exception:
        sm.create_secret(request={
            "parent": parent, "secret_id": secret_id,
            "secret": {"replication": {"automatic": {}}},
        })
        sm.add_secret_version(request={"parent": secret_path, "payload": payload})
    get_secret.cache_clear()


def _delete_secret(secret_id: str) -> None:
    from google.cloud import secretmanager

    sm = secretmanager.SecretManagerServiceClient()
    project = "project-demo-2-482101"
    try:
        sm.delete_secret(request={
            "name": f"projects/{project}/secrets/{secret_id}"})
    except Exception:
        pass  # absent already — deletion is idempotent
    get_secret.cache_clear()


def _secret_exists(secret_id: str) -> bool:
    try:
        return bool(get_secret(secret_id))
    except Exception:
        return False


@router.get("/clinics/{clinic_id}/customerio")
def get_customerio_config(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Configuration status for the clinic's Customer.io workspace.

    Credentials are never returned — only whether each secret exists. The
    sync is enabled exactly when both site_id and track_api_key are set.
    """
    clinic = _clinic_or_404(db, clinic_id)
    require_read_access(clinic.instance_id, caller)

    names = _secret_names(clinic_id)
    site = _secret_exists(names["site_id"])
    key = _secret_exists(names["track_api_key"])
    region = "us"
    try:
        region = (get_secret(names["region"]) or "us").strip().lower()
    except Exception:
        pass
    return {
        "configured": site and key,
        "site_id_set": site,
        "track_api_key_set": key,
        "region": region,
    }


@router.post("/clinics/{clinic_id}/customerio")
def set_customerio_config(
    clinic_id: str,
    body: CustomerIOConfigSet,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Set or rotate the clinic's Customer.io workspace credentials.

    Blank/omitted fields keep their existing value, so site ID and API key
    can be rotated independently.
    """
    clinic = _clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    names = _secret_names(clinic_id)
    written = []
    if body.site_id and body.site_id.strip():
        _write_secret(names["site_id"], body.site_id.strip())
        written.append("site_id")
    if body.track_api_key and body.track_api_key.strip():
        _write_secret(names["track_api_key"], body.track_api_key.strip())
        written.append("track_api_key")
    if body.region:
        _write_secret(names["region"], body.region)
        written.append("region")
    if not written:
        raise HTTPException(status_code=400, detail="Nothing to save")

    return {"status": "success", "clinic_id": clinic_id, "written": written}


@router.delete("/clinics/{clinic_id}/customerio")
def clear_customerio_config(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Remove the clinic's Customer.io credentials — this DISABLES the sync
    (secret existence is the enablement flag; live runs will 409). The
    ``customerio_enrollments`` history is kept."""
    clinic = _clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    for secret_id in _secret_names(clinic_id).values():
        _delete_secret(secret_id)
    return {"status": "success", "clinic_id": clinic_id, "configured": False}
