"""Separate approval blockers from Flow H document disclosures.

Unplaced and invalid current documents are disclosed and acknowledged at approval;
only active assigned documents still processing block the approval.
"""

from alembic import op

revision = "0045_approval_disclosures"
down_revision = "0044_template_scoped_approval"
branch_labels = None
depends_on = None


_APPROVAL_CHECKLIST_FN = """
CREATE OR REPLACE FUNCTION public.get_approval_checklist(
    p_tenant_id UUID,
    p_deal_id TEXT
) RETURNS JSONB
LANGUAGE SQL
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    WITH    current_documents AS (
        SELECT d.id AS document_id,
               v.id AS version_id,
               COALESCE(NULLIF(trim(d.display_name), ''),
                        NULLIF(trim(v.original_filename), ''), 'Unnamed document') AS document_name,
               v.processing_status,
               v.extraction_status,
               EXISTS (
                   SELECT 1
                   FROM public.document_slot_files f
                   JOIN public.document_slots s
                     ON s.tenant_id = f.tenant_id AND s.id = f.slot_id
                   WHERE f.tenant_id = p_tenant_id
                     AND s.deal_id = p_deal_id
                     AND f.document_id = d.id
                     AND f.version_id = v.id
                     AND f.role = 'assigned'
               ) AS assigned
        FROM public.deal_documents d
        JOIN public.deal_document_versions v
          ON v.tenant_id = d.tenant_id AND v.document_id = d.id
        WHERE d.tenant_id = p_tenant_id
          AND d.deal_id = p_deal_id
          AND d.archived_at IS NULL
          AND v.superseded_at IS NULL
    ),
    job_state AS (
        SELECT
            EXISTS (
                SELECT 1
                FROM public.processing_jobs j
                WHERE j.tenant_id = p_tenant_id AND j.deal_id = p_deal_id
                  AND j.status IN ('pending', 'running')
                  AND (
                      j.kind NOT IN ('document', 'parse')
                      OR EXISTS (
                          SELECT 1 FROM current_documents d
                          WHERE d.version_id::text = j.resource_id
                            AND d.assigned AND d.extraction_status IS DISTINCT FROM 'invalid'
                      )
                  )
            ) AS processing_pending,
            EXISTS (
                SELECT 1
                FROM public.processing_jobs j
                WHERE j.tenant_id = p_tenant_id AND j.deal_id = p_deal_id
                  AND j.status = 'failed'
                  AND j.kind NOT IN ('document', 'parse')
                  AND NOT EXISTS (
                      SELECT 1 FROM public.processing_jobs newer
                      WHERE newer.tenant_id = j.tenant_id
                        AND newer.deal_id = j.deal_id
                        AND newer.kind = j.kind
                        AND newer.resource_id = j.resource_id
                        AND newer.created_at > j.created_at
                  )
            ) AS processing_failed
    )
    SELECT jsonb_build_object(
        'revision', COALESCE(d.ledger_revision, 0),
        'processing_pending', j.processing_pending,
        'processing_failed', j.processing_failed,
        'pending_review', EXISTS (
            SELECT 1
            FROM public.fact_review_queue q
            WHERE q.tenant_id = p_tenant_id AND q.deal_id = p_deal_id AND q.status = 'pending'
              AND EXISTS (
                  SELECT 1
                  FROM public.ledger_assertions a
                  JOIN public.deal_document_versions v
                    ON v.tenant_id = a.tenant_id AND v.id = a.source_version_id
                  JOIN public.deal_documents d
                    ON d.tenant_id = v.tenant_id AND d.id = v.document_id
                  WHERE a.tenant_id = q.tenant_id AND a.deal_id = q.deal_id AND a.id = q.assertion_id
                    AND d.archived_at IS NULL AND v.superseded_at IS NULL
                    AND v.extraction_status IS DISTINCT FROM 'invalid'
                    AND EXISTS (
                        SELECT 1
                        FROM public.document_slot_files f
                        JOIN public.document_slots s
                          ON s.tenant_id = f.tenant_id AND s.id = f.slot_id
                        WHERE f.tenant_id = a.tenant_id AND s.deal_id = a.deal_id
                          AND f.document_id = v.document_id AND f.version_id = v.id
                          AND f.role = 'assigned'
                    )
              )
        ),
        'outstanding_items', COALESCE((
            SELECT jsonb_agg(item ORDER BY document_name, id)
            FROM (
                SELECT jsonb_build_object(
                    'id', CASE
                        WHEN NOT cd.assigned AND cd.extraction_status = 'invalid'
                            THEN 'unplaced-invalid:' || cd.document_id::text || ':' || cd.version_id::text
                        WHEN NOT cd.assigned THEN 'unplaced:' || cd.document_id::text || ':' || cd.version_id::text
                        ELSE 'invalid:' || cd.document_id::text || ':' || cd.version_id::text
                    END,
                    'kind', CASE WHEN NOT cd.assigned THEN 'unplaced' ELSE 'invalid' END,
                    'document_name', cd.document_name,
                    'description', CASE
                        WHEN NOT cd.assigned AND cd.extraction_status = 'invalid'
                            THEN 'Unassigned and marked invalid; excluded from approval blockers.'
                        WHEN NOT cd.assigned
                            THEN 'Not assigned to a document slot; excluded from approval blockers.'
                        ELSE 'Marked invalid; excluded from approval blockers.'
                    END
                ) AS item, cd.document_name,
                CASE WHEN NOT cd.assigned AND cd.extraction_status = 'invalid'
                    THEN 'unplaced-invalid:' || cd.document_id::text || ':' || cd.version_id::text
                    WHEN NOT cd.assigned THEN 'unplaced:' || cd.document_id::text || ':' || cd.version_id::text
                    ELSE 'invalid:' || cd.document_id::text || ':' || cd.version_id::text
                END AS id
                FROM current_documents cd
                WHERE NOT cd.assigned OR cd.extraction_status IS NOT DISTINCT FROM 'invalid'
            ) disclosures
        ), '[]'::jsonb),
        'blocking_documents', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'id', cd.document_id,
                'document_name', cd.document_name
            ) ORDER BY cd.document_name)
            FROM current_documents cd
            WHERE cd.assigned
              AND cd.extraction_status IS DISTINCT FROM 'invalid'
              AND (cd.processing_status IS DISTINCT FROM 'ready' OR cd.extraction_status IS DISTINCT FROM 'ready')
        ), '[]'::jsonb),
        'failed_document_jobs', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'id', cd.document_id,
                'document_name', cd.document_name
            ) ORDER BY cd.document_name)
            FROM public.processing_jobs j
            JOIN current_documents cd ON cd.version_id::text = j.resource_id
            WHERE j.tenant_id = p_tenant_id AND j.deal_id = p_deal_id
              AND j.kind = 'document' AND j.status = 'failed'
              AND cd.assigned AND cd.extraction_status IS DISTINCT FROM 'invalid'
              AND NOT EXISTS (
                  SELECT 1 FROM public.processing_jobs newer
                  WHERE newer.tenant_id = j.tenant_id AND newer.deal_id = j.deal_id
                    AND newer.kind = j.kind AND newer.resource_id = j.resource_id
                    AND newer.created_at > j.created_at
              )
        ), '[]'::jsonb)
    )
    FROM public.deals d
    CROSS JOIN job_state j
    WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id;
$$;
"""

_OPEN_CONTRADICTIONS_FN = """
CREATE OR REPLACE FUNCTION public.get_deal_contradictions(
    p_tenant_id UUID,
    p_deal_id TEXT,
    p_status TEXT
) RETURNS JSONB
LANGUAGE SQL
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'id', c.id,
        'concept_path', c.concept_path,
        'assertion_ids', to_jsonb(c.assertion_ids),
        'diff_class', c.diff_class,
        'material', c.material,
        'blocking', (c.status = 'open' AND c.material AND c.diff_class = 'contradiction'),
        'status', c.status,
        'created_at', c.created_at,
        'resolved_value', c.resolved_value,
        'resolved_assertion_id', c.resolved_assertion_id,
        'resolved_by', c.resolved_by,
        'reason', c.reason,
        'resolved_at', c.resolved_at,
        'registry_version', c.registry_version,
        'assertions', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'id', a.id,
                'value', a.fact->'value',
                'document_name', COALESCE(NULLIF(trim(d.display_name), ''),
                                          NULLIF(trim(v.original_filename), ''), 'Analyst entry'),
                'page', a.fact->'provenance'->'page'
            ) ORDER BY a.recorded_at, a.id)
            FROM unnest(c.assertion_ids) member(assertion_id)
            JOIN public.ledger_assertions a
              ON a.tenant_id = c.tenant_id AND a.deal_id = c.deal_id AND a.id = member.assertion_id
            LEFT JOIN public.deal_document_versions v
              ON v.tenant_id = a.tenant_id AND v.id = a.source_version_id
            LEFT JOIN public.deal_documents d
              ON d.tenant_id = v.tenant_id AND d.id = v.document_id
        ), '[]'::jsonb)
    ) ORDER BY c.created_at, c.id) FILTER (WHERE c.id IS NOT NULL), '[]'::jsonb)
    FROM public.deals owned
    LEFT JOIN public.contradiction_register c
      ON c.tenant_id = owned.tenant_id AND c.deal_id = owned.id
     AND (p_status IS NULL OR c.status = p_status)
    WHERE owned.tenant_id = p_tenant_id AND owned.id = p_deal_id
    GROUP BY owned.id;
$$;
"""

_PRIVILEGES = """
REVOKE ALL ON FUNCTION public.get_approval_checklist(UUID, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_deal_contradictions(UUID, TEXT, TEXT) FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.get_approval_checklist(UUID, TEXT) FROM '
                || quote_ident(role_name);
            EXECUTE 'REVOKE ALL ON FUNCTION public.get_deal_contradictions(UUID, TEXT, TEXT) FROM '
                || quote_ident(role_name);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
        GRANT EXECUTE ON FUNCTION public.get_approval_checklist(UUID, TEXT) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.get_deal_contradictions(UUID, TEXT, TEXT) TO creditpaper_runtime;
    END IF;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_APPROVAL_CHECKLIST_FN)
    op.execute(_OPEN_CONTRADICTIONS_FN)
    op.execute(_PRIVILEGES)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.get_deal_contradictions(UUID, TEXT, TEXT)")
    op.execute("DROP FUNCTION IF EXISTS public.get_approval_checklist(UUID, TEXT)")
