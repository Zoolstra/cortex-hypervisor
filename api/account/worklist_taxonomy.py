"""
Per-clinic worklist cohort taxonomy — config CRUD.

Defines the reactivation worklist cohorts (tested-not-sold, fitted-not-sold,
no-show, …) for a clinic. Because which Blueprint appointment ``event_type``s /
``status``es and invoice ``item_type``s mean what differs per clinic, this is
per-clinic config stored as a validated JSON blob on ``clinic_worklist_taxonomy``
(mirrors the ClinicProtocol.config validate-on-write pattern).

This module owns the Pydantic config models, the built-in default (which
reproduces the historic hard-coded ``%fit%`` / ``('ha','hao')`` behavior so
unconfigured clinics are unchanged), the ``resolve_taxonomy`` helper, and the
GET/PUT endpoints. The ``/worklists/...`` consumption + export endpoints live in
``api/worklists.py`` and import ``resolve_taxonomy`` / the models from here.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.orm import Session

from api.core.db import get_session
from api.core.orm import Clinic, ClinicWorklistTaxonomy
from api.deps import require_read_access, require_write_access, verify_token


router = APIRouter()

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# ── Config models ─────────────────────────────────────────────────────────────

class WorklistCohort(BaseModel):
    """One reactivation cohort definition.

    A cohort selects the most-recent qualifying appointment per patient by
    ``event_types`` (explicit, case-insensitive — the UI-editable form) OR the
    advanced ``event_like`` SQL LIKE pattern (used by the built-in default), and
    by ``statuses`` (e.g. Completed/Arrived, or "No show"). When
    ``require_no_sale`` is set, patients with a hearing-aid invoice line
    (``WorklistTaxonomyConfig.ha_item_types``) in the same window are excluded.
    """

    key: str
    label: str
    event_types: list[str] = []
    event_like: str | None = None
    statuses: list[str] = []
    require_no_sale: bool = False
    enabled: bool = True

    model_config = {"extra": "forbid"}

    @field_validator("key")
    @classmethod
    def _key_is_slug(cls, v: str) -> str:
        if not _KEY_RE.match(v):
            raise ValueError("key must be a slug: lowercase letter, then [a-z0-9_]")
        return v

    @model_validator(mode="after")
    def _check(self) -> "WorklistCohort":
        if not self.event_types and not self.event_like:
            raise ValueError(f"cohort '{self.key}': set event_types or event_like")
        if self.event_types and self.event_like:
            raise ValueError(f"cohort '{self.key}': set only one of event_types / event_like")
        if not self.statuses:
            raise ValueError(f"cohort '{self.key}': needs at least one appointment status")
        return self


class WorklistTaxonomyConfig(BaseModel):
    """A clinic's full worklist taxonomy: shared HA-sale item types + cohorts."""

    ha_item_types: list[str] = []
    cohorts: list[WorklistCohort] = []

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check(self) -> "WorklistTaxonomyConfig":
        keys = [c.key for c in self.cohorts]
        if len(keys) != len(set(keys)):
            raise ValueError("cohort keys must be unique")
        if any(c.require_no_sale for c in self.cohorts) and not self.ha_item_types:
            raise ValueError(
                "ha_item_types must be non-empty when any cohort has require_no_sale")
        return self

    def cohort(self, key: str) -> WorklistCohort | None:
        return next((c for c in self.cohorts if c.key == key), None)


# Built-in fallback for clinics with no config row. Reproduces exactly the
# historic behavior of intelligence_report/queries.py's hard-coded constants
# (_FITTING_EVENT_LIKE / _STATUS_COMPLETED / _HA_ITEM_TYPES) so the existing
# "Tested — not sold" worklist is unchanged for unconfigured clinics.
_DEFAULT_TAXONOMY = WorklistTaxonomyConfig(
    ha_item_types=["ha", "hao"],
    cohorts=[
        WorklistCohort(
            key="fitted_not_sold",
            label="Tested — not sold",
            event_like="%fit%",
            statuses=["Completed", "Arrived"],
            require_no_sale=True,
            enabled=True,
        ),
    ],
)


def resolve_taxonomy(clinic: Clinic) -> WorklistTaxonomyConfig:
    """The clinic's taxonomy config, or the built-in default when none is set.

    ``clinic`` is a loaded Clinic ORM object; its ``worklist_taxonomy``
    relationship is used (no extra query needed when eager/lazy-loaded).
    """
    row = clinic.worklist_taxonomy
    if row is not None and row.config:
        return WorklistTaxonomyConfig(**row.config)
    return _DEFAULT_TAXONOMY


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _clinic_or_404(db: Session, clinic_id: str) -> Clinic:
    clinic = db.get(Clinic, clinic_id)
    if not clinic or clinic.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return clinic


@router.get("/clinics/{clinic_id}/worklist-taxonomy")
def get_worklist_taxonomy(
    clinic_id: str,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Return the clinic's worklist taxonomy (or the built-in default).

    ``is_default`` is True when the clinic has no saved config yet.
    ``config_schema`` is the JSON schema, for the config UI to render against.
    """
    clinic = _clinic_or_404(db, clinic_id)
    require_read_access(clinic.instance_id, caller)

    row = db.get(ClinicWorklistTaxonomy, clinic_id)
    if row is not None and row.config:
        cfg = WorklistTaxonomyConfig(**row.config)
        is_default = False
    else:
        cfg = _DEFAULT_TAXONOMY
        is_default = True

    return {
        "config": cfg.model_dump(),
        "config_schema": WorklistTaxonomyConfig.model_json_schema(),
        "is_default": is_default,
    }


@router.put("/clinics/{clinic_id}/worklist-taxonomy")
def set_worklist_taxonomy(
    clinic_id: str,
    body: WorklistTaxonomyConfig,
    caller: dict = Depends(verify_token),
    db: Session = Depends(get_session),
):
    """Validate (via the model) and upsert the clinic's worklist taxonomy."""
    clinic = _clinic_or_404(db, clinic_id)
    require_write_access(clinic.instance_id, caller)

    row = db.get(ClinicWorklistTaxonomy, clinic_id)
    if row is None:
        row = ClinicWorklistTaxonomy(clinic_id=clinic_id)
        db.add(row)
    row.config = body.model_dump()
    row.updated_by = caller.get("email") or caller.get("uid")

    return {"status": "success", "clinic_id": clinic_id, "config": body.model_dump()}
