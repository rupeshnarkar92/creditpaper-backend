"""UserRole model — many-to-many user-role association with tenant scoping."""

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.sql import func

from creditpaper.auth.database import Base


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id = Column(String(36), ForeignKey("users.id"), primary_key=True)
    role_id = Column(String(36), ForeignKey("roles.id"), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), primary_key=True)
    assigned_at = Column(DateTime(timezone=True), server_default=func.now())
    assigned_by = Column(String(36), ForeignKey("users.id"), nullable=True)
