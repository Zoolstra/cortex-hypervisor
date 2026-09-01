"""
Group Intelligence availability is derived from the clinic count.

It used to be a stored flag a super_admin toggled. That restated what the data
already said and could disagree with it: an instance provisioned with four
locations kept 404ing its rollup because nobody flipped the switch. These tests
pin the derivation and, more importantly, pin that the stored column no longer
influences anything — a leftover value must not resurrect the old behaviour.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api.core.grouping import (
    MIN_CLINICS_FOR_GROUP, clinic_count, is_multi_location,
    is_multi_location_for_count,
)
from api.core.orm import Base, Clinic, Instance

INSTANCE = "INST"


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Instance(instance_id=INSTANCE, instance_name="Multi Co"))
    session.commit()
    yield session
    session.close()


def _add_clinic(db, name, **kw):
    c = Clinic(clinic_id=f"C_{name}", instance_id=INSTANCE, clinic_name=name, **kw)
    db.add(c)
    db.commit()
    return c


def test_threshold_is_two(db):
    assert is_multi_location(db, INSTANCE) is False
    _add_clinic(db, "One")
    assert is_multi_location(db, INSTANCE) is False
    _add_clinic(db, "Two")
    assert is_multi_location(db, INSTANCE) is True
    assert MIN_CLINICS_FOR_GROUP == 2


def test_deleted_clinics_do_not_count(db):
    """A closed location is not a location."""
    from datetime import datetime
    _add_clinic(db, "Open")
    gone = _add_clinic(db, "Closed")
    assert is_multi_location(db, INSTANCE) is True
    gone.deleted_at = datetime(2026, 1, 1)
    db.commit()
    assert clinic_count(db, INSTANCE) == 1
    assert is_multi_location(db, INSTANCE) is False


def test_etl_disabled_clinics_still_count(db):
    """Whether a business HAS several locations is a different question from
    whether we are currently ingesting for them. Folding the second in would make
    the rollup vanish mid-onboarding, while clinics are switched on one at a time.
    """
    _add_clinic(db, "Live", etl_enabled=True)
    _add_clinic(db, "NotYet", etl_enabled=False)
    assert is_multi_location(db, INSTANCE) is True


def test_the_stored_column_is_ignored_in_both_directions(db):
    """The whole point of deriving it. A stale 1 must not enable a rollup that
    would aggregate a single clinic, and a stale 0 must not suppress a real one."""
    instance = db.get(Instance, INSTANCE)

    _add_clinic(db, "Only")
    instance.multi_location_group = True
    db.commit()
    assert is_multi_location(db, INSTANCE) is False

    _add_clinic(db, "Second")
    instance.multi_location_group = False
    db.commit()
    assert is_multi_location(db, INSTANCE) is True


def test_count_shortcut_matches_the_query(db):
    """Listing endpoints pass a count they already have; it must apply the same
    rule rather than a second copy of it."""
    for n in range(4):
        assert is_multi_location_for_count(n) is (n >= MIN_CLINICS_FOR_GROUP)
    assert is_multi_location_for_count(None) is False

    _add_clinic(db, "A")
    _add_clinic(db, "B")
    assert is_multi_location_for_count(clinic_count(db, INSTANCE)) \
        is is_multi_location(db, INSTANCE)
