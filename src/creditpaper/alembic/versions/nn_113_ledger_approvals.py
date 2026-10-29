"""Move ledger approval persistence and lookup behind stored functions."""

from alembic import op

revision = "nn_113_ledger_approvals"
down_revision = "nn_112_ledger_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION public.create_ledger_approval(p_tenant_id uuid,p_deal_id text,p_actor_id uuid,p_valid_at timestamptz,p_known_at timestamptz,p_revision integer,p_manifest jsonb,p_digest text) RETURNS uuid LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      INSERT INTO public.ledger_approvals(tenant_id,deal_id,actor_id,valid_at,known_at,ledger_revision,manifest,digest)
      VALUES(p_tenant_id,p_deal_id,p_actor_id,p_valid_at,p_known_at,p_revision,p_manifest,p_digest) RETURNING id
    $$;
    CREATE OR REPLACE FUNCTION public.ledger_approval_row(p_tenant_id uuid,p_deal_id text,p_id uuid)
    RETURNS TABLE(manifest jsonb,valid_at timestamptz,known_at timestamptz,digest text)
    LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      SELECT manifest,valid_at,known_at,digest FROM public.ledger_approvals
      WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND id=p_id
    $$;
    REVOKE ALL ON FUNCTION public.create_ledger_approval(uuid,text,uuid,timestamptz,timestamptz,integer,jsonb,text),public.ledger_approval_row(uuid,text,uuid) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.create_ledger_approval(uuid,text,uuid,timestamptz,timestamptz,integer,jsonb,text),public.ledger_approval_row(uuid,text,uuid) TO creditpaper_runtime;
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.create_ledger_approval(uuid,text,uuid,timestamptz,timestamptz,integer,jsonb,text),public.ledger_approval_row(uuid,text,uuid)"
    )
