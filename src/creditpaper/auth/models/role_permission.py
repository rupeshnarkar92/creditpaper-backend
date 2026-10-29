"""RolePermission model — many-to-many role-permission association."""

from sqlalchemy import Column, ForeignKey, String

from creditpaper.auth.database import Base


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id = Column(String(36), ForeignKey("roles.id"), primary_key=True)
    permission_id = Column(String(36), ForeignKey("permissions.id"), primary_key=True)
