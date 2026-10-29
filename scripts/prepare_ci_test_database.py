"""Prepare a disposable PostgreSQL database for the CI test suite."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    dsn = os.environ["DATABASE_URL"]
    target = urlsplit(dsn)
    if target.hostname not in {"localhost", "127.0.0.1", "::1", "postgres"} or not target.path.endswith(
        "_test"
    ):
        raise RuntimeError("CI database preparation is restricted to a local database ending in _test")

    os.environ["APP_ENV"] = "test"
    os.environ["MIGRATION_DATABASE_URL"] = dsn

    # Several migrations conditionally grant function privileges to this role,
    # while the post-migration grant reconciler needs it to exist as well.
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN "
            "CREATE ROLE creditpaper_runtime NOLOGIN NOSUPERUSER NOBYPASSRLS; "
            "END IF; END $$"
        )

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "src" / "creditpaper" / "alembic"))
    command.upgrade(config, "head")

    with psycopg.connect(dsn) as conn:
        conn.execute((ROOT / "scripts" / "provision_runtime_role.sql").read_text(encoding="utf-8"))

    # Alembic's post-migration reconciler applies the general CRUD default. Keep
    # document versions immutable even when an already-migrated database is
    # provisioned again.
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(sqlalchemy_dsn)
    with engine.begin() as conn:
        conn.execute(text("REVOKE UPDATE ON TABLE public.deal_document_versions FROM creditpaper_runtime"))
        conn.execute(
            text("GRANT SELECT, INSERT, DELETE ON TABLE public.deal_document_versions TO creditpaper_runtime")
        )
    engine.dispose()


if __name__ == "__main__":
    main()
