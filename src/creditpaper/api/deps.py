"""Shared FastAPI dependencies.

Cross-domain dependencies (tenant_id, current_user, etc.) live here.
Domain-specific dependencies live in their own domain folder.
"""

from __future__ import annotations

# Re-export the tenant resolver so the API composition file imports from one place.
from creditpaper.api.tenant import resolve_tenant_id

__all__ = ["resolve_tenant_id"]
