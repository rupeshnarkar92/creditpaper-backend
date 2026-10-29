"""Auth audit events table — structured audit trail for all auth actions.

Matches the deal_audit_events pattern: structured slugs + JSONB metadata.
"""

from sqlalchemy import JSON, Column, DateTime, String
from sqlalchemy.sql import func

from creditpaper.auth.database import Base


class AuthAuditEvent(Base):
    """Append-only audit log for authentication and tenant actions."""

    __tablename__ = "auth_audit_events"

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), nullable=True)
    user_id = Column(String(36), nullable=True)
    action = Column(String(100), nullable=False)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(String(36), nullable=True)
    event_metadata = Column(JSON, default={})
    correlation_id = Column(String(36), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
