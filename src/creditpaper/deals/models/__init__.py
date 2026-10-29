from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from creditpaper.auth.database import Base


class DealDocumentVersion(Base):
    __tablename__ = "deal_document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_deal_document_versions_doc_ver"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    deal_id: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("deal_documents.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(36), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC), nullable=False)
    processing_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    extraction_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    superseded_at: Mapped[datetime | None] = mapped_column(default=None)
    processing_error: Mapped[str | None] = mapped_column(Text, default=None)
    extraction_error: Mapped[str | None] = mapped_column(Text, default=None)
