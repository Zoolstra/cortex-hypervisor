"""
SQLAlchemy engine + session factory for the Cloud SQL (MySQL 8) config store.

Connects via google-cloud-sql-python-connector with IAM database authentication.
No passwords anywhere — the IAM identity from Application Default Credentials
authenticates to the database.

Identity → DB user mapping (Cloud SQL MySQL IAM auth):
    Service account: the user is the SA email WITHOUT `.gserviceaccount.com`,
                     e.g. cortex-accounts-cloudsql-sa@project-demo-2-482101.iam
    User account:    the user is the full email, e.g. will@zoolstra.com

Production runs as the SA (`cortex-accounts-cloudsql-sa@…`). Local dev needs
either SA impersonation:

    gcloud auth application-default login --impersonate-service-account=\\
      cortex-accounts-cloudsql-sa@project-demo-2-482101.iam.gserviceaccount.com

…or override CLOUD_SQL_IAM_USER to the developer's own gcloud identity (and
ensure that identity has been added as an IAM DB user with the right grants).

Setup the instance once:
    1. Create the database:
         gcloud sql databases create cortex --instance=cortex-accounts
    2. Add the SA as an IAM DB user:
         gcloud sql users create \\
           cortex-accounts-cloudsql-sa@project-demo-2-482101.iam.gserviceaccount.com \\
           --instance=cortex-accounts --type=cloud_iam_service_account
    3. Grant DDL/DML on the database (run once as a built-in admin user):
         GRANT ALL PRIVILEGES ON cortex.*
           TO 'cortex-accounts-cloudsql-sa@project-demo-2-482101.iam';

Usage:
    from fastapi import Depends
    from sqlalchemy.orm import Session
    from api.core.db import get_session

    @router.get("/clinics/{clinic_id}")
    def get_clinic(clinic_id: str, db: Session = Depends(get_session)):
        return db.get(Clinic, clinic_id)
"""
import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

import pymysql
from google.cloud.sql.connector import Connector, IPTypes
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


INSTANCE_CONNECTION_NAME = "project-demo-2-482101:us-central1:cortex-accounts"
DB_NAME = "clients"

# Final fallback when neither CLOUD_SQL_IAM_USER is set nor ADC resolves to a
# service account. Matches the prod Cloud Run service identity.
_DEFAULT_IAM_USER = "cortex-hypervisor-sa@project-demo-2-482101.iam"

_METADATA_SA_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/email"
)

# IPType: PUBLIC for local dev (the only path that works without a VPC connector);
# PRIVATE in Cloud Run once private IP is wired. Toggle via env var.
_IP_TYPE = IPTypes.PRIVATE if os.environ.get("CLOUD_SQL_USE_PRIVATE_IP") else IPTypes.PUBLIC


@lru_cache(maxsize=1)
def _resolve_iam_user() -> str:
    """
    Pick the IAM DB user to authenticate as. Cloud SQL MySQL IAM auth requires
    the username to match the calling identity exactly:

      1. CLOUD_SQL_IAM_USER env override (local dev / explicit override)
      2. GCE/Cloud Run metadata server — authoritative when running on GCP
      3. ADC service-account email (gcloud SA impersonation on dev boxes)
      4. Hardcoded default (the prod SA)

    The metadata server is checked before ADC because google.auth's
    `compute_engine.Credentials.service_account_email` returns the literal
    string ``"default"`` until the credentials refresh, which silently sends
    the wrong username on Cloud Run cold starts.
    """
    override = os.environ.get("CLOUD_SQL_IAM_USER")
    if override:
        return override

    try:
        import urllib.request
        req = urllib.request.Request(
            _METADATA_SA_URL, headers={"Metadata-Flavor": "Google"}
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            email = resp.read().decode().strip()
        if email and "@" in email:
            return email.removesuffix(".gserviceaccount.com")
    except Exception:
        pass

    try:
        from google.auth import default as ga_default
        creds, _ = ga_default()
        sa_email = getattr(creds, "service_account_email", None)
        if sa_email and "@" in sa_email:
            return sa_email.removesuffix(".gserviceaccount.com")
    except Exception:
        pass

    return _DEFAULT_IAM_USER


@lru_cache(maxsize=1)
def _get_connector() -> Connector:
    return Connector()


def _getconn() -> pymysql.connections.Connection:
    """Connection factory passed to SQLAlchemy's `creator`."""
    connector = _get_connector()
    return connector.connect(
        INSTANCE_CONNECTION_NAME,
        "pymysql",
        user=_resolve_iam_user(),
        db=DB_NAME,
        enable_iam_auth=True,
        ip_type=_IP_TYPE,
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Lazy singleton engine. Pool tuned for FastAPI workers."""
    return create_engine(
        "mysql+pymysql://",
        creator=_getconn,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,  # under MySQL's default wait_timeout
        future=True,
    )


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


def get_session() -> Iterator[Session]:
    """
    FastAPI dependency. Yields a session, commits on success, rolls back on
    exception, always closes.

        @router.get("/...")
        def handler(db: Session = Depends(get_session)): ...
    """
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """
    Context-manager equivalent for use outside FastAPI handlers (scripts,
    background jobs, tests). Same commit/rollback semantics as get_session.
    """
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
