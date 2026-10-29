"""Remove the unused whole-document chunks GIN write amplifier.

Revision ID: US_extraction_remove_chunks_gin
Revises: US_extraction_start_fast_path
"""

from alembic import op

revision = "US_extraction_remove_chunks_gin"
down_revision = "US_extraction_start_fast_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Retrieval uses ledger_chunks.search. No runtime query uses containment on
    # deal_document_extractions.chunks, while maintaining this index rewrites a
    # large JSON document during every extraction finalization.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS public.ix_deal_document_extractions_chunks")


def downgrade() -> None:
    op.execute(
        """CREATE INDEX IF NOT EXISTS ix_deal_document_extractions_chunks
        ON public.deal_document_extractions USING gin (chunks)"""
    )
