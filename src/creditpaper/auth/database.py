import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import text

from creditpaper.auth.config import Settings, get_settings

settings = get_settings()

# Supabase session pooler caps the project at 15 server connections shared by
# the API, the worker and scripts. The psycopg pool (creditpaper.db) takes its
# share in the API process; this engine gets a small slice with no overflow so
# the API process stays inside its budget.
_SA_POOL_SIZE = max(1, int(os.getenv("CREDITPAPER_SA_POOL_SIZE", "2")))

engine = create_async_engine(
    Settings.get_database_url(settings.DATABASE_URL),
    pool_size=_SA_POOL_SIZE,
    max_overflow=0,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args={"statement_cache_size": 0},
)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def verify_async_runtime_database_role() -> None:
    """Apply the runtime-role safety gate to the async authentication pool."""
    query = text(
        """
        SELECT r.rolname, r.rolsuper, r.rolbypassrls,
               EXISTS (
                   SELECT 1 FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relowner = r.oid
                     AND EXISTS (
                         SELECT 1 FROM pg_attribute a
                         WHERE a.attrelid = c.oid AND a.attname = 'tenant_id'
                           AND NOT a.attisdropped
                     )
               ) AS owns_tenant_table
        FROM pg_roles r
        WHERE r.rolname IN (session_user, current_user)
        """
    )
    async with engine.connect() as connection:
        rows = (await connection.execute(query)).all()
    if not rows:
        raise RuntimeError("Async database runtime identity could not be inspected")
    unsafe = [row.rolname for row in rows if row.rolsuper or row.rolbypassrls or row.owns_tenant_table]
    if unsafe:
        names = ", ".join(sorted(set(unsafe)))
        raise RuntimeError(f"Unsafe async database runtime role: {names}")


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
