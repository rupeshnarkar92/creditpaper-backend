from __future__ import annotations

import logging
import re

from fastapi import Depends, HTTPException

from creditpaper.auth.dependencies import CurrentUser, get_current_user

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

logger = logging.getLogger(__name__)


def _is_valid_tenant_id(value: str) -> bool:
    if _UUID_RE.match(value):
        return True
    if re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$", value):
        return True
    return False


def resolve_tenant_id(
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> str:
    """Extract tenant_id from the Supabase-authenticated user's membership."""
    if not user.tenant_id:
        raise HTTPException(status_code=403, detail="Organisation membership required")
    return str(user.tenant_id)


def require_tenant_id(tenant_id: str) -> str:
    if not tenant_id or not _is_valid_tenant_id(tenant_id):
        raise HTTPException(status_code=401, detail="missing or invalid tenant")  # noqa: B904
    try:
        import uuid

        uuid.UUID(tenant_id)
    except ValueError:
        pass
    return tenant_id
