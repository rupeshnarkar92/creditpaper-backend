"""Global exception handlers and error envelope."""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


def add_exception_handlers(app: FastAPI) -> None:
    from creditpaper.ledger.types import BadInput, Conflict, Hold, LedgerError, NotFound

    @app.exception_handler(LedgerError)
    async def ledger_exception_handler(request: Request, exc: LedgerError):
        status = (
            422
            if isinstance(exc, BadInput)
            else 404
            if isinstance(exc, NotFound)
            else 409
            if isinstance(exc, (Conflict, Hold))
            else 400
        )
        # A subclass may name its own code (RecommendationRequired -> RECOMMENDATION_REQUIRED).
        code = getattr(exc, "code", None) or ("EXTRACTION_HOLD" if isinstance(exc, Hold) else "LEDGER_ERROR")
        error = {"code": code, "message": str(exc)}
        for attribute in ("blocking_items", "outstanding_items", "open_contradictions"):
            items = getattr(exc, attribute, None)
            if items is not None:
                error[attribute] = [
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in items
                ]
        return JSONResponse(status_code=status, content={"error": error})

    import psycopg
    from psycopg_pool import PoolTimeout

    @app.exception_handler(PoolTimeout)
    async def pool_timeout_handler(request: Request, exc: PoolTimeout):
        # Connection checkout starved: tell the client to retry shortly instead of hanging.
        log.warning("DB pool checkout timed out: method=%s path=%s", request.method, request.url.path)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "2"},
            content={
                "error": {
                    "code": "DATABASE_BUSY",
                    "message": "Database is busy; retry shortly",
                }
            },
        )

    @app.exception_handler(psycopg.Error)
    async def database_exception_handler(request: Request, exc: psycopg.Error):
        # Do not log exception text: PostgreSQL details can contain tenant data.
        log.error(
            "Database request failed: method=%s path=%s error=%s sqlstate=%s",
            request.method,
            request.url.path,
            type(exc).__name__,
            exc.sqlstate,
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "Database operation could not complete; retry safely",
                }
            },
        )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "HTTP_ERROR",
                    "message": str(exc.detail),
                    "request_id": request.headers.get("x-request-id", ""),
                }
            },
        )
