"""Move extraction training persistence and reads behind stored functions."""

from alembic import op

revision = "nn_0054_training_functions"
down_revision = "nn_0053_review_signal_functions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.save_training_example(
            p_tenant_id uuid,p_deal_id text,p_document_id text,p_slot_key text,p_model_used text,
            p_instruction_hash text,p_input_pdf_hash text,p_expected jsonb,p_actual jsonb,p_flags jsonb,
            p_quality double precision,p_notes text)
        RETURNS uuid LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            INSERT INTO public.extraction_training_examples
                (tenant_id,deal_id,document_id,slot_key,model_used,instruction_hash,input_pdf_hash,
                 expected_output,actual_output,flags,quality_score,notes)
            VALUES (p_tenant_id,p_deal_id,p_document_id,p_slot_key,p_model_used,p_instruction_hash,
                    p_input_pdf_hash,p_expected,p_actual,p_flags,p_quality,p_notes)
            RETURNING id
        $$;
        CREATE OR REPLACE FUNCTION public.save_prompt_version(
            p_slot_key text,p_version text,p_instruction text,p_instruction_hash text,p_reason text)
        RETURNS uuid LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            INSERT INTO public.extraction_prompt_versions(slot_key,version,instruction_text,instruction_hash,change_reason)
            VALUES (p_slot_key,p_version,p_instruction,p_instruction_hash,p_reason)
            ON CONFLICT (slot_key,version) DO UPDATE SET instruction_text=EXCLUDED.instruction_text,
                instruction_hash=EXCLUDED.instruction_hash,change_reason=EXCLUDED.change_reason,created_at=now()
            RETURNING id
        $$;
        CREATE OR REPLACE FUNCTION public.get_training_examples(p_slot_key text,p_min_quality double precision,p_limit integer)
        RETURNS SETOF public.extraction_training_examples LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT e.* FROM public.extraction_training_examples e
            WHERE (p_slot_key IS NULL OR e.slot_key=p_slot_key)
              AND (p_min_quality IS NULL OR e.quality_score >= p_min_quality)
            ORDER BY e.created_at DESC LIMIT p_limit
        $$;
        CREATE OR REPLACE FUNCTION public.get_extraction_stats()
        RETURNS TABLE(slot_key text,total bigint,avg_quality double precision,avg_flags double precision,first_example timestamptz,last_example timestamptz)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT e.slot_key,count(*),avg(e.quality_score),avg(jsonb_array_length(e.flags)),min(e.created_at),max(e.created_at)
            FROM public.extraction_training_examples e GROUP BY e.slot_key
        $$;
        CREATE OR REPLACE FUNCTION public.get_prompt_versions(p_slot_key text)
        RETURNS SETOF public.extraction_prompt_versions LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT p.* FROM public.extraction_prompt_versions p WHERE p.slot_key=p_slot_key ORDER BY p.created_at DESC
        $$;
        REVOKE ALL ON FUNCTION public.save_training_example(uuid,text,text,text,text,text,text,jsonb,jsonb,jsonb,double precision,text),public.save_prompt_version(text,text,text,text,text),public.get_training_examples(text,double precision,integer),public.get_extraction_stats(),public.get_prompt_versions(text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.save_training_example(uuid,text,text,text,text,text,text,jsonb,jsonb,jsonb,double precision,text),public.save_prompt_version(text,text,text,text,text),public.get_training_examples(text,double precision,integer),public.get_extraction_stats(),public.get_prompt_versions(text) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.save_training_example(uuid,text,text,text,text,text,text,jsonb,jsonb,jsonb,double precision,text), public.save_prompt_version(text,text,text,text,text), public.get_training_examples(text,double precision,integer), public.get_extraction_stats(), public.get_prompt_versions(text)"
    )
