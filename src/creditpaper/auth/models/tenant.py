"""Tenant model — multi-tenant organization.

Mirrors the live `tenants` table exactly (uuid id, name, email_domain, status,
created_at). The previous copy carried slug/domain/settings/updated_at columns
the table never had; autogenerating against it is what produced the broken
0024 and the 0037 provisioning procedure that 0040 had to repair.
"""

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from creditpaper.auth.database import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(UUID(as_uuid=False), primary_key=True)
    name = Column(Text, nullable=False)
    email_domain = Column(Text)
    status = Column(String, server_default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
