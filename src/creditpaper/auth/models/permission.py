"""Permission model — RBAC permissions."""

from sqlalchemy import Column, String, Text

from creditpaper.auth.database import Base


class Permission(Base):
    __tablename__ = "permissions"

    id = Column(String(36), primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    resource = Column(String(50), nullable=False)
    action = Column(String(50), nullable=False)
    description = Column(Text)
