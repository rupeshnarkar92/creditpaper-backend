"""Alembic env.py — reads DB URL, imports all models for autogenerate."""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the project importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

# Load .env so DATABASE_URL is available even when alembic is invoked directly
try:
    from dotenv import load_dotenv

    from creditpaper.env_file import env_file_path

    # Same rule as db.py and auth/config.py: APP_ENV=local reads `.env.local`, otherwise `.env`.
    load_dotenv(env_file_path())
except ImportError:  # dotenv is optional for alembic
    pass

# Import Base and ALL models so autogenerate sees them
from creditpaper.auth.database import Base  # noqa: E402
from creditpaper.auth.models import activity_log, domain, invitation, user  # noqa: E402, F401

# Read DB URL from env or use the one in alembic.ini
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Resolve sqlalchemy.url: prefer DATABASE_URL, fall back to alembic.ini value
db_url = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL")
if not db_url:
    db_url = config.get_main_option("sqlalchemy.url")
if db_url:
    # If the URL is in the libpq keyword form (no scheme) leave it alone.
    # If it's a URL with postgresql+driver, normalise to postgresql+psycopg for
    # Alembic which uses sync drivers.
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    # Supabase requires SSL — append sslmode=require if missing
    dsn_lower = db_url.lower()
    needs_ssl = "supabase" in dsn_lower
    has_ssl = "sslmode=" in dsn_lower or "ssl=" in dsn_lower
    if needs_ssl and not has_ssl:
        sep = "&" if "?" in db_url else "?"
        db_url = f"{db_url}{sep}sslmode=require"
    config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        # The full repository grant sweep is useful for legacy migrations, but
        # every new migration grants its own objects and should not wait behind
        # unrelated table locks. Deployment can disable the redundant sweep;
        # it remains enabled by default for backwards compatibility.
        if os.getenv("CREDITPAPER_ALEMBIC_GRANT_SWEEP", "1") == "1":
            from creditpaper.alembic.grants import ensure_runtime_grants

            ensure_runtime_grants(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
