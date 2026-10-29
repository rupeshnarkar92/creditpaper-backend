"""Role model — RBAC roles."""

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text
from sqlalchemy.sql import func

from creditpaper.auth.database import Base


class Role(Base):
    __tablename__ = "roles"

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=True)
    name = Column(String(50), nullable=False)
    display_name = Column(String(100), nullable=False)
    description = Column(Text)
    is_system = Column(Boolean, server_default="false")
    is_default = Column(Boolean, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
