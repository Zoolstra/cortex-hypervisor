"""
Provisioning orchestration — Cloud SQL.

Creates Instance + Clinic ORM objects (and the 1:1 sub-tables that always
accompany a clinic) inside a SQLAlchemy session. Caller is responsible for
the commit, which gives the request handler control over transaction
boundaries.

Atomicity: a session.commit() either persists everything or nothing — Cloud
SQL gives us proper transactions, unlike the old BigQuery flow which had no
multi-statement transaction support and relied on best-effort compensating
deletes.
"""
import uuid

from sqlalchemy.orm import Session

from api.core.orm import (
    Clinic,
    ClinicLocationDetails,
    ClinicVoiceAgentConfiguration,
    Instance,
)
from api.voice_agent.defaults import seed_voice_agent_defaults


def provision_instance(
    db: Session,
    instance_name: str,
    primary_contact_name: str,
    primary_contact_email: str,
    primary_contact_uid: str,
) -> Instance:
    """Create + add an Instance to the session. Returns the ORM object."""
    instance = Instance(
        instance_id=str(uuid.uuid4()),
        instance_name=instance_name,
        primary_contact_name=primary_contact_name,
        primary_contact_email=primary_contact_email,
        primary_contact_uid=primary_contact_uid,
    )
    db.add(instance)
    return instance


def provision_clinic(
    db: Session,
    clinic_data: dict,
    instance_id: str,
) -> tuple[str, str | None]:
    """
    Create + add a Clinic plus its 1:1 sub-tables to the session.

    Args:
        clinic_data: ClinicCreate fields as dict (may include ref_id, which is
                     stripped and returned to the caller — never persisted).

    Returns:
        (clinic_id, ref_id)
    """
    ref_id = clinic_data.pop("ref_id", None)
    clinic_id = str(uuid.uuid4())

    db.add(Clinic(
        clinic_id=clinic_id,
        instance_id=instance_id,
        clinic_name=clinic_data["clinic_name"],
        address=clinic_data.get("address"),
        place_id=clinic_data.get("place_id"),
        country=clinic_data.get("country"),
        pms_type="none",
        etl_enabled=False,
    ))
    db.add(ClinicLocationDetails(
        clinic_id=clinic_id,
        hours_monday=clinic_data.get("hours_monday"),
        hours_tuesday=clinic_data.get("hours_tuesday"),
        hours_wednesday=clinic_data.get("hours_wednesday"),
        hours_thursday=clinic_data.get("hours_thursday"),
        hours_friday=clinic_data.get("hours_friday"),
        hours_saturday=clinic_data.get("hours_saturday"),
        hours_sunday=clinic_data.get("hours_sunday"),
        about_us=clinic_data.get("about_us"),
        phone=clinic_data.get("phone"),
        email=clinic_data.get("email"),
        time_zone=clinic_data.get("time_zone"),
    ))
    db.add(ClinicVoiceAgentConfiguration(clinic_id=clinic_id))

    # Seed the editable voice-agent defaults (persona, script extension
    # fields, three caller buckets) so admins land in the dashboard with
    # visible, editable content instead of empty sections.
    seed_voice_agent_defaults(db, clinic_id, only_if_missing=True)

    return clinic_id, ref_id


def provision_full_account(
    db: Session,
    instance_create: dict,
    clinics_create: list[dict],
    primary_contact_uid: str,
) -> dict:
    """
    Provision an instance + its clinics in a single transaction.

    The caller's session will commit on handler success or roll back on
    exception (api.core.db.get_session does this automatically).

    Returns:
        {
          "instance_id": "<uuid>",
          "clinic_id_map": {ref_id: clinic_id, ...},  # only entries with ref_id
        }
    """
    instance = provision_instance(
        db,
        instance_name=instance_create["instance_name"],
        primary_contact_name=instance_create["primary_contact_name"],
        primary_contact_email=instance_create["primary_contact_email"],
        primary_contact_uid=primary_contact_uid,
    )

    clinic_id_map: dict[str, str] = {}
    for clinic_data in clinics_create:
        clinic_id, ref_id = provision_clinic(db, clinic_data.copy(), instance.instance_id)
        if ref_id:
            clinic_id_map[ref_id] = clinic_id

    return {
        "instance_id": instance.instance_id,
        "clinic_id_map": clinic_id_map,
    }
