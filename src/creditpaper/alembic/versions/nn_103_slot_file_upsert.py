"""Move slot-file upserts behind a stored function."""

from alembic import op

revision = "nn_103_slot_file_upsert"
down_revision = "nn_102_slot_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.upsert_document_slot_file(p_tenant_id uuid,p_deal_id text,p_slot_id uuid,p_document_id uuid,p_version_id uuid,p_role text,p_confidence numeric,p_reason text) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.document_slot_files(tenant_id,slot_id,document_id,version_id,role,confidence,match_reason) SELECT p_tenant_id,s.id,d.id,p_version_id,p_role,p_confidence,p_reason FROM public.document_slots s JOIN public.deal_documents d ON d.deal_id=s.deal_id AND d.tenant_id=s.tenant_id WHERE s.id=p_slot_id AND s.tenant_id=p_tenant_id AND s.deal_id=p_deal_id AND d.id=p_document_id ON CONFLICT(slot_id,document_id,role) DO UPDATE SET confidence=EXCLUDED.confidence,match_reason=EXCLUDED.match_reason,version_id=EXCLUDED.version_id RETURNING true $$; REVOKE ALL ON FUNCTION public.upsert_document_slot_file(uuid,text,uuid,uuid,uuid,text,numeric,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.upsert_document_slot_file(uuid,text,uuid,uuid,uuid,text,numeric,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.upsert_document_slot_file(uuid,text,uuid,uuid,uuid,text,numeric,text)"
    )
