"""Add unique constraint on onboarding_documents(tenant_id, filename).

The upsert_document() function uses ON CONFLICT (tenant_id, filename) but the
table only had a PK on id, causing psycopg.errors.InvalidColumnReference on
every paper upload.
"""

from alembic import op

revision = "0027_doc_unique_filename"
down_revision = "0026_resolution_guard_no_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE onboarding_documents
        ADD CONSTRAINT uq_onb_doc_tenant_filename
        UNIQUE (tenant_id, filename)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE onboarding_documents
        DROP CONSTRAINT uq_onb_doc_tenant_filename
        """
    )
