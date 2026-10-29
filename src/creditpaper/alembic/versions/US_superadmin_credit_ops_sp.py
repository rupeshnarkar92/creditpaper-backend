"""Superadmin credit-operations read procedures and their indexes."""

from alembic import op

revision = "US_superadmin_credit_ops_sp"
down_revision = "US_superadmin_app_context"
branch_labels = None
depends_on = None

# Unqualified names on purpose: the stored-reads fixture executes this text with
# `SET search_path = <temp schema>` (single string replace), and production
# alembic resolves against `public`. Keep every SQL line <= 110 chars (ruff E501).
_FUNCTIONS = """
CREATE OR REPLACE FUNCTION get_sa_overview()
RETURNS JSONB
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
  SELECT jsonb_build_object(
    'deals_by_state', COALESCE((
      SELECT jsonb_object_agg(t.current_state, t.cnt) FROM (
        SELECT current_state, count(*)::int AS cnt FROM deals
        WHERE deleted_at IS NULL GROUP BY current_state
      ) t), '{}'::jsonb),
    'awaiting_checker', (
      SELECT count(*)::int FROM deals
      WHERE deleted_at IS NULL AND current_state = 'PENDING_APPROVAL'),
    'pending_fact_review', (
      SELECT count(*)::int FROM fact_review_queue q WHERE q.status = 'pending'
      AND EXISTS (SELECT 1 FROM deals d WHERE d.id = q.deal_id
                  AND d.tenant_id = q.tenant_id AND d.deleted_at IS NULL)),
    'open_contradictions', (
      SELECT count(*)::int FROM contradiction_register c WHERE c.status = 'open'
      AND EXISTS (SELECT 1 FROM deals d WHERE d.id = c.deal_id
                  AND d.tenant_id = c.tenant_id AND d.deleted_at IS NULL)),
    'extraction_failures', (
      SELECT count(*)::int FROM deal_document_versions v
      WHERE v.extraction_status IN ('failed', 'invalid')
      AND EXISTS (SELECT 1 FROM deals d WHERE d.id = v.deal_id
                  AND d.tenant_id = v.tenant_id AND d.deleted_at IS NULL)),
    'guardrail_flagged', (
      SELECT count(*)::int FROM guardrail_evaluations g WHERE g.result = 'flagged'),
    'guardrail_errors', (
      SELECT count(*)::int FROM guardrail_evaluations g WHERE g.result = 'error')
  );
$$;

CREATE OR REPLACE FUNCTION get_sa_deal_ref(p_deal_id TEXT)
RETURNS JSONB
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
  SELECT jsonb_build_object(
    'tenant_id', d.tenant_id, 'current_state', d.current_state, 'name', d.name)
  FROM deals d
  WHERE d.id = p_deal_id AND d.deleted_at IS NULL;
$$;

CREATE OR REPLACE FUNCTION get_sa_template_usage()
RETURNS JSONB
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'template_id', u.template_id, 'tenant_id', u.tenant_id,
    'deal_count', u.deal_count)), '[]'::jsonb)
  FROM (
    SELECT template_id, tenant_id, count(*)::int AS deal_count
    FROM deals
    WHERE deleted_at IS NULL AND template_id IS NOT NULL
    GROUP BY template_id, tenant_id
  ) u;
$$;

CREATE OR REPLACE FUNCTION get_sa_deals_list(
    p_search TEXT, p_state TEXT, p_tenant_id UUID, p_deal_type TEXT,
    p_date_from DATE, p_date_to DATE, p_order_by TEXT, p_page INT, p_limit INT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_page INT := greatest(coalesce(p_page, 1), 1);
  v_limit INT := least(greatest(coalesce(p_limit, 20), 1), 100);
  v_offset INT := (v_page - 1) * v_limit;
  v_token TEXT := coalesce(p_order_by, '');
  v_desc BOOLEAN := v_token LIKE '-%';
  v_col TEXT := CASE WHEN v_desc THEN substr(v_token, 2) ELSE v_token END;
  v_known BOOLEAN;
  v_total INT;
  v_rows JSONB;
  v_states JSONB;
  v_types JSONB;
  v_pending INT;
  v_awaiting INT;
BEGIN
  v_known := v_col IN ('name', 'borrower', 'deal_type', 'stage', 'created_at', 'updated_at');

  SELECT count(*) INTO v_total FROM deals de
  WHERE (coalesce(p_search, '') = ''
         OR de.name ILIKE '%' || p_search || '%'
         OR de.borrower ILIKE '%' || p_search || '%'
         OR de.deal_type ILIKE '%' || p_search || '%')
    AND ((coalesce(p_state, '') <> '' AND de.current_state = p_state)
         OR (coalesce(p_state, '') = '' AND de.current_state IS NOT NULL
             AND de.current_state NOT IN ('ISSUED', 'ARCHIVED')))
    AND (p_tenant_id IS NULL OR de.tenant_id = p_tenant_id)
    AND (coalesce(p_deal_type, '') = '' OR de.deal_type = p_deal_type)
    AND (p_date_from IS NULL OR de.created_at >= p_date_from)
    AND (p_date_to IS NULL OR de.created_at < p_date_to);

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', r.id, 'name', coalesce(r.name, ''), 'deal_type', coalesce(r.deal_type, ''),
    'borrower', coalesce(r.borrower, ''), 'stage', coalesce(r.stage, ''),
    'tenant_id', r.tenant_id, 'created_at', r.created_at, 'updated_at', r.updated_at,
    'pending_with', r.owner_email, 'tenant_domain', r.email_domain,
    'document_count', r.document_count, 'document_required', r.document_required)
    ORDER BY
      CASE WHEN v_col = 'name' AND v_desc THEN r.name END DESC NULLS LAST,
      CASE WHEN v_col = 'name' AND NOT v_desc THEN r.name END ASC NULLS LAST,
      CASE WHEN v_col = 'borrower' AND v_desc THEN r.borrower END DESC NULLS LAST,
      CASE WHEN v_col = 'borrower' AND NOT v_desc THEN r.borrower END ASC NULLS LAST,
      CASE WHEN v_col = 'deal_type' AND v_desc THEN r.deal_type END DESC NULLS LAST,
      CASE WHEN v_col = 'deal_type' AND NOT v_desc THEN r.deal_type END ASC NULLS LAST,
      CASE WHEN v_col = 'stage' AND v_desc THEN r.stage END DESC NULLS LAST,
      CASE WHEN v_col = 'stage' AND NOT v_desc THEN r.stage END ASC NULLS LAST,
      CASE WHEN v_col = 'created_at' AND v_desc THEN r.created_at END DESC NULLS LAST,
      CASE WHEN v_col = 'created_at' AND NOT v_desc THEN r.created_at END ASC NULLS LAST,
      CASE WHEN v_col = 'updated_at' AND v_desc THEN r.updated_at END DESC NULLS LAST,
      CASE WHEN v_col = 'updated_at' AND NOT v_desc THEN r.updated_at END ASC NULLS LAST,
      CASE WHEN NOT v_known THEN r.updated_at END DESC NULLS LAST), '[]'::jsonb)
  INTO v_rows
  FROM (
    SELECT de.id, de.name, de.deal_type, de.borrower, de.current_state AS stage,
           de.tenant_id, de.created_at, de.updated_at, de.document_count,
           de.document_required, u.email AS owner_email, t.email_domain
    FROM deals de
    LEFT JOIN users u ON u.id = de.owner_id::text
    LEFT JOIN tenants t ON t.id = de.tenant_id
    WHERE (coalesce(p_search, '') = ''
           OR de.name ILIKE '%' || p_search || '%'
           OR de.borrower ILIKE '%' || p_search || '%'
           OR de.deal_type ILIKE '%' || p_search || '%')
      AND ((coalesce(p_state, '') <> '' AND de.current_state = p_state)
           OR (coalesce(p_state, '') = '' AND de.current_state IS NOT NULL
               AND de.current_state NOT IN ('ISSUED', 'ARCHIVED')))
      AND (p_tenant_id IS NULL OR de.tenant_id = p_tenant_id)
      AND (coalesce(p_deal_type, '') = '' OR de.deal_type = p_deal_type)
      AND (p_date_from IS NULL OR de.created_at >= p_date_from)
      AND (p_date_to IS NULL OR de.created_at < p_date_to)
    ORDER BY
      CASE WHEN v_col = 'name' AND v_desc THEN de.name END DESC NULLS LAST,
      CASE WHEN v_col = 'name' AND NOT v_desc THEN de.name END ASC NULLS LAST,
      CASE WHEN v_col = 'borrower' AND v_desc THEN de.borrower END DESC NULLS LAST,
      CASE WHEN v_col = 'borrower' AND NOT v_desc THEN de.borrower END ASC NULLS LAST,
      CASE WHEN v_col = 'deal_type' AND v_desc THEN de.deal_type END DESC NULLS LAST,
      CASE WHEN v_col = 'deal_type' AND NOT v_desc THEN de.deal_type END ASC NULLS LAST,
      CASE WHEN v_col = 'stage' AND v_desc THEN de.current_state END DESC NULLS LAST,
      CASE WHEN v_col = 'stage' AND NOT v_desc THEN de.current_state END ASC NULLS LAST,
      CASE WHEN v_col = 'created_at' AND v_desc THEN de.created_at END DESC NULLS LAST,
      CASE WHEN v_col = 'created_at' AND NOT v_desc THEN de.created_at END ASC NULLS LAST,
      CASE WHEN v_col = 'updated_at' AND v_desc THEN de.updated_at END DESC NULLS LAST,
      CASE WHEN v_col = 'updated_at' AND NOT v_desc THEN de.updated_at END ASC NULLS LAST,
      CASE WHEN NOT v_known THEN de.updated_at END DESC NULLS LAST
    LIMIT v_limit OFFSET v_offset
  ) r;

  SELECT coalesce(jsonb_agg(s.value ORDER BY s.value), '[]'::jsonb)
  INTO v_states
  FROM (
    SELECT DISTINCT current_state AS value FROM deals
    WHERE current_state IS NOT NULL AND current_state <> ''
  ) s;
  SELECT coalesce(jsonb_agg(s.value ORDER BY s.value), '[]'::jsonb)
  INTO v_types
  FROM (
    SELECT DISTINCT deal_type AS value FROM deals
    WHERE deal_type IS NOT NULL AND deal_type <> ''
  ) s;

  SELECT count(*) INTO v_pending FROM deals de
  WHERE de.current_state IS NOT NULL AND de.current_state NOT IN ('ISSUED', 'ARCHIVED');
  SELECT count(*) INTO v_awaiting FROM deals de
  WHERE de.current_state IS NOT NULL
    AND de.current_state NOT IN ('ISSUED', 'ARCHIVED')
    AND de.current_state = 'PENDING_APPROVAL';

  RETURN jsonb_build_object(
    'rows', v_rows, 'total', v_total, 'page', v_page, 'limit', v_limit,
    'facets', jsonb_build_object('states', v_states, 'deal_types', v_types),
    'summary', jsonb_build_object(
      'total_pending', v_pending, 'high_priority', NULL,
      'awaiting_checker', v_awaiting, 'avg_time_to_approve_days', NULL));
END;
$$;

CREATE OR REPLACE FUNCTION get_sa_deal_workspace(p_deal_id TEXT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_tenant UUID;
  v_deal JSONB;
  v_tenant_obj JSONB;
  v_template JSONB;
  v_transitions JSONB;
  v_audit JSONB;
  v_comments JSONB;
BEGIN
  SELECT d.tenant_id INTO v_tenant
  FROM deals d WHERE d.id = p_deal_id AND d.deleted_at IS NULL;
  IF v_tenant IS NULL THEN
    RETURN NULL;
  END IF;

  SELECT jsonb_build_object(
    'id', d.id, 'name', d.name, 'deal_type', d.deal_type, 'borrower', d.borrower,
    'sector', d.sector, 'facility_amount', d.facility_amount,
    'current_state', d.current_state, 'created_at', d.created_at,
    'updated_at', d.updated_at, 'state_changed_at', d.state_changed_at,
    'owner', u.email, 'document_count', d.document_count,
    'document_required', d.document_required, 'template_id', d.template_id)
  INTO v_deal
  FROM deals d
  LEFT JOIN users u ON u.id = d.owner_id::text
  WHERE d.id = p_deal_id;

  SELECT jsonb_build_object(
    'id', t.id, 'name', t.name, 'email_domain', t.email_domain, 'status', t.status)
  INTO v_tenant_obj
  FROM tenants t WHERE t.id = v_tenant;

  SELECT jsonb_build_object(
    'id', tp.id, 'name', tp.name, 'source', tp.source, 'is_active', tp.is_active,
    'version_ref', d.template_snapshot -> 'version_ref')
  INTO v_template
  FROM deals d
  JOIN templates tp ON tp.id = d.template_id
  WHERE d.id = p_deal_id;

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', x.id, 'from_state', x.from_state, 'to_state', x.to_state,
    'reason', x.reason, 'actor_id', x.actor_id,
    'actor', coalesce(u.first_name || ' ' || u.last_name, u.email, 'Platform'),
    'created_at', x.created_at) ORDER BY x.created_at), '[]'::jsonb)
  INTO v_transitions
  FROM (
    SELECT tr.* FROM deal_state_transitions tr
    WHERE tr.tenant_id = v_tenant AND tr.deal_id = p_deal_id
    ORDER BY tr.created_at DESC
    LIMIT 50
  ) x
  LEFT JOIN users u ON u.id = x.actor_id::text;

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', e.id, 'action', e.action, 'entity_type', e.entity_type,
    'entity_id', e.entity_id, 'correlation_id', e.correlation_id,
    'actor_id', e.actor_id,
    'actor', coalesce(u.first_name || ' ' || u.last_name, u.email, 'Platform'),
    'created_at', e.created_at) ORDER BY e.created_at), '[]'::jsonb)
  INTO v_audit
  FROM (
    SELECT ev.* FROM deal_audit_events ev
    WHERE ev.tenant_id = v_tenant AND ev.deal_id = p_deal_id
    ORDER BY ev.created_at DESC
    LIMIT 50
  ) e
  LEFT JOIN users u ON u.id = e.actor_id::text;

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', th.id, 'section_key', th.section_key, 'created_by', th.created_by,
    'created_by_name', coalesce(cu.first_name || ' ' || cu.last_name, cu.email),
    'created_at', th.created_at, 'resolved_at', th.resolved_at,
    'comments', (
      SELECT coalesce(jsonb_agg(jsonb_build_object(
        'id', c.id, 'body', c.body, 'author_id', c.author_id,
        'author', coalesce(au.first_name || ' ' || au.last_name, au.email,
                           'Former member'),
        'created_at', c.created_at) ORDER BY c.created_at), '[]'::jsonb)
      FROM paper_comments c
      LEFT JOIN users au ON au.id = c.author_id::text
      WHERE c.thread_id = th.id)
  ) ORDER BY th.created_at), '[]'::jsonb)
  INTO v_comments
  FROM paper_comment_threads th
  LEFT JOIN users cu ON cu.id = th.created_by::text
  WHERE th.tenant_id = v_tenant AND th.deal_id = p_deal_id;

  RETURN jsonb_build_object(
    'deal', v_deal, 'tenant', v_tenant_obj, 'template', v_template,
    'transitions', v_transitions, 'audit', v_audit, 'comments', v_comments);
END;
$$;

CREATE OR REPLACE FUNCTION get_sa_deal_documents(p_deal_id TEXT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_tenant UUID;
  v_docs JSONB;
  v_total_docs INT;
  v_total_versions INT;
BEGIN
  SELECT d.tenant_id INTO v_tenant
  FROM deals d WHERE d.id = p_deal_id AND d.deleted_at IS NULL;
  IF v_tenant IS NULL THEN
    RETURN NULL;
  END IF;

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', doc.id, 'display_name', doc.display_name,
    'document_kind', doc.document_kind, 'created_by', doc.created_by,
    'created_at', doc.created_at, 'archived_at', doc.archived_at,
    'versions', (
      SELECT coalesce(jsonb_agg(jsonb_build_object(
        'id', v.id, 'version_number', v.version_number,
        'original_filename', v.original_filename, 'mime_type', v.mime_type,
        'size_bytes', v.size_bytes, 'uploaded_at', v.uploaded_at,
        'uploaded_by', v.uploaded_by,
        'uploaded_by_name', coalesce(au.first_name || ' ' || au.last_name, au.email),
        'processing_status', v.processing_status, 'processing_error', v.processing_error,
        'extraction_status', v.extraction_status, 'extraction_error', v.extraction_error,
        'storage_path', v.storage_path) ORDER BY v.version_number DESC), '[]'::jsonb)
      FROM deal_document_versions v
      LEFT JOIN users au ON au.id = v.uploaded_by::text
      WHERE v.document_id = doc.id)
  ) ORDER BY doc.created_at), '[]'::jsonb)
  INTO v_docs
  FROM deal_documents doc
  WHERE doc.tenant_id = v_tenant AND doc.deal_id = p_deal_id;

  SELECT count(*), coalesce(sum((SELECT count(*) FROM deal_document_versions dv
                                 WHERE dv.document_id = doc.id)), 0)
  INTO v_total_docs, v_total_versions
  FROM deal_documents doc
  WHERE doc.tenant_id = v_tenant AND doc.deal_id = p_deal_id;

  RETURN jsonb_build_object(
    'documents', v_docs, 'total_documents', v_total_docs,
    'total_versions', v_total_versions::int);
END;
$$;

CREATE OR REPLACE FUNCTION get_sa_fact_sheet(p_deal_id TEXT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_tenant UUID;
BEGIN
  SELECT d.tenant_id INTO v_tenant
  FROM deals d WHERE d.id = p_deal_id AND d.deleted_at IS NULL;
  IF v_tenant IS NULL THEN
    RETURN NULL;
  END IF;
  RETURN get_fact_sheet_data(v_tenant, p_deal_id);
END;
$$;

CREATE OR REPLACE FUNCTION get_sa_validation(p_deal_id TEXT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_tenant UUID;
  v_blocking JSONB;
  v_failed_jobs JSONB;
  v_contradictions JSONB;
  v_reviews JSONB;
  v_evaluations JSONB;
  v_signoffs JSONB;
BEGIN
  SELECT d.tenant_id INTO v_tenant
  FROM deals d WHERE d.id = p_deal_id AND d.deleted_at IS NULL;
  IF v_tenant IS NULL THEN
    RETURN NULL;
  END IF;

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'document_id', b.document_id, 'display_name', doc.display_name,
    'version_id', b.id, 'processing_status', b.processing_status,
    'processing_error', b.processing_error, 'extraction_status', b.extraction_status,
    'extraction_error', b.extraction_error, 'uploaded_at', b.uploaded_at)
    ORDER BY b.document_id), '[]'::jsonb)
  INTO v_blocking
  FROM (
    SELECT DISTINCT ON (v.document_id) v.*
    FROM deal_document_versions v
    WHERE v.tenant_id = v_tenant AND v.deal_id = p_deal_id
      AND (v.processing_status = 'failed'
           OR v.extraction_status IN ('failed', 'invalid'))
    ORDER BY v.document_id, v.version_number DESC
  ) b
  JOIN deal_documents doc ON doc.id = b.document_id;

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', j.id, 'kind', j.kind, 'resource_id', j.resource_id, 'status', j.status,
    'attempts', j.attempts, 'last_error_type', j.last_error_type,
    'created_at', j.created_at, 'updated_at', j.updated_at)
    ORDER BY j.created_at DESC), '[]'::jsonb)
  INTO v_failed_jobs
  FROM (
    SELECT * FROM processing_jobs
    WHERE tenant_id = v_tenant AND deal_id = p_deal_id AND status = 'failed'
    ORDER BY created_at DESC
    LIMIT 50
  ) j;

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', c.id, 'concept_path', c.concept_path, 'diff_class', c.diff_class,
    'material', c.material, 'created_at', c.created_at) ORDER BY c.created_at),
    '[]'::jsonb)
  INTO v_contradictions
  FROM contradiction_register c
  WHERE c.tenant_id = v_tenant AND c.deal_id = p_deal_id AND c.status = 'open';

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', q.id, 'field_path', q.field_path, 'document_id', q.document_id,
    'page', q.page, 'extracted_value', q.extracted_value,
    'extracted_confidence', q.extracted_confidence, 'created_at', q.created_at)
    ORDER BY q.created_at), '[]'::jsonb)
  INTO v_reviews
  FROM fact_review_queue q
  WHERE q.tenant_id = v_tenant AND q.deal_id = p_deal_id AND q.status = 'pending';

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'section', s.section, 'constitution_version', s.constitution_version,
    'evaluations', s.guardrail_snapshot -> 'evaluations') ORDER BY s.section),
    '[]'::jsonb)
  INTO v_evaluations
  FROM deal_paper_sections s
  WHERE s.deal_id = p_deal_id
    AND jsonb_typeof(s.guardrail_snapshot -> 'evaluations') = 'array'
    AND jsonb_array_length(s.guardrail_snapshot -> 'evaluations') > 0;

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'section', s.section, 'constitution_version', s.constitution_version,
    'signoffs', s.guardrail_snapshot -> 'signoffs') ORDER BY s.section),
    '[]'::jsonb)
  INTO v_signoffs
  FROM deal_paper_sections s
  WHERE s.deal_id = p_deal_id
    AND jsonb_typeof(s.guardrail_snapshot -> 'signoffs') = 'array'
    AND jsonb_array_length(s.guardrail_snapshot -> 'signoffs') > 0;

  RETURN jsonb_build_object(
    'blocking_documents', v_blocking, 'failed_jobs', v_failed_jobs,
    'open_contradictions', v_contradictions, 'pending_fact_reviews', v_reviews,
    'guardrail_evaluations', v_evaluations, 'guardrail_signoffs', v_signoffs);
END;
$$;

CREATE OR REPLACE FUNCTION get_sa_paper(p_deal_id TEXT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_tenant UUID;
  v_template JSONB;
  v_sections JSONB;
  v_recommendation JSONB;
  v_final JSONB;
BEGIN
  SELECT d.tenant_id INTO v_tenant
  FROM deals d WHERE d.id = p_deal_id AND d.deleted_at IS NULL;
  IF v_tenant IS NULL THEN
    RETURN NULL;
  END IF;

  SELECT jsonb_build_object(
    'id', tp.id, 'name', tp.name, 'source', tp.source, 'is_active', tp.is_active,
    'version_ref', d.template_snapshot -> 'version_ref')
  INTO v_template
  FROM deals d
  JOIN templates tp ON tp.id = d.template_id
  WHERE d.id = p_deal_id;

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'section', s.section, 'status', s.status, 'error', s.error,
    'citations_count', coalesce(jsonb_array_length(s.citations), 0),
    'has_editor_value', s.editor_value IS NOT NULL,
    'editor_value', s.editor_value, 'prose', s.prose,
    'edit_revision', s.edit_revision, 'updated_at', s.updated_at)
    ORDER BY s.section), '[]'::jsonb)
  INTO v_sections
  FROM deal_paper_sections s
  WHERE s.deal_id = p_deal_id;

  SELECT jsonb_build_object(
    'id', r.id, 'body', r.body, 'author_id', r.author_id, 'signed_at', r.signed_at,
    'author', coalesce(u.first_name || ' ' || u.last_name, u.email, 'Former member'))
  INTO v_recommendation
  FROM deal_recommendations r
  LEFT JOIN users u ON u.id = r.author_id::text
  WHERE r.tenant_id = v_tenant AND r.deal_id = p_deal_id
  ORDER BY r.signed_at DESC NULLS LAST, r.id DESC
  LIMIT 1;

  SELECT jsonb_build_object(
    'available', a.id IS NOT NULL, 'id', a.id, 'digest', a.digest,
    'approved_at', a.known_at)
  INTO v_final
  FROM (SELECT 1) one
  LEFT JOIN LATERAL (
    SELECT a.id, a.digest, a.known_at
    FROM ledger_approvals a
    WHERE a.tenant_id = v_tenant AND a.deal_id = p_deal_id
    ORDER BY a.known_at DESC, a.id DESC
    LIMIT 1
  ) a ON true;

  RETURN jsonb_build_object(
    'template', v_template, 'sections', v_sections,
    'recommendation', v_recommendation, 'final_paper', v_final);
END;
$$;

CREATE OR REPLACE FUNCTION get_sa_queue(p_tenant_id UUID, p_page INT, p_limit INT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_page INT := greatest(coalesce(p_page, 1), 1);
  v_limit INT := least(greatest(coalesce(p_limit, 20), 1), 100);
  v_offset INT := (v_page - 1) * v_limit;
  v_total INT;
  v_rows JSONB;
  v_reviews JSONB;
  v_decisions JSONB;
BEGIN
  SELECT count(*) INTO v_total FROM deals de
  WHERE de.current_state IS NOT NULL
    AND de.current_state NOT IN ('ISSUED', 'ARCHIVED')
    AND (p_tenant_id IS NULL OR de.tenant_id = p_tenant_id);

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', r.id, 'name', coalesce(r.name, ''), 'deal_type', coalesce(r.deal_type, ''),
    'borrower', coalesce(r.borrower, ''), 'stage', coalesce(r.stage, ''),
    'tenant_id', r.tenant_id, 'created_at', r.created_at, 'updated_at', r.updated_at,
    'pending_with', r.owner_email, 'tenant_domain', r.email_domain,
    'document_count', r.document_count, 'document_required', r.document_required)
    ORDER BY r.updated_at DESC NULLS LAST), '[]'::jsonb)
  INTO v_rows
  FROM (
    SELECT de.id, de.name, de.deal_type, de.borrower, de.current_state AS stage,
           de.tenant_id, de.created_at, de.updated_at, de.document_count,
           de.document_required, u.email AS owner_email, t.email_domain
    FROM deals de
    LEFT JOIN users u ON u.id = de.owner_id::text
    LEFT JOIN tenants t ON t.id = de.tenant_id
    WHERE de.current_state IS NOT NULL
      AND de.current_state NOT IN ('ISSUED', 'ARCHIVED')
      AND (p_tenant_id IS NULL OR de.tenant_id = p_tenant_id)
    ORDER BY de.updated_at DESC NULLS LAST
    LIMIT v_limit OFFSET v_offset
  ) r;

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', q.id, 'deal_id', q.deal_id, 'tenant_id', q.tenant_id,
    'field_path', q.field_path, 'document_id', q.document_id, 'page', q.page,
    'extracted_value', q.extracted_value,
    'extracted_confidence', q.extracted_confidence, 'created_at', q.created_at)
    ORDER BY q.created_at), '[]'::jsonb)
  INTO v_reviews
  FROM (
    SELECT * FROM fact_review_queue
    WHERE status = 'pending'
      AND (p_tenant_id IS NULL OR tenant_id = p_tenant_id)
    ORDER BY created_at DESC
    LIMIT 50
  ) q;

  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', x.id, 'deal_id', x.deal_id, 'tenant_id', x.tenant_id,
    'from_state', x.from_state, 'to_state', x.to_state, 'reason', x.reason,
    'actor_id', x.actor_id,
    'actor', coalesce(u.first_name || ' ' || u.last_name, u.email, 'Platform'),
    'created_at', x.created_at) ORDER BY x.created_at DESC), '[]'::jsonb)
  INTO v_decisions
  FROM (
    SELECT tr.* FROM deal_state_transitions tr
    WHERE tr.from_state = 'PENDING_APPROVAL'
      AND (p_tenant_id IS NULL OR tr.tenant_id = p_tenant_id)
    ORDER BY tr.created_at DESC
    LIMIT 50
  ) x
  LEFT JOIN users u ON u.id = x.actor_id::text;

  RETURN jsonb_build_object(
    'deals', jsonb_build_object(
      'rows', v_rows, 'total', v_total, 'page', v_page, 'limit', v_limit,
      'pages', greatest(1, ceil(v_total::numeric / v_limit)::int)),
    'fact_reviews', v_reviews, 'recent_decisions', v_decisions);
END;
$$;
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_deals_live_updated
    ON deals (updated_at DESC NULLS LAST) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_review_queue_status_created
    ON fact_review_queue (status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_guardrail_evaluations_result_evaluated
    ON guardrail_evaluations (result, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS ix_deal_state_transitions_created
    ON deal_state_transitions (created_at DESC);
"""

_PRIVILEGES = """
DO $$
DECLARE fn TEXT;
BEGIN
  FOREACH fn IN ARRAY ARRAY[
    'get_sa_overview()',
    'get_sa_deals_list(text,text,uuid,text,date,date,text,int,int)',
    'get_sa_deal_ref(text)',
    'get_sa_deal_workspace(text)',
    'get_sa_deal_documents(text)',
    'get_sa_fact_sheet(text)',
    'get_sa_validation(text)',
    'get_sa_paper(text)',
    'get_sa_queue(uuid,int,int)',
    'get_sa_template_usage()'
  ] LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', fn);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
      EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', fn);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
      EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', fn);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
      EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO creditpaper_runtime', fn);
    END IF;
  END LOOP;
END $$;
"""

_DOWNGRADE_FNS = """
DROP FUNCTION IF EXISTS get_sa_overview();
DROP FUNCTION IF EXISTS get_sa_deals_list(text, text, uuid, text, date, date, text, int, int);
DROP FUNCTION IF EXISTS get_sa_deal_ref(text);
DROP FUNCTION IF EXISTS get_sa_deal_workspace(text);
DROP FUNCTION IF EXISTS get_sa_deal_documents(text);
DROP FUNCTION IF EXISTS get_sa_fact_sheet(text);
DROP FUNCTION IF EXISTS get_sa_validation(text);
DROP FUNCTION IF EXISTS get_sa_paper(text);
DROP FUNCTION IF EXISTS get_sa_queue(uuid, int, int);
DROP FUNCTION IF EXISTS get_sa_template_usage();
"""

_DOWNGRADE_INDEXES = """
DROP INDEX IF EXISTS ix_deals_live_updated;
DROP INDEX IF EXISTS ix_review_queue_status_created;
DROP INDEX IF EXISTS ix_guardrail_evaluations_result_evaluated;
DROP INDEX IF EXISTS ix_deal_state_transitions_created;
"""


def upgrade() -> None:
    op.execute(_FUNCTIONS)
    op.execute(_INDEXES)
    op.execute(_PRIVILEGES)


def downgrade() -> None:
    op.execute(_DOWNGRADE_FNS)
    op.execute(_DOWNGRADE_INDEXES)
