"""Move review-queue and signal persistence behind tenant-scoped functions."""

from alembic import op

revision = "nn_0053_review_signal_functions"
down_revision = "nn_0052_deal_read_functions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.add_fact_review_item(
            p_tenant_id uuid,p_deal_id text,p_field_path text,p_document_id text,
            p_page integer,p_value text,p_confidence double precision
        ) RETURNS uuid LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            INSERT INTO public.fact_review_queue(tenant_id,deal_id,field_path,document_id,page,extracted_value,extracted_confidence)
            VALUES(p_tenant_id,p_deal_id,p_field_path,p_document_id,p_page,p_value,p_confidence)
            RETURNING id
        $$;
        CREATE OR REPLACE FUNCTION public.pending_fact_reviews(p_tenant_id uuid,p_deal_id text)
        RETURNS TABLE(id uuid,field_path text,document_id text,page integer,extracted_value text,
            extracted_confidence double precision,created_at timestamptz,assertion_id uuid,ledger_revision bigint)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT q.id,q.field_path,q.document_id,q.page,q.extracted_value,q.extracted_confidence,
                q.created_at,q.assertion_id,d.ledger_revision
            FROM public.fact_review_queue q JOIN public.deals d ON d.tenant_id=q.tenant_id AND d.id=q.deal_id
            WHERE q.tenant_id=p_tenant_id AND q.deal_id=p_deal_id AND q.status='pending'
              AND p_tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid
            ORDER BY q.created_at DESC
        $$;
        CREATE OR REPLACE FUNCTION public.fact_review_stats(p_tenant_id uuid,p_deal_id text)
        RETURNS TABLE(status text,item_count bigint,avg_confidence double precision)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT q.status,count(*),avg(q.extracted_confidence) FROM public.fact_review_queue q
            WHERE q.tenant_id=p_tenant_id AND q.deal_id=p_deal_id
              AND p_tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid GROUP BY q.status
        $$;
        CREATE OR REPLACE FUNCTION public.append_signal(
            p_id uuid,p_tenant_id uuid,p_deal_id text,p_section integer,p_kind text,p_author text,
            p_comment text,p_anchored text,p_before text,p_after text
        ) RETURNS TABLE(id uuid,deal_id text,section integer,kind text,author text,comment_text text,
            anchored_text text,before_text text,after_text text,created_at timestamptz)
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
        BEGIN
            IF p_tenant_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
                RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE='42501';
            END IF;
            INSERT INTO public.signals(id,tenant_id,deal_id,section,kind,author,comment_text,anchored_text,before_text,after_text)
            VALUES(p_id,p_tenant_id,p_deal_id,p_section,p_kind,p_author,p_comment,p_anchored,p_before,p_after);
            RETURN QUERY SELECT s.id,s.deal_id,s.section,s.kind,s.author,s.comment_text,s.anchored_text,
                s.before_text,s.after_text,s.created_at FROM public.signals s WHERE s.id=p_id;
        END $$;
        CREATE OR REPLACE FUNCTION public.list_signals(p_tenant_id uuid,p_deal_id text,p_section integer)
        RETURNS TABLE(id uuid,deal_id text,section integer,kind text,author text,comment_text text,
            anchored_text text,before_text text,after_text text,created_at timestamptz)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT s.id,s.deal_id,s.section,s.kind,s.author,s.comment_text,s.anchored_text,s.before_text,s.after_text,s.created_at
            FROM public.signals s WHERE s.tenant_id=p_tenant_id AND s.deal_id=p_deal_id
              AND (p_section IS NULL OR s.section=p_section)
              AND p_tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid ORDER BY s.created_at,s.id
        $$;
        CREATE OR REPLACE FUNCTION public.latest_signal_edit(p_tenant_id uuid,p_deal_id text,p_section integer)
        RETURNS text LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT s.after_text FROM public.signals s WHERE s.tenant_id=p_tenant_id AND s.deal_id=p_deal_id
              AND s.section=p_section AND s.kind='edit' AND s.after_text IS NOT NULL
              AND p_tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid ORDER BY s.created_at DESC,s.id DESC LIMIT 1
        $$;
        REVOKE ALL ON FUNCTION public.add_fact_review_item(uuid,text,text,text,integer,text,double precision),public.pending_fact_reviews(uuid,text),public.fact_review_stats(uuid,text),public.append_signal(uuid,uuid,text,integer,text,text,text,text,text,text),public.list_signals(uuid,text,integer),public.latest_signal_edit(uuid,text,integer) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.add_fact_review_item(uuid,text,text,text,integer,text,double precision),public.pending_fact_reviews(uuid,text),public.fact_review_stats(uuid,text),public.append_signal(uuid,uuid,text,integer,text,text,text,text,text,text),public.list_signals(uuid,text,integer),public.latest_signal_edit(uuid,text,integer) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    raise RuntimeError("Do not restore inline review or signal SQL")
