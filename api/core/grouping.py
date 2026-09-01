"""
Whether an instance is a multi-location business.

This used to be a stored capability flag (`instances.multi_location_group`,
alembic 0013) that a super_admin toggled per client. That made it a second
statement of something the data already said, and the two could disagree: an
instance could grow from one clinic to five and the rollup would keep 404ing
until someone remembered the switch — which is exactly what happened to Calgary
Hearing Aid and Audiology after its four locations were provisioned.

So it is derived. A business with two or more clinics is a multi-location
business, and Group Intelligence is available to it.

The rule lives here rather than being spelled out at each gate because there are
six readers (four rollup endpoints, two payload fields) and a derivation
duplicated six times is a derivation that will eventually differ in one of them.

Deleted clinics do not count — they are not locations any more. `etl_enabled`
deliberately does NOT enter into it: whether a business *has* several locations
is a different question from whether we are currently ingesting for them, and
folding the second into the first would make the rollup vanish mid-onboarding
while clinics are switched on one at a time.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.core.orm import Clinic

#: A rollup across one location is just that location, so two is the threshold.
MIN_CLINICS_FOR_GROUP = 2


def clinic_count(db: Session, instance_id: str) -> int:
    """Live (non-deleted) clinics belonging to an instance."""
    return int(db.scalar(
        select(func.count(Clinic.clinic_id)).where(
            Clinic.instance_id == instance_id,
            Clinic.deleted_at.is_(None),
        )
    ) or 0)


def is_multi_location(db: Session, instance_id: str) -> bool:
    """Whether Group Intelligence applies to this instance."""
    return clinic_count(db, instance_id) >= MIN_CLINICS_FOR_GROUP


def is_multi_location_for_count(count: int | None) -> bool:
    """Same rule, for callers that already have the count in hand.

    Exists so a listing endpoint that has counted clinics anyway does not issue a
    second query per row, while still applying one definition of the rule.
    """
    return (count or 0) >= MIN_CLINICS_FOR_GROUP
