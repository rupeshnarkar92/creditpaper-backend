from __future__ import annotations

import atexit
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

from creditpaper.env_file import env_file_path

# Env loading, one rule for every entry point (`env_file.py`): APP_ENV=local reads
# `.env.local`, otherwise `.env`. Both are gitignored; never commit either.
load_dotenv(env_file_path())

import psycopg  # noqa: E402
from psycopg.pq import TransactionStatus  # noqa: E402
from psycopg_pool import ConnectionPool  # noqa: E402


def _build_dsn() -> str:
    """Resolve the PostgreSQL DSN.

    Prefers a single DATABASE_URL (Supabase, Render, etc.). Falls back to
    individual DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD for local
    development. The raw psycopg pool needs a `postgresql://` URL, so any
    SQLAlchemy-style `+asyncpg` / `+psycopg` driver suffix is stripped. If the
    host looks like Supabase and the URL has no SSL parameter, appends
    `sslmode=require` because Supabase rejects plain TCP.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        dsn = (
            f"host={os.getenv('DB_HOST', 'localhost')} "
            f"port={os.getenv('DB_PORT', '5432')} "
            f"dbname={os.getenv('DB_NAME', 'postgres')} "
            f"user={os.getenv('DB_USER', 'postgres')} "
            f"password={os.getenv('DB_PASSWORD', '')}"
        )
    port_override = os.getenv("CREDITPAPER_DB_PORT_OVERRIDE", "").strip()
    if port_override and "://" in dsn:
        parsed = urlsplit(dsn)
        credentials, separator, host_port = parsed.netloc.rpartition("@")
        host = host_port.rsplit(":", 1)[0] if ":" in host_port else host_port
        authority = f"{host}:{int(port_override)}"
        if separator:
            authority = f"{credentials}@{authority}"
        dsn = urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, parsed.fragment))
    # Strip SQLAlchemy driver suffix — psycopg expects plain postgresql://
    for driver_suffix in ("+asyncpg", "+psycopg", "+psycopg2"):
        if driver_suffix in dsn:
            dsn = dsn.replace(driver_suffix, "", 1)
    dsn_lower = dsn.lower()
    needs_ssl = "supabase" in dsn_lower or "pooler.supabase" in dsn_lower
    has_sslmode = "sslmode=" in dsn_lower
    # JDBC-style ?ssl=require → psycopg-style sslmode=require
    if "ssl=require" in dsn_lower and not has_sslmode:
        dsn = dsn.replace("ssl=require", "sslmode=require")
    if needs_ssl and not has_sslmode:
        sep = "&" if "?" in dsn else "?"
        dsn = f"{dsn}{sep}sslmode=require"
    # TCP keepalive — reduces pooler disconnects on idle connections.
    if "keepalives=" not in dsn.lower():
        sep = "&" if "?" in dsn else "?"
        dsn = f"{dsn}{sep}keepalives=1&keepalives_idle=60&keepalives_interval=10&keepalives_count=3"
    return dsn


_DSN = _build_dsn()

_POOL: ConnectionPool | None = None
_POOL_LOCK = threading.Lock()


def _get_or_create_pool() -> ConnectionPool:
    global _POOL
    with _POOL_LOCK:
        return _open_pool()


def _reset_session_state(conn: psycopg.Connection[Any]) -> None:
    """Clear session-level state before a connection goes back to the pool.

    `SET LOCAL` is cleared by PostgreSQL at transaction end, but a plain `SET`
    (or `set_config(..., false)`) survives on the connection itself. Without
    this callback psycopg-pool returns an `IDLE` connection untouched, so any
    session-level `app.tenant_id` would be inherited by the next borrower of
    that physical connection and every RLS policy would evaluate against the
    wrong tenant. `RESET ALL` is safe here: connections are autocommit and
    `prepare_threshold=0`, so there is no prepared-statement state to lose, and
    no code path relies on a session-level `SET` persisting across borrows —
    the only such use was the leak this closes.
    """
    if conn.info.transaction_status is not TransactionStatus.IDLE:
        return
    with conn.cursor() as cur:
        cur.execute("RESET ALL")


# Supabase session-mode pooler caps this project at 15 server connections.
POOL_CONNECTION_BUDGET = 15
_API_POOL_MAX = max(1, int(os.getenv("CREDITPAPER_DB_POOL_MAX", "8")))
_WORKER_POOL_MAX = max(2, int(os.getenv("CREDITPAPER_WORKER_DB_POOL_MAX", "2")))
_WORKER_REPLICAS = max(1, int(os.getenv("CREDITPAPER_WORKER_REPLICAS", "2")))
_PROCESS_ROLE = os.getenv("CREDITPAPER_PROCESS_ROLE", "api").strip().lower()
_WORKER_TASK_ACTIVE: ContextVar[bool] = ContextVar("creditpaper_worker_db_task", default=False)
# Work transactions are admitted separately from compute concurrency. One
# pooled session remains available to discovery and batched lease renewal.
# Keep one worker connection available for lease renewal, heartbeats and
# successor discovery. A one-connection worker makes the job and heartbeat
# race the same checkout, turning healthy work into PoolTimeout retries.
_WORKER_DB_SLOTS = threading.BoundedSemaphore(_WORKER_POOL_MAX - 1)


def assert_pool_budget(api_max: int, worker_max: int, worker_replicas: int = 1) -> None:
    """Fail fast when all process pools plus two spare sessions exceed the cap."""
    if api_max < 1 or worker_max < 1 or worker_replicas < 1:
        raise RuntimeError("DB pool sizes and worker replica count must be positive")
    total_worker = worker_max * worker_replicas
    total = api_max + total_worker + 2
    if total > POOL_CONNECTION_BUDGET:
        raise RuntimeError(
            f"DB pool budget exceeded: API({api_max}) + "
            f"WORKER({worker_max} x {worker_replicas}) + 2 = {total} "
            f"> {POOL_CONNECTION_BUDGET}. Lower CREDITPAPER_DB_POOL_MAX, "
            "CREDITPAPER_WORKER_DB_POOL_MAX, or CREDITPAPER_WORKER_REPLICAS."
        )


def configured_pool_limits() -> tuple[int, int]:
    """Return the validated API and worker connection budgets."""
    assert_pool_budget(_API_POOL_MAX, _WORKER_POOL_MAX, _WORKER_REPLICAS)
    return _API_POOL_MAX, _WORKER_POOL_MAX


def _open_pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        # Keep the API pool below the server cap while allowing the dashboard
        # read to proceed when onboarding makes several concurrent reads.
        assert_pool_budget(_API_POOL_MAX, _WORKER_POOL_MAX, _WORKER_REPLICAS)
        if _PROCESS_ROLE not in {"api", "worker"}:
            raise RuntimeError("CREDITPAPER_PROCESS_ROLE must be 'api' or 'worker'")
        pool_max = _WORKER_POOL_MAX if _PROCESS_ROLE == "worker" else _API_POOL_MAX
        _POOL = ConnectionPool(
            conninfo=_DSN,
            min_size=1,
            max_size=pool_max,
            # Short acquire timeout: a starved pool returns 503 + Retry-After (api/errors.py)
            # instead of hanging the request for 15s.
            timeout=float(os.getenv("CREDITPAPER_DB_POOL_TIMEOUT", "4")),
            max_waiting=100,
            open=True,
            check=ConnectionPool.check_connection,
            reset=_reset_session_state,
            # Transaction-pooler sessions can move between server connections;
            # client-side prepared statements would then collide or disappear.
            kwargs={"autocommit": True, "prepare_threshold": None},
        )
    return _POOL


def get_pool() -> ConnectionPool:
    return _get_or_create_pool()


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    pool = get_pool()
    reserve = _PROCESS_ROLE == "worker" and _WORKER_TASK_ACTIVE.get()
    if reserve:
        _WORKER_DB_SLOTS.acquire()
    try:
        with pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    yield cur
    finally:
        if reserve:
            _WORKER_DB_SLOTS.release()


@contextmanager
def worker_db_task() -> Iterator[None]:
    """Mark a worker job so all nested/fan-out DB sections respect the reserve.

    This is a ContextVar rather than thread-local state because ingestion jobs
    fan out through the context-preserving executor. Every child must inherit
    the admission limit or six speculative readers can exhaust a two-session
    worker pool while the outer job appears correctly bounded.
    """
    token = _WORKER_TASK_ACTIVE.set(True)
    try:
        yield
    finally:
        _WORKER_TASK_ACTIVE.reset(token)


@contextmanager
def read_cursor() -> Iterator[psycopg.Cursor]:
    """Read on the autocommit pool, without extra BEGIN/COMMIT trips.

    Each SELECT gets its own snapshot. Use cursor() for mutations or atomic operations, and
    tenant_cursor() when transaction-local tenant settings are required.
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            yield cur


@contextmanager
def tenant_cursor(tenant_id: str) -> Iterator[psycopg.Cursor]:
    """One atomic, bounded transaction with connection-local tenant isolation.

    SET LOCAL is cleared on commit/rollback, including on pooled connections.
    Never hold this transaction while calling an LLM or reading a document.
    """
    tid = tenant_uuid(str(tenant_id))
    with cursor() as cur:
        cur.execute(
            """SELECT set_config('app.tenant_id', %s, true),
                      set_config('statement_timeout', '15s', true),
                      set_config('lock_timeout', '5s', true),
                      set_config('idle_in_transaction_session_timeout', '30s', true)""",
            (str(tid),),
        )
        yield cur


@atexit.register
def close_pool() -> None:
    if _POOL is not None:
        _POOL.close()


def tenant_uuid(tenant_id: str | uuid.UUID) -> uuid.UUID:
    if isinstance(tenant_id, uuid.UUID):
        return tenant_id
    try:
        return uuid.UUID(str(tenant_id))
    except (ValueError, AttributeError) as e:
        raise ValueError(f"invalid tenant_id: {tenant_id!r}") from e


def verify_runtime_database_role() -> None:
    """Reject privileged or table-owning runtime identities.

    Checks login and effective roles independently so ``SET ROLE`` cannot hide
    a superuser or BYPASSRLS login. Caller owns local/test exemption policy.
    """
    with read_cursor() as cur:
        cur.execute(
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
        rows = cur.fetchall()
    if not rows:
        raise RuntimeError("Database runtime identity could not be inspected")
    unsafe = [name for name, superuser, bypass, owner in rows if superuser or bypass or owner]
    if unsafe:
        names = ", ".join(sorted(set(unsafe)))
        raise RuntimeError(f"Unsafe database runtime role: {names}")


def init_schema() -> None:
    """Compatibility shim — schema is now managed by Alembic.

    Runs `alembic upgrade head` so existing test code that calls
    `init_schema()` keeps working. New code should use Alembic directly.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
