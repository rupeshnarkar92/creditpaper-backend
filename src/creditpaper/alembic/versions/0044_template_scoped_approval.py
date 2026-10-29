"""Read the deal's template and paper rows in one approval query.

Approval readiness is scoped to engine sections represented by the deal's frozen
template snapshot, not the global nine-section engine registry.
"""

from alembic import op

revision = "0044_template_scoped_approval"
down_revision = "0043_ingestion_data"
branch_labels = None
depends_on = None


_APPROVAL_PAPER_DATA_FN = """
CREATE OR REPLACE FUNCTION public.get_approval_paper_data(
    p_tenant_id UUID,
    p_deal_id TEXT,
    p_actor_id UUID
) RETURNS JSONB
LANGUAGE SQL
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    SELECT jsonb_build_object(
        'name', d.name,
        'template_id', d.template_id,
        'template_source', d.template_source,
        'template_snapshot', d.template_snapshot,
        'profile', jsonb_build_object(
            'brand_profile', (
                SELECT bp.profile || jsonb_build_object('updated_at', bp.updated_at)
                FROM public.brand_profile bp
                WHERE bp.tenant_id = p_tenant_id
            ),
            'voice_profile', (
                SELECT vp.profile
                FROM public.voice_profile vp
                WHERE vp.tenant_id = p_tenant_id
            ),
            'section_orders', (
                SELECT so.sections
                FROM public.section_orders so
                WHERE so.tenant_id = p_tenant_id
            ),
            'firm_name', (
                SELECT domain.company
                FROM public.domains domain
                WHERE domain.id::text = p_tenant_id::text
            )
        ),
        'actor_name', COALESCE((
            SELECT COALESCE(
                NULLIF(trim(concat_ws(' ', u.first_name, u.last_name)), ''),
                NULLIF(split_part(u.email, '@', 1), ''),
                'Former member'
            )
            FROM public.users u
            WHERE u.id = p_actor_id::text AND u.domain_id = p_tenant_id::text
        ), 'Former member'),
        'recommendation', (
            SELECT jsonb_build_object(
                'id', r.id,
                'author_id', r.author_id,
                'author_name', COALESCE(
                    NULLIF(trim(concat_ws(' ', u.first_name, u.last_name)), ''),
                    NULLIF(split_part(u.email, '@', 1), ''),
                    'Former member'
                ),
                'signed_at', r.signed_at,
                'body', r.body
            )
            FROM public.deal_recommendations r
            LEFT JOIN public.users u
              ON u.id = r.author_id::text AND u.domain_id = p_tenant_id::text
            WHERE r.tenant_id = p_tenant_id AND r.deal_id = p_deal_id
            ORDER BY r.signed_at DESC, r.id DESC
            LIMIT 1
        ),
        'theme_default', (
            SELECT jsonb_build_object('version', pd.version, 'payload', pd.payload)
            FROM public.platform_defaults pd
            WHERE pd.kind = 'paper_theme' AND pd.is_current = true
            LIMIT 1
        ),
        'firm_default', (
            SELECT jsonb_build_object(
                'id', t.id,
                'name', t.name,
                'source', t.source,
                'updated_at', t.updated_at,
                'sections', t.sections
            )
            FROM public.templates t
            WHERE t.tenant_id = p_tenant_id
              AND t.is_default = true
              AND t.is_active = true
              AND jsonb_array_length(
                  CASE
                      WHEN jsonb_typeof(t.sections) = 'array' THEN t.sections
                      ELSE '[]'::jsonb
                  END
              ) > 0
            ORDER BY t.updated_at DESC, t.id
            LIMIT 1
        ),
        'platform_default', (
            SELECT jsonb_build_object('version', pd.version, 'payload', pd.payload)
            FROM public.platform_defaults pd
            WHERE pd.kind = 'template' AND pd.is_current = true
            LIMIT 1
        ),
        'sections', COALESCE((
            SELECT jsonb_agg(jsonb_build_array(
                s.section,
                s.status,
                s.prose,
                s.citations,
                s.ledger_revision,
                s.editor_value,
                s.edit_revision
            ) ORDER BY s.section)
            FROM public.deal_paper_sections s
            WHERE s.deal_id = d.id
              AND EXISTS (
                  SELECT 1
                  FROM public.deals owned_deal
                  WHERE owned_deal.tenant_id = p_tenant_id
                    AND owned_deal.id = s.deal_id
              )
        ), '[]'::jsonb)
    )
    FROM public.deals d
    WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id;
$$;
"""

_FINAL_PAPER_SIGNER_NAMES_FN = """
CREATE OR REPLACE FUNCTION public.get_final_paper_signer_names(
    p_tenant_id UUID,
    p_user_ids UUID[]
) RETURNS JSONB
LANGUAGE SQL
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    SELECT COALESCE(jsonb_object_agg(
        u.id,
        COALESCE(
            NULLIF(trim(concat_ws(' ', u.first_name, u.last_name)), ''),
            NULLIF(split_part(u.email, '@', 1), ''),
            'Former member'
        )
    ), '{}'::jsonb)
    FROM public.users u
    WHERE u.domain_id = p_tenant_id::text
      AND u.id::uuid = ANY(COALESCE(p_user_ids, ARRAY[]::uuid[]));
$$;
"""

_PRIVILEGES = """
REVOKE ALL ON FUNCTION public.get_approval_paper_data(UUID, TEXT, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_final_paper_signer_names(UUID, UUID[]) FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.get_approval_paper_data(UUID, TEXT, UUID) FROM '
                || quote_ident(role_name);
            EXECUTE 'REVOKE ALL ON FUNCTION public.get_final_paper_signer_names(UUID, UUID[]) FROM '
                || quote_ident(role_name);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
        GRANT EXECUTE ON FUNCTION public.get_approval_paper_data(UUID, TEXT, UUID) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.get_final_paper_signer_names(UUID, UUID[]) TO creditpaper_runtime;
    END IF;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_APPROVAL_PAPER_DATA_FN)
    op.execute(_FINAL_PAPER_SIGNER_NAMES_FN)
    op.execute(_PRIVILEGES)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.get_final_paper_signer_names(UUID, UUID[])")
    op.execute("DROP FUNCTION IF EXISTS public.get_approval_paper_data(UUID, TEXT, UUID)")
