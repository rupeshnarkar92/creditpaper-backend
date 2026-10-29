"""Persist structured causes for facts sent to analyst review.

Revision ID: US_ingestion_review_reasons
Revises: US_ingestion_status_snapshot
"""

from alembic import op

revision = "US_ingestion_review_reasons"
down_revision = "US_ingestion_status_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE public.fact_review_queue "
        "ADD COLUMN review_reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute(
        "DROP FUNCTION public.enqueue_fact_review(uuid,text,text,uuid,integer,jsonb,double precision,uuid)"
    )
    op.execute(
        """
        CREATE FUNCTION public.enqueue_fact_review(
          p_tenant_id uuid, p_deal_id text, p_field_path text, p_document_id uuid,
          p_page integer, p_extracted_value jsonb, p_extracted_confidence double precision,
          p_assertion_id uuid, p_review_reason_codes jsonb
        ) RETURNS boolean
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
        DECLARE affected integer;
        BEGIN
          IF jsonb_typeof(p_review_reason_codes) <> 'array' THEN
            RAISE EXCEPTION 'review reason codes must be a JSON array';
          END IF;
          INSERT INTO public.fact_review_queue
            (tenant_id,deal_id,field_path,document_id,page,extracted_value,
             extracted_confidence,assertion_id,review_reason_codes)
          VALUES
            (p_tenant_id,p_deal_id,p_field_path,p_document_id,p_page,p_extracted_value,
             p_extracted_confidence,p_assertion_id,p_review_reason_codes)
          ON CONFLICT(tenant_id,assertion_id) DO UPDATE
          SET review_reason_codes=EXCLUDED.review_reason_codes;
          GET DIAGNOSTICS affected = ROW_COUNT;
          RETURN affected > 0;
        END $$;
        COMMENT ON FUNCTION public.enqueue_fact_review(uuid,text,text,uuid,integer,jsonb,double precision,uuid,jsonb)
          IS 'Queues an assertion for review with structured hold causes; returns true when inserted or refreshed.';
        REVOKE ALL ON FUNCTION public.enqueue_fact_review(uuid,text,text,uuid,integer,jsonb,double precision,uuid,jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.enqueue_fact_review(uuid,text,text,uuid,integer,jsonb,double precision,uuid,jsonb) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION public.enqueue_fact_review(uuid,text,text,uuid,integer,jsonb,double precision,uuid,jsonb)"
    )
    op.execute(
        """
        CREATE FUNCTION public.enqueue_fact_review(
          p_tenant_id uuid, p_deal_id text, p_field_path text, p_document_id uuid,
          p_page integer, p_extracted_value jsonb, p_extracted_confidence double precision,
          p_assertion_id uuid
        ) RETURNS boolean
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
        DECLARE affected integer;
        BEGIN
          INSERT INTO public.fact_review_queue
            (tenant_id,deal_id,field_path,document_id,page,extracted_value,extracted_confidence,assertion_id)
          VALUES
            (p_tenant_id,p_deal_id,p_field_path,p_document_id,p_page,p_extracted_value,p_extracted_confidence,p_assertion_id)
          ON CONFLICT(tenant_id,assertion_id) DO NOTHING;
          GET DIAGNOSTICS affected = ROW_COUNT;
          RETURN affected > 0;
        END $$;
        REVOKE ALL ON FUNCTION public.enqueue_fact_review(uuid,text,text,uuid,integer,jsonb,double precision,uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.enqueue_fact_review(uuid,text,text,uuid,integer,jsonb,double precision,uuid) TO creditpaper_runtime;
        """
    )
    op.execute("ALTER TABLE public.fact_review_queue DROP COLUMN review_reason_codes")
