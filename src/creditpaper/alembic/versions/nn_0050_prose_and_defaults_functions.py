"""Move prose persistence and platform-default reads behind database functions."""

from alembic import op

revision = "nn_0050_prose_defaults"
down_revision = "nn_0049_billing_stored_functions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.get_current_platform_default(p_kind text)
        RETURNS TABLE (id uuid, kind text, version integer, payload jsonb, created_at timestamptz)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT d.id, d.kind, d.version, d.payload, d.created_at
            FROM public.platform_defaults d
            WHERE d.kind = p_kind AND d.is_current = true LIMIT 1
        $$;

        CREATE OR REPLACE FUNCTION public.list_paper_prose_deal_ids(p_tenant_id uuid)
        RETURNS SETOF text LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path = pg_catalog, public AS $$
            SELECT p.deal_id FROM public.paper_prose p
            WHERE p.tenant_id = p_tenant_id
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            ORDER BY p.deal_id
        $$;

        CREATE OR REPLACE FUNCTION public.get_paper_prose(p_tenant_id uuid, p_deal_id text)
        RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path = pg_catalog, public AS $$
            SELECT p.prose FROM public.paper_prose p
            WHERE p.tenant_id = p_tenant_id AND p.deal_id = p_deal_id
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        $$;

        CREATE OR REPLACE FUNCTION public.save_paper_prose(
            p_tenant_id uuid, p_deal_id text, p_prose jsonb
        ) RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER
        SET search_path = pg_catalog, public AS $$
        DECLARE v_changed integer;
        BEGIN
            IF p_tenant_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id', true), '')::uuid THEN
                RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.paper_prose AS p (deal_id, tenant_id, prose, updated_at)
            VALUES (p_deal_id, p_tenant_id, p_prose, now())
            ON CONFLICT (deal_id) DO UPDATE SET prose = EXCLUDED.prose, updated_at = now()
            WHERE p.tenant_id = EXCLUDED.tenant_id;
            GET DIAGNOSTICS v_changed = ROW_COUNT;
            RETURN v_changed = 1;
        END $$;

        COMMENT ON FUNCTION public.get_current_platform_default(text) IS
            'Returns current platform default for an allowlisted application kind. Parameter: kind. Return: zero or one default row.';
        COMMENT ON FUNCTION public.list_paper_prose_deal_ids(uuid) IS
            'Lists prose-bearing deal IDs for the transaction tenant. Parameter: tenant UUID. Return: ordered text rows.';
        COMMENT ON FUNCTION public.get_paper_prose(uuid,text) IS
            'Reads generated prose for one deal in the transaction tenant. Return: prose JSONB or NULL.';
        COMMENT ON FUNCTION public.save_paper_prose(uuid,text,jsonb) IS
            'Upserts generated prose without permitting cross-tenant replacement. Return: true when saved.';

        ALTER TABLE public.paper_prose ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.paper_prose FORCE ROW LEVEL SECURITY;
        CREATE POLICY paper_prose_tenant ON public.paper_prose
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

        REVOKE ALL ON FUNCTION public.get_current_platform_default(text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.list_paper_prose_deal_ids(uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.get_paper_prose(uuid,text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.save_paper_prose(uuid,text,jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.get_current_platform_default(text),
            public.list_paper_prose_deal_ids(uuid), public.get_paper_prose(uuid,text),
            public.save_paper_prose(uuid,text,jsonb) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    raise RuntimeError("Do not restore direct prose/default table access; restore a verified backup")
