"""Move onboarding document upsert and cleanup behind stored functions."""

from alembic import op

revision = "nn_069_doc_writes"
down_revision = "nn_068_onboarding_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.upsert_onboarding_document(p_id uuid,p_tenant_id uuid,p_filename text,p_status text,p_credit boolean,p_approval boolean,p_confidence double precision,p_reason text,p_size bigint,p_content_type text,p_tags jsonb,p_storage_path text,p_sections jsonb,p_style jsonb)
        RETURNS SETOF public.onboarding_documents LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            INSERT INTO public.onboarding_documents(id,tenant_id,filename,status,is_credit_paper,approval_detected,confidence,reason,size,content_type,tags,storage_path,extracted_sections,extracted_style,updated_at)
            VALUES(p_id,p_tenant_id,p_filename,p_status,p_credit,p_approval,p_confidence,p_reason,p_size,p_content_type,p_tags,p_storage_path,p_sections,p_style,now())
            ON CONFLICT(tenant_id,filename) DO UPDATE SET status=EXCLUDED.status,is_credit_paper=EXCLUDED.is_credit_paper,approval_detected=EXCLUDED.approval_detected,confidence=EXCLUDED.confidence,reason=EXCLUDED.reason,size=EXCLUDED.size,content_type=EXCLUDED.content_type,tags=EXCLUDED.tags,storage_path=EXCLUDED.storage_path,extracted_sections=EXCLUDED.extracted_sections,extracted_style=EXCLUDED.extracted_style,updated_at=now()
            RETURNING *
        $$;
        CREATE OR REPLACE FUNCTION public.delete_all_onboarding_documents(p_tenant_id uuid) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ DELETE FROM public.onboarding_documents WHERE tenant_id=p_tenant_id $$;
        REVOKE ALL ON FUNCTION public.upsert_onboarding_document(uuid,uuid,text,text,boolean,boolean,double precision,text,bigint,text,jsonb,text,jsonb,jsonb),public.delete_all_onboarding_documents(uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.upsert_onboarding_document(uuid,uuid,text,text,boolean,boolean,double precision,text,bigint,text,jsonb,text,jsonb,jsonb),public.delete_all_onboarding_documents(uuid) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.upsert_onboarding_document(uuid,uuid,text,text,boolean,boolean,double precision,text,bigint,text,jsonb,text,jsonb,jsonb),public.delete_all_onboarding_documents(uuid)"
    )
