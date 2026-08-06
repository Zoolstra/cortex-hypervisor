"""invoca_promo_numbers: registry of call-tracking numbers per Invoca campaign

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-30

Creates ``invoca_promo_numbers``, the per-campaign registry of Invoca promo
(call-tracking) numbers. Until now campaign attribution rested on an unmodeled
assumption — "each market's tracking number belongs to that market's one
campaign" — that neither Google Ads nor Invoca enforces (the retired Earlens
account had Freehold ads carrying Princeton's number). This table makes the
number→campaign mapping an audited invariant:

- ``promo_number`` is UNIQUE globally: a tracking number routes to exactly one
  Invoca campaign, so a second row would make attribution ambiguous — the
  exact failure this registry exists to prevent.
- ``invoca_promo_id`` (Invoca's own id for the number) is UNIQUE for idempotent
  sync upserts.
- Rows FK to ``invoca_campaigns.id`` (not the raw campaign id) so the clinic
  linkage is unambiguous even if an Invoca campaign were ever mapped to two
  clinics.

Source of truth is the Invoca API (``advertisers/{id}/advertiser_campaigns/
{cid}/promo_numbers.json``); ``configure_promo_numbers.py`` syncs it into this
table and audits it against the Google Ads call assets (the number actually
shown on each ad). ``media_type`` distinguishes ad-extension numbers
("Google Call Extension") from GMB-listing / website-pool numbers, and
``adwords_account_id`` carries Invoca's own record of which Google Ads account
the number serves. No seed rows — population is API-driven via the script so
the registry always reflects what Invoca actually has.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "invoca_promo_numbers",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("invoca_campaign_row_id", sa.BigInteger, nullable=False, index=True),
        sa.Column("invoca_promo_id", sa.String(32), nullable=False),
        sa.Column("promo_number", sa.String(20), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("media_type", sa.String(64), nullable=True),
        sa.Column("promo_type", sa.String(32), nullable=True),
        sa.Column("adwords_account_id", sa.String(20), nullable=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp(),
                  server_onupdate=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["invoca_campaign_row_id"], ["invoca_campaigns.id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("promo_number", name="uq_promo_number"),
        sa.UniqueConstraint("invoca_promo_id", name="uq_invoca_promo_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("invoca_promo_numbers")
