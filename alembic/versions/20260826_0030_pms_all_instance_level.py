"""PMS config is instance-level for every vendor; the location map is the only clinic-level part

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-26

0028 added account-level PMS config *alongside* the per-clinic tables, with the
clinic winning. That left two places to configure one thing, and the losing one
invisible: a clinic wired directly kept claiming its whole account's feed while
the account config sat there looking correct.

This makes the account the only place PMS credentials live, for Blueprint and
CounselEar alike, and reduces the clinic-level part to the one thing that is
genuinely per-clinic — which vendor location feeds it.

    instance_pms_config     credentials, URLs, feed identifiers, fallback clinic
                            + CounselEar's account-level feed fields, which were
                              duplicated onto every clinic row of a practice
    pms_clinic_locations    vendor location -> clinic, plus the two settings that
                            are per-clinic rather than per-account:
                              prompt_for_location, booking_user_id

CounselEar was already three-quarters of the way here and is what proves the
shape: ``counselear_location_code`` (the SFTP folder) and
``counselear_sftp_username`` are facts about the *practice*, and were copied
identically onto all seven Virsono clinic rows; ``counselear_clinic_id`` is the
vendor's per-row location key and becomes ``vendor_location_key``.

**The catch-all key.** A single-location account still needs a map row, or
"configured" and "ingesting" come apart again. ``vendor_location_key = '*'``
means "every row of this feed belongs to this clinic" — which is precisely what
a one-clinic account means, and what every migrated Blueprint clinic gets here.
It must be the only row for its account: a catch-all next to specific keys has
no defined meaning, and the API rejects the combination.

Migrating a clinic to a ``'*'`` row is deliberately **behaviour-preserving**.
Calgary's Heritage clinic currently holds the config for a five-location account
and receives all five sites' rows; after this it still does, via one catch-all.
Splitting it is a config change made in the dashboard, not something a schema
migration should do silently.

Conflicts **fail the upgrade** rather than resolve themselves. If two clinics of
one instance carry different account config for the same PMS, only one can
survive and guessing would discard a client's credentials — so the migration
stops and names them.

The old tables are left in place, marked deprecated via a table COMMENT, and read
by nothing. Dropping them is a follow-up once this is proven in production; doing
it here would remove the only way back.

Secrets are NOT touched — a migration cannot write Secret Manager. The ETL and
the voice agent read the instance scope first and fall back to a mapped clinic's
own secret with a loud warning, so this migration does not have to be sequenced
against a secret copy. ``scripts/copy_pms_secrets_to_instance.py`` retires the
fallback.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text


revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CATCH_ALL = "*"

_DEPRECATED = (
    "DEPRECATED by alembic 0030 — PMS config moved to instance_pms_config + "
    "pms_clinic_locations. Read by nothing; kept as the rollback path."
)


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. Account-level columns for CounselEar ────────────────────────────────
    # Both are facts about the practice, not the clinic: the SFTP folder its feed
    # lands in, and the login it is delivered under.
    op.add_column("instance_pms_config",
                  sa.Column("counselear_location_code", sa.String(64), nullable=True))
    op.add_column("instance_pms_config",
                  sa.Column("counselear_sftp_username", sa.String(64), nullable=True))

    # ── 2. The per-clinic PMS settings, onto the mapping row ──────────────────
    op.add_column("pms_clinic_locations",
                  sa.Column("prompt_for_location", sa.Boolean, nullable=False,
                            server_default="0"))
    op.add_column("pms_clinic_locations",
                  sa.Column("booking_user_id", sa.Integer, nullable=True))

    # ── 3. Refuse to guess between conflicting per-clinic configs ─────────────
    conflicts = bind.execute(text("""
        SELECT c.instance_id,
               COUNT(DISTINCT CONCAT_WS('|',
                   COALESCE(b.clinic_code, ''), COALESCE(b.api_url, ''),
                   COALESCE(b.aws_url, ''))) AS variants,
               GROUP_CONCAT(c.clinic_name SEPARATOR ', ') AS clinics
        FROM clinic_blueprint_config b
        JOIN clinics c ON c.clinic_id = b.clinic_id
        WHERE c.deleted_at IS NULL
        GROUP BY c.instance_id
        HAVING variants > 1
    """)).all()
    if conflicts:
        detail = "; ".join(
            f"instance {r._mapping['instance_id']} ({r._mapping['clinics']}) has "
            f"{r._mapping['variants']} different Blueprint configs"
            for r in conflicts
        )
        raise RuntimeError(
            "Cannot migrate to instance-level PMS config: " + detail + ". "
            "One account config survives per instance, so resolve these by hand "
            "(keep the correct clinic's row, delete the others) and re-run."
        )

    ce_conflicts = bind.execute(text("""
        SELECT c.instance_id,
               COUNT(DISTINCT CONCAT_WS('|',
                   COALESCE(cc.counselear_location_code, ''),
                   COALESCE(cc.counselear_sftp_username, ''))) AS variants,
               GROUP_CONCAT(c.clinic_name SEPARATOR ', ') AS clinics
        FROM clinic_counselear_config cc
        JOIN clinics c ON c.clinic_id = cc.clinic_id
        WHERE c.deleted_at IS NULL
        GROUP BY c.instance_id
        HAVING variants > 1
    """)).all()
    if ce_conflicts:
        detail = "; ".join(
            f"instance {r._mapping['instance_id']} ({r._mapping['clinics']}) has "
            f"{r._mapping['variants']} different CounselEar practice configs"
            for r in ce_conflicts
        )
        raise RuntimeError(
            "Cannot migrate to instance-level PMS config: " + detail + ". "
            "location_code and sftp_username are per-practice; if one instance "
            "genuinely has two practices they need two instances."
        )

    # ── 4. Blueprint: one account config per instance, catch-all map rows ─────
    # COALESCE keeps whatever 0028-era account config already exists rather than
    # overwriting it with a clinic's copy — Calgary was configured through the
    # dashboard before this ran.
    bind.execute(text(f"""
        INSERT INTO instance_pms_config
            (instance_id, pms_type, clinic_code, api_url, aws_url, primary_clinic_id)
        SELECT c.instance_id, 'blueprint',
               MAX(b.clinic_code), MAX(b.api_url), MAX(b.aws_url),
               MIN(b.clinic_id)
        FROM clinic_blueprint_config b
        JOIN clinics c ON c.clinic_id = b.clinic_id
        WHERE c.deleted_at IS NULL
        GROUP BY c.instance_id
        ON DUPLICATE KEY UPDATE
            clinic_code = COALESCE(instance_pms_config.clinic_code, VALUES(clinic_code)),
            api_url     = COALESCE(instance_pms_config.api_url,     VALUES(api_url)),
            aws_url     = COALESCE(instance_pms_config.aws_url,     VALUES(aws_url)),
            primary_clinic_id = COALESCE(instance_pms_config.primary_clinic_id,
                                         VALUES(primary_clinic_id))
    """))

    # One catch-all row per migrated clinic, carrying its two per-clinic settings.
    # Skipped for any clinic the dashboard has already mapped to real locations —
    # adding a catch-all beside those would be the ambiguous state the API rejects.
    bind.execute(text("""
        INSERT INTO pms_clinic_locations
            (instance_id, pms_type, vendor_location_key, clinic_id, location_name,
             active, prompt_for_location, booking_user_id)
        SELECT c.instance_id, 'blueprint', :catch_all, c.clinic_id, c.clinic_name,
               1, b.prompt_for_location, b.user_id
        FROM clinic_blueprint_config b
        JOIN clinics c ON c.clinic_id = b.clinic_id
        WHERE c.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM pms_clinic_locations l
              WHERE l.instance_id = c.instance_id AND l.pms_type = 'blueprint'
          )
        ON DUPLICATE KEY UPDATE
            prompt_for_location = VALUES(prompt_for_location),
            booking_user_id     = VALUES(booking_user_id)
    """), {"catch_all": CATCH_ALL})

    # A clinic already mapped by hand still needs its per-clinic settings carried
    # across, matched on the clinic rather than the location key.
    bind.execute(text("""
        UPDATE pms_clinic_locations l
        JOIN clinic_blueprint_config b ON b.clinic_id = l.clinic_id
        SET l.prompt_for_location = b.prompt_for_location,
            l.booking_user_id     = b.user_id
        WHERE l.pms_type = 'blueprint'
    """))

    # ── 5. CounselEar: practice config up, per-row clinic id becomes the key ──
    bind.execute(text("""
        INSERT INTO instance_pms_config
            (instance_id, pms_type, counselear_location_code,
             counselear_sftp_username, primary_clinic_id)
        SELECT c.instance_id, 'counselear',
               MAX(cc.counselear_location_code), MAX(cc.counselear_sftp_username),
               MIN(cc.clinic_id)
        FROM clinic_counselear_config cc
        JOIN clinics c ON c.clinic_id = cc.clinic_id
        WHERE c.deleted_at IS NULL
        GROUP BY c.instance_id
        ON DUPLICATE KEY UPDATE
            counselear_location_code = COALESCE(
                instance_pms_config.counselear_location_code,
                VALUES(counselear_location_code)),
            counselear_sftp_username = COALESCE(
                instance_pms_config.counselear_sftp_username,
                VALUES(counselear_sftp_username)),
            primary_clinic_id = COALESCE(instance_pms_config.primary_clinic_id,
                                         VALUES(primary_clinic_id))
    """))

    # CounselEar already tags every feed row with its own clinic id, so unlike
    # Blueprint these map to real keys, not a catch-all.
    bind.execute(text("""
        INSERT INTO pms_clinic_locations
            (instance_id, pms_type, vendor_location_key, clinic_id, location_name, active)
        SELECT c.instance_id, 'counselear', cc.counselear_clinic_id,
               c.clinic_id, c.clinic_name, 1
        FROM clinic_counselear_config cc
        JOIN clinics c ON c.clinic_id = cc.clinic_id
        WHERE c.deleted_at IS NULL
          AND cc.counselear_clinic_id IS NOT NULL
          AND cc.counselear_clinic_id <> ''
        ON DUPLICATE KEY UPDATE
            clinic_id = VALUES(clinic_id),
            location_name = VALUES(location_name)
    """))

    # ── 6. Mark the old tables as the rollback path, not a source of truth ────
    for table in ("clinic_blueprint_config", "clinic_counselear_config"):
        op.execute(f"ALTER TABLE {table} COMMENT = '{_DEPRECATED}'")


def downgrade() -> None:
    # The per-clinic tables were never emptied, so going back is a matter of
    # removing what this added rather than reconstructing them.
    op.execute("DELETE FROM pms_clinic_locations WHERE pms_type = 'counselear'")
    op.execute(f"DELETE FROM pms_clinic_locations WHERE vendor_location_key = '{CATCH_ALL}'")
    op.execute("DELETE FROM instance_pms_config WHERE pms_type = 'counselear'")

    op.drop_column("pms_clinic_locations", "booking_user_id")
    op.drop_column("pms_clinic_locations", "prompt_for_location")
    op.drop_column("instance_pms_config", "counselear_sftp_username")
    op.drop_column("instance_pms_config", "counselear_location_code")

    for table in ("clinic_blueprint_config", "clinic_counselear_config"):
        op.execute(f"ALTER TABLE {table} COMMENT = ''")
