"""Paper workflow safeguards, guardrail snapshots, comments, and usage accounting."""

from alembic import op

revision = "0046_paper_workflow"
down_revision = "0045_approval_disclosures"
branch_labels = None
depends_on = None


_SQL = """
ALTER TABLE public.deal_paper_sections
    ADD COLUMN guardrail_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN constitution_version TEXT NOT NULL DEFAULT 'unknown';

CREATE TABLE public.paper_section_edit_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    deal_id TEXT NOT NULL,
    section INTEGER NOT NULL,
    edit_revision INTEGER NOT NULL,
    editor_value JSONB NOT NULL,
    prose TEXT NOT NULL,
    archived_by UUID,
    generation_job_id UUID,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, deal_id) REFERENCES public.deals(tenant_id, id)
);
CREATE INDEX ix_paper_edit_history_section
    ON public.paper_section_edit_history(tenant_id, deal_id, section, archived_at DESC);
ALTER TABLE public.paper_section_edit_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.paper_section_edit_history FORCE ROW LEVEL SECURITY;
CREATE POLICY paper_edit_history_tenant ON public.paper_section_edit_history
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
REVOKE ALL ON public.paper_section_edit_history FROM PUBLIC;

CREATE TABLE public.paper_comment_threads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    deal_id TEXT NOT NULL,
    section_key TEXT NOT NULL,
    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    resolved_by UUID,
    resolved_at TIMESTAMPTZ,
    FOREIGN KEY (tenant_id, deal_id) REFERENCES public.deals(tenant_id, id),
    UNIQUE (tenant_id, id),
    CHECK ((resolved_at IS NULL) = (resolved_by IS NULL))
);
CREATE INDEX ix_paper_comment_threads_deal
    ON public.paper_comment_threads(tenant_id, deal_id, created_at);
CREATE TABLE public.paper_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    thread_id UUID NOT NULL,
    author_id UUID,
    body TEXT NOT NULL CHECK (length(btrim(body)) BETWEEN 1 AND 10000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, thread_id) REFERENCES public.paper_comment_threads(tenant_id, id)
);
CREATE INDEX ix_paper_comments_thread
    ON public.paper_comments(tenant_id, thread_id, created_at);
ALTER TABLE public.paper_comment_threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.paper_comment_threads FORCE ROW LEVEL SECURITY;
CREATE POLICY paper_comment_threads_tenant ON public.paper_comment_threads
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE public.paper_comments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.paper_comments FORCE ROW LEVEL SECURITY;
CREATE POLICY paper_comments_tenant ON public.paper_comments
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
REVOKE ALL ON public.paper_comment_threads, public.paper_comments FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.get_paper_generation_context(
    p_tenant_id UUID, p_deal_id TEXT
) RETURNS JSONB LANGUAGE SQL STABLE SECURITY INVOKER
SET search_path = public, pg_temp AS $$
    SELECT jsonb_build_object(
        'guardrail_snapshot', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'id', r.id, 'kind', r.kind, 'payload', r.payload,
                'severity', r.severity, 'weight', r.weight,
                'scope_type', r.scope_type, 'scope_value', r.scope_value
            ) ORDER BY r.created_at, r.id)
            FROM public.guardrail_rules r
            WHERE r.tenant_id = p_tenant_id AND r.is_active
              AND (r.scope_type = 'global' OR (r.scope_type = 'module' AND r.scope_value = 'deals'))
        ), '[]'::jsonb)
    )
    FROM public.deals d
    WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id;
$$;

CREATE OR REPLACE FUNCTION public.archive_paper_section_edit(
    p_tenant_id UUID, p_deal_id TEXT, p_section INTEGER,
    p_actor_id UUID, p_generation_job_id UUID
) RETURNS UUID LANGUAGE plpgsql SECURITY INVOKER
SET search_path = public, pg_temp AS $$
DECLARE saved_id UUID;
BEGIN
    INSERT INTO public.paper_section_edit_history
        (tenant_id, deal_id, section, edit_revision, editor_value, prose, archived_by, generation_job_id)
    SELECT p_tenant_id, p_deal_id, s.section, s.edit_revision, s.editor_value, s.prose,
           p_actor_id, p_generation_job_id
    FROM public.deal_paper_sections s
    WHERE s.deal_id = p_deal_id AND s.section = p_section AND s.editor_value IS NOT NULL
    RETURNING id INTO saved_id;
    RETURN saved_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.list_paper_section_edit_history(
    p_tenant_id UUID, p_deal_id TEXT, p_section INTEGER
) RETURNS JSONB LANGUAGE SQL STABLE SECURITY INVOKER
SET search_path = public, pg_temp AS $$
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'id', h.id, 'section', h.section, 'edit_revision', h.edit_revision,
        'editor_value', h.editor_value, 'prose', h.prose,
        'archived_by', h.archived_by, 'archived_at', h.archived_at
    ) ORDER BY h.archived_at DESC, h.id DESC), '[]'::jsonb)
    FROM public.paper_section_edit_history h
    JOIN public.deals d ON d.tenant_id = h.tenant_id AND d.id = h.deal_id
    WHERE h.tenant_id = p_tenant_id AND h.deal_id = p_deal_id AND h.section = p_section
      AND d.tenant_id = p_tenant_id;
$$;

CREATE OR REPLACE FUNCTION public.restore_paper_section_edit(
    p_tenant_id UUID, p_deal_id TEXT, p_section INTEGER, p_history_id UUID,
    p_expected_revision INTEGER, p_actor_id UUID
) RETURNS INTEGER LANGUAGE plpgsql SECURITY INVOKER
SET search_path = public, pg_temp AS $$
DECLARE new_revision INTEGER;
DECLARE current_state TEXT;
BEGIN
    SELECT d.current_state
      INTO current_state
      FROM public.deals d
     WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id
     FOR UPDATE;
    IF NOT FOUND OR p_actor_id IS NULL
       OR current_state IN ('APPROVED', 'ISSUED', 'ARCHIVED') THEN
        RETURN NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.paper_section_edit_history h
        WHERE h.tenant_id = p_tenant_id AND h.deal_id = p_deal_id
          AND h.section = p_section AND h.id = p_history_id
    ) OR NOT EXISTS (
        SELECT 1 FROM public.deal_paper_sections s
        WHERE s.deal_id = p_deal_id AND s.section = p_section
          AND s.edit_revision = p_expected_revision AND s.status = 'ready'
    ) THEN
        RETURN NULL;
    END IF;
    INSERT INTO public.paper_section_edit_history
        (tenant_id, deal_id, section, edit_revision, editor_value, prose, archived_by)
    SELECT p_tenant_id, p_deal_id, s.section, s.edit_revision, s.editor_value, s.prose, p_actor_id
    FROM public.deal_paper_sections s
    WHERE s.deal_id = p_deal_id AND s.section = p_section
      AND s.edit_revision = p_expected_revision AND s.status = 'ready'
      AND s.editor_value IS NOT NULL;
    UPDATE public.deal_paper_sections s
       SET editor_value = h.editor_value, prose = h.prose,
           edit_revision = s.edit_revision + 1, updated_at = clock_timestamp()
      FROM public.paper_section_edit_history h
     WHERE h.tenant_id = p_tenant_id AND h.deal_id = p_deal_id
       AND h.section = p_section AND h.id = p_history_id
       AND s.deal_id = p_deal_id AND s.section = p_section
       AND s.edit_revision = p_expected_revision AND s.status = 'ready'
    RETURNING s.edit_revision INTO new_revision;
    IF new_revision IS NULL THEN RETURN NULL; END IF;
    IF current_state = 'PAPER_GENERATED' THEN
        UPDATE public.deals AS d SET current_state = 'PAPER_EDITING',
            state_changed_at = clock_timestamp(), state_changed_by = p_actor_id,
            workspace_revision = d.workspace_revision + 1
        WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id;
        INSERT INTO public.deal_state_transitions
            (tenant_id, deal_id, from_state, to_state, reason, actor_id, created_at)
        VALUES (p_tenant_id, p_deal_id, 'PAPER_GENERATED', 'PAPER_EDITING',
                'Restored paper edit', p_actor_id, clock_timestamp());
        INSERT INTO public.deal_audit_events
            (tenant_id, deal_id, actor_id, action, entity_type, entity_id, event_metadata)
        VALUES (p_tenant_id, p_deal_id, p_actor_id, 'state.transition', 'deal', p_deal_id,
                jsonb_build_object('from', 'PAPER_GENERATED', 'to', 'PAPER_EDITING',
                                   'reason', 'Restored paper edit'));
    END IF;
    INSERT INTO public.deal_audit_events
        (tenant_id, deal_id, actor_id, action, entity_type, entity_id, event_metadata)
    VALUES (p_tenant_id, p_deal_id, p_actor_id, 'paper.section_restored', 'deal', p_deal_id,
            jsonb_build_object('section', p_section, 'history_id', p_history_id,
                               'edit_revision', new_revision));
    RETURN new_revision;
END;
$$;

CREATE OR REPLACE FUNCTION public.save_generated_paper_section(
    p_tenant_id UUID, p_deal_id TEXT, p_section INTEGER, p_status TEXT,
    p_prose TEXT, p_error TEXT, p_ledger_revision BIGINT,
    p_generation_job_id UUID, p_actor_id UUID, p_constitution_version TEXT, p_guardrail_snapshot JSONB
) RETURNS INTEGER LANGUAGE plpgsql SECURITY INVOKER
SET search_path = public, pg_temp AS $$
DECLARE new_revision INTEGER;
BEGIN
    PERFORM 1 FROM public.deals d
    WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF p_status = 'ready' THEN
        INSERT INTO public.paper_section_edit_history
            (tenant_id, deal_id, section, edit_revision, editor_value, prose, archived_by, generation_job_id)
        SELECT p_tenant_id, p_deal_id, s.section, s.edit_revision, s.editor_value, s.prose,
               p_actor_id, p_generation_job_id
        FROM public.deal_paper_sections s
        WHERE s.deal_id = p_deal_id AND s.section = p_section AND s.editor_value IS NOT NULL;
        INSERT INTO public.deal_paper_sections
            (deal_id, section, status, prose, ledger_revision, guardrail_snapshot,
             constitution_version, updated_at)
        VALUES (p_deal_id, p_section, p_status, p_prose, p_ledger_revision,
                COALESCE(p_guardrail_snapshot, '{}'::jsonb), p_constitution_version, clock_timestamp())
        ON CONFLICT (deal_id, section) DO UPDATE SET
            status = EXCLUDED.status,
            prose = EXCLUDED.prose,
            editor_value = NULL,
            edit_revision = deal_paper_sections.edit_revision + 1,
            ledger_revision = EXCLUDED.ledger_revision,
            guardrail_snapshot = EXCLUDED.guardrail_snapshot,
            constitution_version = EXCLUDED.constitution_version,
            error = NULL,
            updated_at = clock_timestamp()
        RETURNING edit_revision INTO new_revision;
    ELSE
        INSERT INTO public.deal_paper_sections
            (deal_id, section, status, error, ledger_revision, updated_at)
        VALUES (p_deal_id, p_section, p_status, p_error, p_ledger_revision, clock_timestamp())
        ON CONFLICT (deal_id, section) DO UPDATE SET
            status = EXCLUDED.status,
            error = EXCLUDED.error,
            ledger_revision = EXCLUDED.ledger_revision,
            updated_at = clock_timestamp()
        RETURNING edit_revision INTO new_revision;
    END IF;
    RETURN new_revision;
END;
$$;

CREATE OR REPLACE FUNCTION public.complete_paper_generation_stage(
    p_tenant_id UUID, p_deal_id TEXT, p_actor_id UUID, p_required_sections INTEGER[]
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY INVOKER
SET search_path = public, pg_temp AS $$
DECLARE from_state TEXT;
DECLARE target_state TEXT;
BEGIN
    SELECT current_state INTO from_state FROM public.deals
    WHERE tenant_id = p_tenant_id AND id = p_deal_id FOR UPDATE;
    IF NOT FOUND OR p_actor_id IS NULL OR from_state NOT IN ('DRAFT', 'INTAKE', 'PROCESSING', 'FACT_REVIEW') THEN
        RETURN FALSE;
    END IF;
    IF COALESCE(cardinality(p_required_sections), 0) = 0 OR EXISTS (
        SELECT 1 FROM unnest(p_required_sections) required(section)
        WHERE NOT EXISTS (
            SELECT 1 FROM public.deal_paper_sections s
            WHERE s.deal_id = p_deal_id AND s.section = required.section
              AND s.status = 'ready'
              AND s.ledger_revision = (SELECT ledger_revision FROM public.deals
                                       WHERE tenant_id = p_tenant_id AND id = p_deal_id)
        )
    ) THEN
        RETURN FALSE;
    END IF;
    FOR target_state IN
        SELECT step FROM unnest(CASE from_state
            WHEN 'DRAFT' THEN ARRAY['INTAKE', 'PROCESSING', 'FACT_REVIEW', 'PAPER_GENERATED']
            WHEN 'INTAKE' THEN ARRAY['PROCESSING', 'FACT_REVIEW', 'PAPER_GENERATED']
            WHEN 'PROCESSING' THEN ARRAY['FACT_REVIEW', 'PAPER_GENERATED']
            ELSE ARRAY['PAPER_GENERATED'] END) AS steps(step)
    LOOP
        UPDATE public.deals SET current_state = target_state,
            state_changed_at = clock_timestamp(), state_changed_by = p_actor_id,
            workspace_revision = workspace_revision + 1
        WHERE tenant_id = p_tenant_id AND id = p_deal_id;
        INSERT INTO public.deal_state_transitions
            (tenant_id, deal_id, from_state, to_state, reason, actor_id, created_at)
        VALUES (p_tenant_id, p_deal_id, from_state, target_state,
                'Paper generation completed', p_actor_id, clock_timestamp());
        INSERT INTO public.deal_audit_events
            (tenant_id, deal_id, actor_id, action, entity_type, entity_id, event_metadata)
        VALUES (p_tenant_id, p_deal_id, p_actor_id, 'state.transition', 'deal', p_deal_id,
                jsonb_build_object('from', from_state, 'to', target_state,
                                   'reason', 'Paper generation completed'));
        from_state := target_state;
    END LOOP;
    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION public.save_edited_paper_section(
    p_tenant_id UUID, p_deal_id TEXT, p_section INTEGER, p_expected_revision INTEGER,
    p_editor_value JSONB, p_prose TEXT, p_actor_id UUID
) RETURNS INTEGER LANGUAGE plpgsql SECURITY INVOKER
SET search_path = public, pg_temp AS $$
DECLARE new_revision INTEGER;
DECLARE current_state TEXT;
DECLARE next_revision INTEGER;
BEGIN
    SELECT d.current_state, d.workspace_revision INTO current_state, next_revision
    FROM public.deals d WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id FOR UPDATE;
    IF NOT FOUND OR current_state IN ('APPROVED', 'ISSUED', 'ARCHIVED') THEN RETURN NULL; END IF;
    UPDATE public.deal_paper_sections s
       SET prose = p_prose, editor_value = p_editor_value,
           edit_revision = s.edit_revision + 1, updated_at = clock_timestamp()
     WHERE s.deal_id = p_deal_id AND s.section = p_section
       AND s.edit_revision = p_expected_revision AND s.status = 'ready'
    RETURNING edit_revision INTO new_revision;
    IF new_revision IS NULL THEN RETURN NULL; END IF;
    IF current_state = 'PAPER_GENERATED' THEN
        next_revision := next_revision + 1;
        UPDATE public.deals SET current_state = 'PAPER_EDITING',
            state_changed_at = clock_timestamp(), state_changed_by = p_actor_id,
            workspace_revision = next_revision
        WHERE tenant_id = p_tenant_id AND id = p_deal_id;
        INSERT INTO public.deal_state_transitions
            (tenant_id, deal_id, from_state, to_state, reason, actor_id, created_at)
        VALUES (p_tenant_id, p_deal_id, 'PAPER_GENERATED', 'PAPER_EDITING',
                'First paper edit', p_actor_id, clock_timestamp());
        INSERT INTO public.deal_audit_events
            (tenant_id, deal_id, actor_id, action, entity_type, entity_id, event_metadata)
        VALUES (p_tenant_id, p_deal_id, p_actor_id, 'state.transition', 'deal', p_deal_id,
                jsonb_build_object('from', 'PAPER_GENERATED', 'to', 'PAPER_EDITING',
                                   'reason', 'First paper edit'));
    END IF;
    INSERT INTO public.deal_audit_events
        (tenant_id, deal_id, actor_id, action, entity_type, entity_id, event_metadata)
    VALUES (p_tenant_id, p_deal_id, p_actor_id, 'paper.section_edited', 'deal', p_deal_id,
            jsonb_build_object('section', p_section, 'edit_revision', new_revision));
    RETURN new_revision;
END;
$$;

CREATE OR REPLACE FUNCTION public.record_paper_guardrail_evaluation(
    p_tenant_id UUID, p_rule_id UUID, p_section INTEGER, p_result TEXT, p_measured JSONB
) RETURNS UUID LANGUAGE plpgsql SECURITY INVOKER
SET search_path = public, pg_temp AS $$
DECLARE evaluation_id UUID;
BEGIN
    INSERT INTO public.guardrail_evaluations
        (id, tenant_id, rule_id, subject_type, subject_id, result, measured)
    VALUES (gen_random_uuid(), p_tenant_id, p_rule_id, 'section', p_section::text,
            p_result, COALESCE(p_measured, '{}'::jsonb))
    RETURNING id INTO evaluation_id;
    RETURN evaluation_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_approval_guardrail_snapshots(
    p_tenant_id UUID, p_deal_id TEXT, p_sections INTEGER[]
) RETURNS JSONB LANGUAGE SQL STABLE SECURITY INVOKER
SET search_path = public, pg_temp AS $$
    SELECT jsonb_build_object(
        'sections', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'section', s.section,
                        'rules', s.guardrail_snapshot->'rules',
                'evaluations', s.guardrail_snapshot->'evaluations',
                'signoffs', s.guardrail_snapshot->'signoffs',
                'constitution_version', s.constitution_version
            ) ORDER BY s.section)
            FROM public.deal_paper_sections s
            WHERE s.deal_id = p_deal_id AND s.status = 'ready'
              AND s.section = ANY(COALESCE(p_sections, ARRAY[]::integer[]))
        ), '[]'::jsonb),
        'blocking_items', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'section', s.section,
                'rule_id', evaluation->>'rule_id',
                'kind', evaluation->>'kind',
                'result', evaluation->>'result',
                'measured', evaluation->'measured'
            ) ORDER BY s.section, evaluation->>'rule_id')
            FROM public.deal_paper_sections s
            CROSS JOIN LATERAL jsonb_array_elements(
                COALESCE(s.guardrail_snapshot->'evaluations', '[]'::jsonb)
            ) evaluation
            WHERE s.deal_id = p_deal_id AND s.status = 'ready'
              AND s.section = ANY(COALESCE(p_sections, ARRAY[]::integer[]))
              AND evaluation->>'severity' = 'block'
              AND evaluation->>'result' IN ('flagged', 'error')
              AND NOT EXISTS (
                  SELECT 1 FROM jsonb_array_elements(
                      COALESCE(s.guardrail_snapshot->'signoffs', '[]'::jsonb)
                  ) signoff
                  WHERE signoff->>'rule_id' = evaluation->>'rule_id'
                    AND signoff->>'result' = evaluation->>'result'
              )
        ), '[]'::jsonb),
        'signoffs', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'section', s.section, 'signoff', signoff
            ) ORDER BY s.section, signoff->>'signed_at')
            FROM public.deal_paper_sections s
            CROSS JOIN LATERAL jsonb_array_elements(
                COALESCE(s.guardrail_snapshot->'signoffs', '[]'::jsonb)
            ) signoff
            WHERE s.deal_id = p_deal_id AND s.status = 'ready'
              AND s.section = ANY(COALESCE(p_sections, ARRAY[]::integer[]))
        ), '[]'::jsonb)
    )
    FROM public.deals d
    WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id;
$$;

CREATE OR REPLACE FUNCTION public.get_paper_comment_threads(
    p_tenant_id UUID, p_deal_id TEXT, p_include_resolved BOOLEAN DEFAULT TRUE
) RETURNS JSONB LANGUAGE SQL STABLE SECURITY INVOKER
SET search_path = public, pg_temp AS $$
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'id', t.id, 'section_key', t.section_key, 'created_by', t.created_by,
        'created_at', t.created_at, 'resolved_by', t.resolved_by, 'resolved_at', t.resolved_at,
        'comments', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'id', c.id, 'author_id', c.author_id, 'author_name',
                COALESCE(NULLIF(trim(concat_ws(' ', u.first_name, u.last_name)), ''),
                         NULLIF(split_part(u.email, '@', 1), ''), 'Former member'),
                'body', c.body, 'created_at', c.created_at
            ) ORDER BY c.created_at, c.id)
            FROM public.paper_comments c
            LEFT JOIN public.users u ON u.id = c.author_id::text AND u.domain_id = p_tenant_id::text
            WHERE c.tenant_id = t.tenant_id AND c.thread_id = t.id
        ), '[]'::jsonb)
    ) ORDER BY t.created_at, t.id), '[]'::jsonb)
    FROM public.paper_comment_threads t
    JOIN public.deals d ON d.tenant_id = t.tenant_id AND d.id = t.deal_id
    WHERE t.tenant_id = p_tenant_id AND t.deal_id = p_deal_id
      AND (p_include_resolved OR t.resolved_at IS NULL);
$$;

CREATE OR REPLACE FUNCTION public.record_guardrail_signoff(
    p_tenant_id UUID, p_deal_id TEXT, p_section INTEGER, p_rule_id UUID,
    p_result TEXT, p_actor_id UUID, p_reason TEXT
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY INVOKER
SET search_path = public, pg_temp AS $$
DECLARE current_state TEXT;
BEGIN
    SELECT d.current_state INTO current_state
    FROM public.deals d
    WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id
    FOR UPDATE;
    IF NOT FOUND OR current_state IN ('APPROVED', 'ISSUED', 'ARCHIVED')
       OR p_actor_id IS NULL
       OR length(btrim(COALESCE(p_reason, ''))) NOT BETWEEN 1 AND 2000 THEN
        RETURN FALSE;
    END IF;
    UPDATE public.deal_paper_sections s SET guardrail_snapshot = jsonb_set(
        s.guardrail_snapshot,
        '{signoffs}',
        (CASE WHEN jsonb_typeof(s.guardrail_snapshot->'signoffs') = 'array'
              THEN s.guardrail_snapshot->'signoffs' ELSE '[]'::jsonb END) || jsonb_build_array(
            jsonb_build_object('rule_id', p_rule_id, 'result', p_result, 'actor_id', p_actor_id,
                               'reason', p_reason, 'signed_at', clock_timestamp())
        ),
        true
    )
    WHERE s.deal_id = p_deal_id AND s.section = p_section
      AND EXISTS (SELECT 1 FROM public.deals d
                  WHERE d.tenant_id = p_tenant_id AND d.id = s.deal_id)
      AND CASE
          WHEN s.guardrail_snapshot->'evaluations' IS NULL THEN TRUE
          WHEN jsonb_typeof(s.guardrail_snapshot->'evaluations') = 'array' THEN TRUE
          ELSE FALSE
      END
      AND EXISTS (
          SELECT 1 FROM jsonb_array_elements(
              COALESCE(s.guardrail_snapshot->'evaluations', '[]'::jsonb)
          ) evaluation
          WHERE evaluation->>'rule_id' = p_rule_id::text
            AND evaluation->>'severity' = 'block'
            AND evaluation->>'result' = p_result
            AND p_result IN ('flagged', 'error')
      );
    IF FOUND THEN
        INSERT INTO public.deal_audit_events
            (tenant_id, deal_id, actor_id, action, entity_type, entity_id, event_metadata)
        VALUES (p_tenant_id, p_deal_id, p_actor_id, 'paper.guardrail_signed_off', 'deal', p_deal_id,
                jsonb_build_object('section', p_section, 'rule_id', p_rule_id,
                                   'result', p_result, 'reason', p_reason));
    END IF;
    RETURN FOUND;
END;
$$;

CREATE OR REPLACE FUNCTION public.add_paper_comment(
    p_tenant_id UUID, p_deal_id TEXT, p_section_key TEXT, p_actor_id UUID,
    p_body TEXT, p_thread_id UUID DEFAULT NULL
) RETURNS UUID LANGUAGE plpgsql SECURITY INVOKER
SET search_path = public, pg_temp AS $$
DECLARE target_thread UUID;
DECLARE current_state TEXT;
BEGIN
    SELECT d.current_state INTO current_state FROM public.deals d
    WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id FOR UPDATE;
    IF NOT FOUND OR current_state IN ('APPROVED', 'ISSUED', 'ARCHIVED') THEN RETURN NULL; END IF;
    IF p_thread_id IS NULL THEN
        INSERT INTO public.paper_comment_threads(tenant_id, deal_id, section_key, created_by)
        SELECT p_tenant_id, d.id, p_section_key, p_actor_id
        FROM public.deals d
        WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id
          AND EXISTS (
              SELECT 1 FROM jsonb_array_elements(
                  CASE WHEN jsonb_typeof(d.template_snapshot->'sections') = 'array'
                       THEN d.template_snapshot->'sections' ELSE '[]'::jsonb END
              ) section
              WHERE section->>'key' = p_section_key
          )
        RETURNING id INTO target_thread;
        IF target_thread IS NULL THEN RETURN NULL; END IF;
    ELSE
        SELECT t.id INTO target_thread FROM public.paper_comment_threads t
        WHERE t.tenant_id = p_tenant_id AND t.deal_id = p_deal_id
          AND t.section_key = p_section_key AND t.id = p_thread_id AND t.resolved_at IS NULL
        FOR UPDATE;
        IF target_thread IS NULL THEN RETURN NULL; END IF;
    END IF;
    INSERT INTO public.paper_comments(tenant_id, thread_id, author_id, body)
    VALUES (p_tenant_id, target_thread, p_actor_id, p_body);
    RETURN target_thread;
END;
$$;

CREATE OR REPLACE FUNCTION public.resolve_paper_comment_thread(
    p_tenant_id UUID, p_deal_id TEXT, p_thread_id UUID, p_actor_id UUID
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY INVOKER
SET search_path = public, pg_temp AS $$
BEGIN
    UPDATE public.paper_comment_threads SET resolved_by = p_actor_id, resolved_at = clock_timestamp()
    WHERE tenant_id = p_tenant_id AND deal_id = p_deal_id
      AND id = p_thread_id AND resolved_at IS NULL
      AND EXISTS (SELECT 1 FROM public.deals d
                  WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id
                    AND d.current_state NOT IN ('APPROVED', 'ISSUED', 'ARCHIVED'));
    RETURN FOUND;
END;
$$;

CREATE OR REPLACE FUNCTION public.record_paper_approved_event(
    p_tenant_id UUID, p_deal_id TEXT, p_metadata JSONB DEFAULT NULL
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY INVOKER
SET search_path = public, pg_temp AS $$
BEGIN
    INSERT INTO public.billable_events(id, tenant_id, deal_id, event_type, metadata)
    VALUES (gen_random_uuid(), p_tenant_id, p_deal_id, 'paper_first_approved_render', p_metadata)
    ON CONFLICT ON CONSTRAINT uq_billable_once_per_deal DO NOTHING;
    RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION public.get_paper_generation_context(UUID, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.save_generated_paper_section(UUID, TEXT, INTEGER, TEXT, TEXT, TEXT, BIGINT, UUID, UUID, TEXT, JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.save_edited_paper_section(UUID, TEXT, INTEGER, INTEGER, JSONB, TEXT, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.archive_paper_section_edit(UUID, TEXT, INTEGER, UUID, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.list_paper_section_edit_history(UUID, TEXT, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.restore_paper_section_edit(UUID, TEXT, INTEGER, UUID, INTEGER, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.record_paper_guardrail_evaluation(UUID, UUID, INTEGER, TEXT, JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_approval_guardrail_snapshots(UUID, TEXT, INTEGER[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.record_guardrail_signoff(UUID, TEXT, INTEGER, UUID, TEXT, UUID, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.complete_paper_generation_stage(UUID, TEXT, UUID, INTEGER[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_paper_comment_threads(UUID, TEXT, BOOLEAN) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.add_paper_comment(UUID, TEXT, TEXT, UUID, TEXT, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.resolve_paper_comment_thread(UUID, TEXT, UUID, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.record_paper_approved_event(UUID, TEXT, JSONB) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
        GRANT SELECT, INSERT ON public.paper_section_edit_history TO creditpaper_runtime;
        GRANT SELECT, INSERT, UPDATE ON public.paper_comment_threads, public.paper_comments TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.get_paper_generation_context(UUID, TEXT) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.save_generated_paper_section(UUID, TEXT, INTEGER, TEXT, TEXT, TEXT, BIGINT, UUID, UUID, TEXT, JSONB) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.save_edited_paper_section(UUID, TEXT, INTEGER, INTEGER, JSONB, TEXT, UUID) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.complete_paper_generation_stage(UUID, TEXT, UUID, INTEGER[]) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.archive_paper_section_edit(UUID, TEXT, INTEGER, UUID, UUID) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.list_paper_section_edit_history(UUID, TEXT, INTEGER) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.restore_paper_section_edit(UUID, TEXT, INTEGER, UUID, INTEGER, UUID) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.record_paper_guardrail_evaluation(UUID, UUID, INTEGER, TEXT, JSONB) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.get_approval_guardrail_snapshots(UUID, TEXT, INTEGER[]) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.record_guardrail_signoff(UUID, TEXT, INTEGER, UUID, TEXT, UUID, TEXT) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.get_paper_comment_threads(UUID, TEXT, BOOLEAN) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.add_paper_comment(UUID, TEXT, TEXT, UUID, TEXT, UUID) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.resolve_paper_comment_thread(UUID, TEXT, UUID, UUID) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.record_paper_approved_event(UUID, TEXT, JSONB) TO creditpaper_runtime;
    END IF;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.record_paper_approved_event(UUID, TEXT, JSONB)")
    op.execute("DROP FUNCTION IF EXISTS public.resolve_paper_comment_thread(UUID, TEXT, UUID, UUID)")
    op.execute("DROP FUNCTION IF EXISTS public.add_paper_comment(UUID, TEXT, TEXT, UUID, TEXT, UUID)")
    op.execute("DROP FUNCTION IF EXISTS public.get_paper_comment_threads(UUID, TEXT, BOOLEAN)")
    op.execute(
        "DROP FUNCTION IF EXISTS public.record_guardrail_signoff(UUID, TEXT, INTEGER, UUID, TEXT, UUID, TEXT)"
    )
    op.execute("DROP FUNCTION IF EXISTS public.get_approval_guardrail_snapshots(UUID, TEXT, INTEGER[])")
    op.execute(
        "DROP FUNCTION IF EXISTS public.record_paper_guardrail_evaluation(UUID, UUID, INTEGER, TEXT, JSONB)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.save_edited_paper_section(UUID, TEXT, INTEGER, INTEGER, JSONB, TEXT, UUID)"
    )
    op.execute("DROP FUNCTION IF EXISTS public.complete_paper_generation_stage(UUID, TEXT, UUID, INTEGER[])")
    op.execute(
        "DROP FUNCTION IF EXISTS public.save_generated_paper_section(UUID, TEXT, INTEGER, TEXT, TEXT, TEXT, BIGINT, UUID, UUID, TEXT, JSONB)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.restore_paper_section_edit(UUID, TEXT, INTEGER, UUID, INTEGER, UUID)"
    )
    op.execute("DROP FUNCTION IF EXISTS public.list_paper_section_edit_history(UUID, TEXT, INTEGER)")
    op.execute("DROP FUNCTION IF EXISTS public.archive_paper_section_edit(UUID, TEXT, INTEGER, UUID, UUID)")
    op.execute("DROP FUNCTION IF EXISTS public.get_paper_generation_context(UUID, TEXT)")
    op.execute("DROP TABLE IF EXISTS public.paper_comments")
    op.execute("DROP TABLE IF EXISTS public.paper_comment_threads")
    op.execute("DROP TABLE IF EXISTS public.paper_section_edit_history")
    op.execute(
        "ALTER TABLE public.deal_paper_sections DROP COLUMN guardrail_snapshot, DROP COLUMN constitution_version"
    )
