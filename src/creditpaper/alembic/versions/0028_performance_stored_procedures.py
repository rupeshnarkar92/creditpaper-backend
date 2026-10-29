"""Performance: PostgreSQL functions for hot read paths.

Combines sequential Python->DB round-trips into single server-side calls.
Each function returns JSONB; presentation and model calculations stay in Python.
Apply this migration before deploying the callers. No silent query fallback is used.

Functions added:
  - get_slot_board(tenant_id, deal_id)        -- slots endpoint (9 queries -> 1)
  - get_workspace_payload(tenant_id, deal_id) -- workspace endpoint (6 queries -> 1)
  - get_fact_sheet_data(tenant_id, deal_id)   -- fact-sheet endpoint (7 queries -> 1)
  - get_tenant_activity(tenant_id, category, limit, before_ts, before_id)
"""

from alembic import op

revision = "0028_stored_procedures"
down_revision = "0027_doc_unique_filename"
branch_labels = None
depends_on = None


_SLOT_BOARD_FN = """
CREATE OR REPLACE FUNCTION get_slot_board(
    p_tenant_id UUID,
    p_deal_id   TEXT
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_deal       JSONB;
    v_slot_types JSONB;
    v_slots      JSONB;
    v_files      JSONB;
    v_documents  JSONB;
    v_refused    JSONB;
    v_result     JSONB;
BEGIN
    -- 1. deal
    SELECT to_jsonb(d) INTO v_deal
    FROM deals d
    WHERE d.id = p_deal_id AND d.tenant_id = p_tenant_id;

    IF v_deal IS NULL THEN
        RETURN NULL;
    END IF;

    IF coalesce(v_deal->>'deal_type', '') NOT IN ('construction', 'bridge', 'residual_stock') THEN
        RETURN jsonb_build_object('deal', v_deal);
    END IF;

    -- 2. slot types (platform rows, not scoped by tenant)
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'key',         st.key,
            'name',        st.name,
            'description', st.description,
            'category',    st.category,
            'applies_to',  st.applies_to,
            'optional_for',st.optional_for,
            'multi',       st.multi,
            'extractor',   st.extractor,
            'model_block', st.model_block,
            'checks',      st.checks,
            'sort_order',  st.sort_order
        ) ORDER BY st.sort_order
    ), '[]'::jsonb)
    INTO v_slot_types
    FROM document_slot_types st;

    -- Lazily create applicable slots. The single statement commits atomically;
    -- concurrent first reads are safe because the unique constraint arbitrates.
    INSERT INTO document_slots (tenant_id, deal_id, slot_key, status)
    SELECT p_tenant_id, p_deal_id, st.key, 'empty'
    FROM document_slot_types st
    WHERE 'all' = ANY(st.applies_to) OR v_deal->>'deal_type' = ANY(st.applies_to)
    ON CONFLICT (deal_id, slot_key) DO NOTHING;

    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'id',           s.id,
            'slot_key',     s.slot_key,
            'status',       s.status,
            'confidence',   s.confidence,
            'match_reason', s.match_reason,
            'error',        s.error,
            'updated_at',   s.updated_at
        )
    ), '[]'::jsonb)
    INTO v_slots
    FROM document_slots s
    WHERE s.tenant_id = p_tenant_id AND s.deal_id = p_deal_id;


    -- 5. refused classes
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'key',      r.key,
            'name',     r.name,
            'reason',   r.reason,
            'match',    r.match,
            'decision', r.decision
        ) ORDER BY r.key
    ), '[]'::jsonb)
    INTO v_refused
    FROM document_refused_classes r;

    -- 6. slot files (JOIN)
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'slot_id',      f.slot_id,
            'document_id',  f.document_id,
            'version_id',   f.version_id,
            'role',         f.role,
            'confidence',   f.confidence,
            'match_reason', f.match_reason,
            'created_at',   f.created_at
        ) ORDER BY f.created_at
    ), '[]'::jsonb)
    INTO v_files
    FROM document_slot_files f
    JOIN document_slots s ON s.id = f.slot_id
    WHERE s.tenant_id = p_tenant_id AND s.deal_id = p_deal_id AND f.tenant_id = p_tenant_id;

    -- 7. documents with versions (correlated subquery per doc)
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'document_id',   d.id,
            'display_name',  d.display_name,
            'document_kind', d.document_kind,
            'created_at',    d.created_at,
            'versions',      (
                SELECT coalesce(jsonb_agg(
                    jsonb_build_object(
                        'version_id',        v.id,
                        'version_number',    v.version_number,
                        'original_filename', v.original_filename,
                        'size_bytes',        v.size_bytes,
                        'uploaded_at',       v.uploaded_at,
                        'processing_status', v.processing_status,
                        'extraction_status', v.extraction_status,
                        'extraction_error',  v.extraction_error,
                        'superseded_at',     v.superseded_at
                    ) ORDER BY v.version_number DESC
                ), '[]'::jsonb)
                FROM deal_document_versions v
                WHERE v.document_id = d.id AND v.tenant_id = p_tenant_id
            )
        ) ORDER BY d.created_at
    ), '[]'::jsonb)
    INTO v_documents
    FROM deal_documents d
    WHERE d.tenant_id = p_tenant_id AND d.deal_id = p_deal_id
      AND d.archived_at IS NULL
      AND EXISTS (SELECT 1 FROM deal_document_versions v
                  WHERE v.document_id = d.id AND v.tenant_id = p_tenant_id);

    v_result := jsonb_build_object(
        'deal',       v_deal,
        'slot_types', v_slot_types,
        'slots',      v_slots,
        'files',      v_files,
        'documents',  v_documents,
        'refused',    v_refused
    );
    RETURN v_result;
END;
$$;
"""

_WORKSPACE_FN = """
CREATE OR REPLACE FUNCTION get_workspace_payload(
    p_tenant_id UUID,
    p_deal_id   TEXT
) RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_deal   JSONB;
    v_docs   JSONB;
    v_audit  JSONB;
    v_result JSONB;
    v_names JSONB;
    v_ids TEXT[];
BEGIN
    -- 1. deal
    SELECT to_jsonb(d) INTO v_deal
    FROM (
        SELECT id, tenant_id, name, borrower, deal_type, current_state,
               workspace_revision, state_changed_at, state_changed_by,
               facility_amount, status, updated_at
        FROM deals
        WHERE id = p_deal_id AND tenant_id = p_tenant_id
    ) d;

    IF v_deal IS NULL THEN
        RETURN NULL;
    END IF;

    -- 2. documents (non-archived) with nested versions
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'id',            d.id,
            'display_name',  d.display_name,
            'document_kind', d.document_kind,
            'created_at',    d.created_at,
            'versions',      (
                SELECT coalesce(jsonb_agg(
                    jsonb_build_object(
                        'id',                v.id,
                        'version_number',    v.version_number,
                        'original_filename', v.original_filename,
                        'mime_type',         v.mime_type,
                        'size_bytes',        v.size_bytes,
                        'content_sha256',    v.content_sha256,
                        'uploaded_by',       v.uploaded_by,
                        'uploaded_at',       v.uploaded_at,
                        'processing_status', v.processing_status,
                        'extraction_status', v.extraction_status,
                        'extraction_error',  v.extraction_error
                    ) ORDER BY v.version_number DESC
                ), '[]'::jsonb)
                FROM deal_document_versions v
                WHERE v.document_id = d.id AND v.tenant_id = p_tenant_id
            )
        ) ORDER BY d.created_at DESC
    ), '[]'::jsonb)
    INTO v_docs
    FROM deal_documents d
    WHERE d.deal_id = p_deal_id AND d.tenant_id = p_tenant_id AND d.archived_at IS NULL;

    -- 3. recent audit events (LIMIT 20)
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'id',            a.id,
            'actor_id',      a.actor_id,
            'action',        a.action,
            'entity_type',   a.entity_type,
            'entity_id',     a.entity_id,
            'metadata',      a.event_metadata,
            'created_at',    a.created_at
        ) ORDER BY a.created_at DESC, a.id DESC
    ), '[]'::jsonb)
    INTO v_audit
    FROM (
        SELECT id, actor_id, action, entity_type, entity_id,
               event_metadata, created_at
        FROM deal_audit_events
        WHERE deal_id = p_deal_id AND tenant_id = p_tenant_id
        ORDER BY created_at DESC, id DESC
        LIMIT 20
    ) a;

    -- 3. collect actor ids and look up names
    SELECT array_agg(DISTINCT e->>'actor_id')
    INTO v_ids
    FROM jsonb_array_elements(v_audit) e
    WHERE e->>'actor_id' IS NOT NULL;

    IF v_ids IS NOT NULL AND array_length(v_ids, 1) > 0 THEN
        SELECT coalesce(jsonb_object_agg(
            u.id::text,
            CASE
                WHEN trim(trim(coalesce(u.first_name,'')) || ' ' || trim(coalesce(u.last_name,''))) <> ''
                    THEN trim(trim(coalesce(u.first_name,'')) || ' ' || trim(coalesce(u.last_name,'')))
                ELSE split_part(u.email, '@', 1)
            END
        ), '{}'::jsonb)
        INTO v_names
        FROM users u
        WHERE u.id = ANY(v_ids) AND u.domain_id = p_tenant_id::text;
    ELSE
        v_names := '{}'::jsonb;
    END IF;

    v_result := jsonb_build_object(
        'deal',     v_deal,
        'documents', v_docs,
        'audit',    v_audit,
        'names',    v_names
    );
    RETURN v_result;
END;
$$;
"""

_FACT_SHEET_FN = """
CREATE OR REPLACE FUNCTION get_fact_sheet_data(
    p_tenant_id UUID,
    p_deal_id   TEXT
) RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_deal       JSONB;
    v_slot_types JSONB;
    v_documents  JSONB;
    v_slots      JSONB;
    v_files      JSONB;
    v_model      JSONB;
    v_result     JSONB;
BEGIN
    -- 1. deal
    SELECT to_jsonb(d) INTO v_deal
    FROM deals d
    WHERE d.id = p_deal_id AND d.tenant_id = p_tenant_id;

    IF v_deal IS NULL THEN
        RETURN NULL;
    END IF;

    IF coalesce(v_deal->>'deal_type', '') NOT IN ('construction', 'bridge', 'residual_stock') THEN
        RETURN jsonb_build_object('deal', v_deal);
    END IF;

    -- 2. slot types
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'key',         st.key,
            'name',        st.name,
            'description', st.description,
            'category',    st.category,
            'applies_to',  st.applies_to,
            'optional_for',st.optional_for,
            'multi',       st.multi,
            'extractor',   st.extractor,
            'model_block', st.model_block,
            'checks',      st.checks,
            'sort_order',  st.sort_order
        ) ORDER BY st.sort_order
    ), '[]'::jsonb)
    INTO v_slot_types
    FROM document_slot_types st;

    -- 3. documents with versions
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'document_id',   d.id,
            'display_name',  d.display_name,
            'document_kind', d.document_kind,
            'created_at',    d.created_at,
            'versions',      (
                SELECT coalesce(jsonb_agg(
                    jsonb_build_object(
                        'version_id',        v.id,
                        'version_number',    v.version_number,
                        'original_filename', v.original_filename,
                        'size_bytes',        v.size_bytes,
                        'uploaded_at',       v.uploaded_at,
                        'processing_status', v.processing_status,
                        'extraction_status', v.extraction_status,
                        'extraction_error',  v.extraction_error,
                        'superseded_at',     v.superseded_at
                    ) ORDER BY v.version_number DESC
                ), '[]'::jsonb)
                FROM deal_document_versions v
                WHERE v.document_id = d.id AND v.tenant_id = p_tenant_id
            )
        ) ORDER BY d.created_at
    ), '[]'::jsonb)
    INTO v_documents
    FROM deal_documents d
    WHERE d.tenant_id = p_tenant_id AND d.deal_id = p_deal_id
      AND d.archived_at IS NULL
      AND EXISTS (SELECT 1 FROM deal_document_versions v
                  WHERE v.document_id = d.id AND v.tenant_id = p_tenant_id);

    -- 4. slots
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'id',           s.id,
            'slot_key',     s.slot_key,
            'status',       s.status,
            'confidence',   s.confidence,
            'match_reason', s.match_reason,
            'error',        s.error,
            'updated_at',   s.updated_at
        )
    ), '[]'::jsonb)
    INTO v_slots
    FROM document_slots s
    WHERE s.tenant_id = p_tenant_id AND s.deal_id = p_deal_id;

    -- 5. slot files (assigned only, for name mapping)
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'slot_id',      f.slot_id,
            'document_id',  f.document_id,
            'role',         f.role
        )
    ), '[]'::jsonb)
    INTO v_files
    FROM document_slot_files f
    JOIN document_slots s ON s.id = f.slot_id
    WHERE s.tenant_id = p_tenant_id AND s.deal_id = p_deal_id
      AND f.tenant_id = p_tenant_id AND f.role = 'assigned';

    -- 6. model snapshot
    SELECT m.model INTO v_model
    FROM deal_model_snapshots m
    WHERE m.deal_id = p_deal_id AND m.tenant_id = p_tenant_id;

    v_result := jsonb_build_object(
        'deal',       v_deal,
        'slot_types', v_slot_types,
        'documents',  v_documents,
        'slots',      v_slots,
        'files',      v_files,
        'model',      v_model
    );
    RETURN v_result;
END;
$$;
"""

_ACTIVITY_FN = """
CREATE OR REPLACE FUNCTION get_tenant_activity(
    p_tenant_id UUID,
    p_category  TEXT,
    p_limit     INT,
    p_before_ts TIMESTAMPTZ,
    p_before_id UUID
) RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_counts  JSONB;
    v_events  JSONB;
    v_names   JSONB;
    v_ids     TEXT[];
    v_result  JSONB;
BEGIN
    -- 1. action counts (no LIMIT — full scan for this tenant)
    SELECT coalesce(jsonb_object_agg(q.action, q.cnt), '{}'::jsonb)
    INTO v_counts
    FROM (
        SELECT e.action, count(*) AS cnt
        FROM deal_audit_events e
        JOIN deals d ON d.id = e.deal_id AND d.tenant_id = e.tenant_id
        WHERE e.tenant_id = p_tenant_id
        GROUP BY e.action
    ) q;

    -- 2. paged events with deal names
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'id',            e.id,
            'actor_id',      e.actor_id,
            'action',        e.action,
            'entity_type',   e.entity_type,
            'entity_id',     e.entity_id,
            'metadata',      e.event_metadata,
            'created_at',    e.created_at,
            'deal',          jsonb_build_object('id', d.id, 'name', d.name)
        ) ORDER BY e.created_at DESC, e.id DESC
    ), '[]'::jsonb)
    INTO v_events
    FROM (
        SELECT e2.id, e2.actor_id, e2.action, e2.entity_type,
               e2.entity_id, e2.event_metadata, e2.created_at, e2.tenant_id, e2.deal_id
        FROM deal_audit_events e2
        JOIN deals d2 ON d2.id = e2.deal_id AND d2.tenant_id = e2.tenant_id
        WHERE e2.tenant_id = p_tenant_id
          AND (p_category IS NULL OR CASE
              WHEN starts_with(e2.action, 'document.') OR starts_with(e2.action, 'document_slot.')
                   OR starts_with(e2.action, 'document_slots.') OR starts_with(e2.action, 'extraction.')
                  THEN 'documents'
              WHEN starts_with(e2.action, 'fact.') THEN 'facts'
              WHEN starts_with(e2.action, 'paper.') OR starts_with(e2.action, 'template.') THEN 'paper'
              WHEN starts_with(e2.action, 'state.') THEN 'stage'
              ELSE 'deal'
          END = p_category)
          AND (p_before_ts IS NULL OR (e2.created_at, e2.id) < (p_before_ts, p_before_id))
        ORDER BY e2.created_at DESC, e2.id DESC
        LIMIT p_limit + 1
    ) e
    JOIN deals d ON d.tenant_id = e.tenant_id AND d.id = e.deal_id;

    -- 3. collect actor ids and look up names
    SELECT array_agg(DISTINCT e->>'actor_id')
    INTO v_ids
    FROM jsonb_array_elements(v_events) e
    WHERE e->>'actor_id' IS NOT NULL;

    IF v_ids IS NOT NULL AND array_length(v_ids, 1) > 0 THEN
        SELECT coalesce(jsonb_object_agg(
            u.id::text,
            CASE
                WHEN trim(trim(coalesce(u.first_name,'')) || ' ' || trim(coalesce(u.last_name,''))) <> ''
                    THEN trim(trim(coalesce(u.first_name,'')) || ' ' || trim(coalesce(u.last_name,'')))
                ELSE split_part(u.email, '@', 1)
            END
        ), '{}'::jsonb)
        INTO v_names
        FROM users u
        WHERE u.id = ANY(v_ids) AND u.domain_id = p_tenant_id::text;
    ELSE
        v_names := '{}'::jsonb;
    END IF;

    v_result := jsonb_build_object(
        'counts', v_counts,
        'events', v_events,
        'names',  v_names
    );
    RETURN v_result;
END;
$$;
"""

_DOWNGRADE_FNS = """
DROP FUNCTION IF EXISTS get_slot_board(UUID, TEXT);
DROP FUNCTION IF EXISTS get_workspace_payload(UUID, TEXT);
DROP FUNCTION IF EXISTS get_fact_sheet_data(UUID, TEXT);
DROP FUNCTION IF EXISTS get_tenant_activity(UUID, TEXT, INT, TIMESTAMPTZ, UUID);
"""


def upgrade() -> None:
    op.execute(_SLOT_BOARD_FN)
    op.execute(_WORKSPACE_FN)
    op.execute(_FACT_SHEET_FN)
    op.execute(_ACTIVITY_FN)


def downgrade() -> None:
    op.execute(_DOWNGRADE_FNS)
