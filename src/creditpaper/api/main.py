"""FastAPI shell — composition only.

This file wires routers, middleware, exception handlers, and lifespan.
No business logic lives here. Each domain owns its own routes/services/store.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from creditpaper import __version__
from creditpaper.api.errors import add_exception_handlers
from creditpaper.api.rate_limit import RateLimitMiddleware
from creditpaper.api.routes.invite import router as invite_router
from creditpaper.api.routes.tenant_members import router as tenant_members_router
from creditpaper.api.security import SecurityMiddleware
from creditpaper.auth.config import get_settings
from creditpaper.auth.routes.superadmin import router as superadmin_router
from creditpaper.auth.routes.superadmin_credit_ops import router as superadmin_credit_ops_router
from creditpaper.auth.routes_supabase import router as supabase_auth_router
from creditpaper.billing.routes import router as billing_router
from creditpaper.deals.routes.activity import tenant_activity_router
from creditpaper.deals.routes.deals_router import editor_router
from creditpaper.deals.routes.deals_router import router as deals_router
from creditpaper.deals.routes.final_paper import router as final_paper_router
from creditpaper.deals.routes.paper import deal_paper_router, paper_router, template_options_router
from creditpaper.deals.routes.paper_workflow import router as paper_workflow_router
from creditpaper.deals.routes.workspace import router as deals_workspace_router
from creditpaper.guardrails.routes.guardrails import router as guardrails_router
from creditpaper.integrations.routes import router as integrations_router
from creditpaper.ledger.routes import router as ledger_router
from creditpaper.llm.byok_routes import router as llm_provider_router
from creditpaper.lookups.routes.lookups import router as lookups_router
from creditpaper.onboarding.routes.brand import router as brand_router
from creditpaper.onboarding.routes.house_style import router as house_style_router
from creditpaper.onboarding.routes.papers import router as papers_router
from creditpaper.onboarding.routes.preview import router as preview_router
from creditpaper.onboarding.routes.preview_doc import router as preview_doc_router
from creditpaper.onboarding.routes.state import router as state_router
from creditpaper.onboarding.routes.voice import router as voice_router
from creditpaper.processing.routes import router as processing_router
from creditpaper.templates.routes.templates import router as templates_router


def _local_or_test() -> bool:
    """True when this process is running locally or under the test suite.

    The environment variable is consulted first so the value is never stale:
    `get_settings()` is cached at first use, so a process whose `APP_ENV`
    changed after startup would otherwise keep deciding on the old answer.
    """
    return (os.getenv("APP_ENV") or get_settings().APP_ENV) in {"local", "test"}


def _rate_limiting_enabled() -> bool:
    """Rate limiting is off in local/test so test suites are not throttled."""
    return not _local_or_test()


def _api_docs_enabled() -> bool:
    """SEC-12: the interactive docs and the OpenAPI schema are development aids.

    In any deployed environment they hand an unauthenticated caller a complete
    map of every route, dependency and model — free reconnaissance. They stay
    on locally so the browser tooling still works.
    """
    return _local_or_test()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is managed by Alembic — run `alembic upgrade head` before starting.
    # Runtime fallbacks (template, paper theme, voice) are stored records; a missing
    # current row is a deployment fault and fails startup loudly (spec 2026-09-15 §5).
    from creditpaper.paper.defaults_store import verify_platform_defaults
    from creditpaper.seed.seed_rbac import seed_rbac

    if not _local_or_test():
        from creditpaper.auth.database import verify_async_runtime_database_role
        from creditpaper.db import verify_runtime_database_role

        verify_runtime_database_role()
        await verify_async_runtime_database_role()
    seed_rbac()  # idempotent — permissions/roles required by require_permission
    verify_platform_defaults()
    yield


_DOCS_ENABLED = _api_docs_enabled()

app = FastAPI(
    title="creditpaper engine",
    version=__version__,
    lifespan=lifespan,
    # SEC-12: None removes the route entirely, so /docs and /openapi.json 404
    # outside local/test rather than being merely hidden in the UI.
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+" if _local_or_test() else None,
    allow_origins=[get_settings().FRONTEND_URL.rstrip("/")],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

app.add_middleware(SecurityMiddleware)
# Throttling runs before any handler work, so a flood never reaches auth,
# the database, or the LLM client. Starlette applies middleware in reverse
# registration order, so this is the outermost layer.
app.add_middleware(RateLimitMiddleware, enabled=_rate_limiting_enabled())

add_exception_handlers(app)

# Auth domain — Supabase + invite flow
app.include_router(supabase_auth_router)
app.include_router(invite_router)
app.include_router(superadmin_router, prefix="/api")
app.include_router(superadmin_credit_ops_router, prefix="/api")
app.include_router(billing_router, prefix="/api")
app.include_router(llm_provider_router, prefix="/api")
app.include_router(integrations_router, prefix="/api")
app.include_router(tenant_members_router)

# Onboarding domain
app.include_router(brand_router)
app.include_router(house_style_router)
app.include_router(papers_router)
app.include_router(voice_router)
app.include_router(preview_router)
app.include_router(preview_doc_router)
app.include_router(state_router)

# Deals domain (CRUD under /api/deals, workspace). The template-options routes go
# first: `GET /api/deals/{deal_id}` would otherwise capture "template-options".
app.include_router(template_options_router)
app.include_router(deals_router)
app.include_router(editor_router)
app.include_router(deal_paper_router)
app.include_router(paper_workflow_router)
app.include_router(final_paper_router)
app.include_router(paper_router)
app.include_router(deals_workspace_router)
app.include_router(processing_router)
app.include_router(ledger_router)
app.include_router(tenant_activity_router)

# Templates domain
app.include_router(templates_router, prefix="/api")

# Guardrails domain
app.include_router(guardrails_router, prefix="/api")

# Lookups domain (platform-level reference data)
app.include_router(lookups_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/readyz")
def readiness() -> JSONResponse:
    """Return dependency readiness for supervisors and load balancers.

    `/health` intentionally remains a cheap liveness check. This endpoint
    performs bounded probes for the database and optional OCR runtime so a
    deployment can stop routing traffic when a dependency is unavailable.
    """
    checks: dict[str, str] = {}
    try:
        from creditpaper.db import read_cursor

        with read_cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - readiness must never raise a 500
        checks["database"] = type(exc).__name__

    # Resolve the same way ingestion does. On Windows the installer commonly
    # places Tesseract outside PATH, while processing/ocr.py knows its standard
    # install locations. A readiness check must not disagree with the worker.
    from creditpaper.processing import ocr_rapid
    from creditpaper.processing.ocr import ocr_available, tesseract_path

    tesseract = tesseract_path()
    if not tesseract:
        checks["tesseract"] = "unavailable"
    else:
        try:
            completed = subprocess.run(
                [tesseract, "--version"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            checks["tesseract"] = "ok" if completed.returncode == 0 else "unavailable"
        except (OSError, subprocess.TimeoutExpired):
            checks["tesseract"] = "unavailable"

    checks["rapidocr"] = "ok" if ocr_rapid.available() else "unavailable"
    checks["ocr"] = "ok" if ocr_available() else "unavailable"
    if os.getenv("PROCESSING_QUEUE_BACKEND", "database").lower() == "celery":
        try:
            from redis import Redis

            from creditpaper.queueing.config import broker_url
            from creditpaper.queueing.health import workers_alive

            Redis.from_url(broker_url()).ping()
            checks["redis"] = "ok"
            checks["celery_workers"] = "ok" if workers_alive() else "unavailable"
        except Exception:
            checks["redis"] = "unavailable"
            checks["celery_workers"] = "unavailable"
    # OCR is a dedicated, failure-isolated lane. Missing OCR must not take the
    # API or native-text ingestion out of service; OCR-bound documents remain
    # waiting_ocr and the worker health UI reports that degraded capability.
    ready = checks["database"] == "ok" and checks.get("redis", "ok") == "ok"
    degraded = ready and (checks["ocr"] != "ok" or checks.get("celery_workers", "ok") != "ok")
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "degraded" if degraded else ("ready" if ready else "not_ready"), "checks": checks},
        headers={} if ready else {"Retry-After": "2"},
    )
