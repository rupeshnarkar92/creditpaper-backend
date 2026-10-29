"""Inbound rate limiting for unauthenticated and expensive endpoints.

Why this exists: the sign-up surface (`check-email`, `validate-domain`) and the
audit routes accept anonymous traffic, and SCALE-03 flagged that the public API
had no throttling at all. Without a limit those endpoints are usable both for
enumeration and for exhausting the connection pool.

Design notes:
- Fixed-window counters kept in process memory. No new dependency, no Redis, no
  database round trip on the hot path. This is deliberately *per process*:
  running N API processes multiplies the effective limit by N. Moving to a
  shared limiter is a deployment decision and is tracked as SCALE-03, not
  something to smuggle in here.
- The window is keyed by client IP *and* route class, so a burst against one
  endpoint cannot consume another endpoint's budget.
- Only unauthenticated and explicitly-listed routes are limited. Authenticated
  tenant traffic is governed by per-tenant integration rate limits that already
  exist in `integrations/store.py`; applying an IP bucket on top would punish
  offices and CI behind one NAT.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Routes reachable without a session, keyed to (limit, window_seconds).
# Keep this list explicit and small: every entry is a deliberate decision to
# throttle an anonymous endpoint, and reviewers should see it in one place.
#
# SEC-09: `check-email` and `validate-domain` answer "does this person exist?",
# so they get the tightest budget here — 10/minute from one IP is well above a
# human typing and well below a script walking an address list.
# `audit/login-failed` is unauthenticated by necessity and writes a durable row
# per call, so it is capped to keep the audit log from being flooded.
# `/api/auth/password-history/check` is gone: SEC-04 made it authenticated, so
# the anonymous branch can never reach it.
ANONYMOUS_ROUTE_LIMITS: dict[str, tuple[int, int]] = {
    "/api/auth/check-email": (10, 60),
    "/api/auth/validate-domain": (10, 60),
    "/api/auth/audit/login-failed": (20, 60),
    "/api/auth/signup": (10, 300),
}

# A general ceiling for anything else that reaches the API anonymously.
DEFAULT_ANONYMOUS_LIMIT = (120, 60)


class FixedWindowLimiter:
    """Thread-safe fixed-window counter. One process, one budget per key."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Record a hit and report whether it is within budget.

        Returns (allowed, retry_after_seconds). retry_after is 0 when allowed.
        """
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, int(hits[0] + window_seconds - now) + 1)
                return False, retry_after
            hits.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_limiter = FixedWindowLimiter()


def _client_key(request: Request) -> str:
    """Identify the caller. Prefers the proxy header when a proxy sets it."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_anonymous(request: Request) -> bool:
    """No bearer token and no session cookie means unauthenticated traffic."""
    if request.headers.get("authorization"):
        return False
    return not (request.cookies.get("creditpaper_session") or request.cookies.get("access_token"))


def limit_for(path: str) -> tuple[int, int]:
    return ANONYMOUS_ROUTE_LIMITS.get(path, DEFAULT_ANONYMOUS_LIMIT)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Throttle anonymous requests, returning 429 with Retry-After."""

    def __init__(self, app, enabled: bool = True) -> None:  # noqa: ANN001
        super().__init__(app)
        self.enabled = enabled
        self.limiter = _limiter

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        if not self.enabled or request.method == "OPTIONS":
            return await call_next(request)
        if not _is_anonymous(request):
            return await call_next(request)

        path = request.url.path
        limit, window = limit_for(path)
        allowed, retry_after = self.limiter.allow(f"{path}|{_client_key(request)}", limit, window)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": "Too many requests. Try again shortly.",
                    }
                },
                headers={
                    "Retry-After": str(retry_after),
                    # Do not let a shared cache keep a throttled response.
                    "Cache-Control": "no-store",
                },
            )
        return await call_next(request)
