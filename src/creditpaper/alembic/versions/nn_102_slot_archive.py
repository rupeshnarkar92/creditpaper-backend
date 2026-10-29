"""Move document archival updates behind a stored function."""

from alembic import op

revision = "nn_102_slot_archive"
down_revision = "nn_101_slot_reset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.archive_slot_document(p_tenant_id uuid,p_deal_id text,p_document_id uuid,p_archived_at timestamptz) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ WITH u AS (UPDATE public.deal_documents SET archived_at=p_archived_at WHERE id=p_document_id AND tenant_id=p_tenant_id AND deal_id=p_deal_id AND archived_at IS NULL RETURNING 1) SELECT EXISTS(SELECT 1 FROM u) $$; REVOKE ALL ON FUNCTION public.archive_slot_document(uuid,text,uuid,timestamptz) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.archive_slot_document(uuid,text,uuid,timestamptz) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.archive_slot_document(uuid,text,uuid,timestamptz)")
