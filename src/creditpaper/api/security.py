"""Browser request protections; no request bodies or secrets are logged."""

from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from creditpaper.auth.config import get_settings


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        settings = get_settings()
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            request.cookies.get("access_token") or request.cookies.get("creditpaper_session")
        ):
            if not request.headers.get("authorization", "").lower().startswith("bearer "):
                origin = request.headers.get("origin", "")
                frontend = settings.FRONTEND_URL.rstrip("/")
                parts = urlsplit(origin)
                local = (
                    settings.APP_ENV in {"local", "test"}
                    and parts.scheme == "http"
                    and parts.hostname in {"localhost", "127.0.0.1"}
                )
                if origin != frontend and not local:
                    return JSONResponse(
                        status_code=403,
                        content={"error": {"code": "CSRF_ORIGIN", "message": "Untrusted request origin"}},
                    )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            # A route that opts into private caching (the firm logo) keeps its own header.
            response.headers["Cache-Control"] = "no-store"
        if settings.APP_ENV not in {"local", "test"}:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
