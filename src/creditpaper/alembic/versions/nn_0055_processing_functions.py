"""Move extraction upsert behind a stored function."""

from alembic import op

revision = "nn_0055_processing_functions"
down_revision = "nn_0054_training_functions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.upsert_document_extraction(
            p_tenant_id uuid,p_deal_id text,p_document_id uuid,p_version_id uuid,p_version_number integer,
            p_mime_type text,p_full_text text,p_page_texts jsonb,p_chunks jsonb,p_page_count integer,
            p_sheet_count integer,p_char_count integer,p_classification jsonb)
        RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            INSERT INTO public.deal_document_extractions
                (tenant_id,deal_id,document_id,version_id,version_number,mime_type,full_text,page_texts,chunks,
                 page_count,sheet_count,char_count,classification,processing_status,processed_at)
            VALUES (p_tenant_id,p_deal_id,p_document_id,p_version_id,p_version_number,p_mime_type,p_full_text,
                    p_page_texts,p_chunks,p_page_count,p_sheet_count,p_char_count,p_classification,'ready',now())
            ON CONFLICT (version_id) DO UPDATE SET full_text=EXCLUDED.full_text,page_texts=EXCLUDED.page_texts,
                chunks=EXCLUDED.chunks,page_count=EXCLUDED.page_count,sheet_count=EXCLUDED.sheet_count,
                char_count=EXCLUDED.char_count,classification=EXCLUDED.classification,processed_at=EXCLUDED.processed_at
        $$;
        REVOKE ALL ON FUNCTION public.upsert_document_extraction(uuid,text,uuid,uuid,integer,text,text,jsonb,jsonb,integer,integer,integer,jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.upsert_document_extraction(uuid,text,uuid,uuid,integer,text,text,jsonb,jsonb,integer,integer,integer,jsonb) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.upsert_document_extraction(uuid,text,uuid,uuid,integer,text,text,jsonb,jsonb,integer,integer,integer,jsonb)"
    )
