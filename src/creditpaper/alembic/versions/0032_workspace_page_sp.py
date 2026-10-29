"""One-call workspace-page payload."""

from alembic import op

revision = "0032_workspace_page_sp"
down_revision = "0031_perf_indexes"
branch_labels = None
depends_on = None


_WORKSPACE_PAGE_FN = """
CREATE OR REPLACE FUNCTION get_workspace_page(p_tenant_id UUID, p_deal_id TEXT)
RETURNS JSONB LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_workspace JSONB; v_board JSONB; v_factsheet JSONB; v_activity JSONB;
    v_reviews JSONB; v_paper JSONB; v_theme JSONB; v_recommendation JSONB;
BEGIN
    v_workspace := get_workspace_payload(p_tenant_id, p_deal_id);
    IF v_workspace IS NULL THEN RETURN NULL; END IF;
    -- get_slot_board creates missing slots; wrapper must be VOLATILE.
    v_board := get_slot_board(p_tenant_id, p_deal_id);
    v_factsheet := get_fact_sheet_data(p_tenant_id, p_deal_id);

    SELECT jsonb_build_object(
        'deal', jsonb_build_object('id', d.id, 'name', d.name, 'created_at', d.created_at),
        'action_counts', COALESCE((SELECT jsonb_object_agg(action, count) FROM (
            SELECT action, COUNT(*) AS count FROM deal_audit_events
            WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id GROUP BY action
        ) counts), '{}'::jsonb),
        'events', COALESCE((SELECT jsonb_agg(event ORDER BY event->>'created_at' DESC, event->>'id' DESC) FROM (
            SELECT jsonb_build_object('id',e.id,'actor_id',e.actor_id,'action',e.action,
                'entity_type',e.entity_type,'entity_id',e.entity_id,
                'metadata',COALESCE(e.event_metadata,'{}'::jsonb),'created_at',e.created_at) AS event
            FROM deal_audit_events e WHERE e.tenant_id=p_tenant_id AND e.deal_id=p_deal_id
            ORDER BY e.created_at DESC,e.id DESC LIMIT 51
        ) event_rows), '[]'::jsonb),
        'names', COALESCE((SELECT jsonb_object_agg(user_id,name) FROM (
            SELECT u.id::text AS user_id, COALESCE(NULLIF(trim(concat_ws(' ',u.first_name,u.last_name)),''),split_part(u.email,'@',1)) AS name
            FROM users u WHERE u.domain_id=p_tenant_id::text AND u.id::text IN (
                SELECT DISTINCT actor_id::text FROM deal_audit_events
                WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND actor_id IS NOT NULL
            )
        ) users), '{}'::jsonb)
    ) INTO v_activity FROM deals d WHERE d.tenant_id=p_tenant_id AND d.id=p_deal_id;

    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'id',q.id,'field_path',q.field_path,'document_id',q.document_id,'page',q.page,
        'extracted_value',q.extracted_value,'extracted_confidence',q.extracted_confidence,
        'created_at',q.created_at,'assertion_id',q.assertion_id,'ledger_revision',d.ledger_revision
    ) ORDER BY q.created_at DESC),'[]'::jsonb) INTO v_reviews
    FROM fact_review_queue q JOIN deals d ON d.tenant_id=q.tenant_id AND d.id=q.deal_id
    WHERE q.tenant_id=p_tenant_id AND q.deal_id=p_deal_id AND q.status='pending';

    SELECT jsonb_build_object('template',jsonb_build_object(
        'id',COALESCE(d.template_id::text,'standard'),'source',COALESCE(d.template_source,'standard'),
        'snapshot',COALESCE(d.template_snapshot,'{}'::jsonb),'active_job',(
            SELECT jsonb_build_object('job_id',j.id,'status',j.status) FROM processing_jobs j
            WHERE j.tenant_id=p_tenant_id AND j.deal_id=p_deal_id AND j.kind='generation'
              AND j.status IN ('pending','running') ORDER BY j.created_at LIMIT 1
        )), 'rows',COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'section',s.section,'status',s.status,'prose',s.prose,'citations',COALESCE(s.citations,'[]'::jsonb),
            'error',s.error,'editor_value',s.editor_value,'edit_revision',s.edit_revision
        ) ORDER BY s.section) FROM deal_paper_sections s WHERE s.deal_id=p_deal_id),'[]'::jsonb)) INTO v_paper
    FROM deals d WHERE d.tenant_id=p_tenant_id AND d.id=p_deal_id;

    SELECT jsonb_build_object(
        'brand_profile',COALESCE((SELECT bp.profile || jsonb_build_object('updated_at',bp.updated_at)
            FROM brand_profile bp WHERE bp.tenant_id=p_tenant_id),'{}'::jsonb),
        'default_record',(SELECT jsonb_build_object('version',pd.version,'payload',pd.payload)
            FROM platform_defaults pd WHERE pd.kind='paper_theme' AND pd.is_current=true LIMIT 1),
        'firm_name',(SELECT company FROM domains WHERE id::text=p_tenant_id::text)
    ) INTO v_theme;

    SELECT jsonb_build_object('current',CASE WHEN r.id IS NULL THEN NULL ELSE jsonb_build_object(
        'id',r.id,'body',r.body,'author',jsonb_build_object('id',r.author_id,
            'name',COALESCE(NULLIF(trim(concat_ws(' ',u.first_name,u.last_name)),''),split_part(u.email,'@',1),'Former member')),
        'signed_at',r.signed_at,'ledger_revision',r.ledger_revision
    ) END,'history_count',COALESCE(r.history_count,0)) INTO v_recommendation
    FROM (SELECT r.*,COUNT(*) OVER () AS history_count FROM deal_recommendations r
          WHERE r.tenant_id=p_tenant_id AND r.deal_id=p_deal_id ORDER BY r.signed_at DESC,r.id DESC LIMIT 1) r
    LEFT JOIN users u ON u.id::text=r.author_id::text AND u.domain_id=p_tenant_id::text;
    IF v_recommendation IS NULL THEN v_recommendation:=jsonb_build_object('current',NULL,'history_count',0); END IF;

    RETURN jsonb_build_object('workspace',v_workspace,'board',v_board,'fact_sheet',v_factsheet,
        'activity',v_activity,'reviews',v_reviews,'paper',v_paper,'theme',v_theme,'recommendation',v_recommendation);
END;
$$;
"""


def upgrade() -> None:
    op.execute(_WORKSPACE_PAGE_FN)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS get_workspace_page(UUID, TEXT)")
