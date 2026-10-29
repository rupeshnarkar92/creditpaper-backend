"""Force tenant isolation for guardrail rules and evaluations."""

from alembic import op

revision = "nn_124_guardrail_rls"
down_revision = "nn_123_integration_writes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE public.guardrail_rules ENABLE ROW LEVEL SECURITY;
    ALTER TABLE public.guardrail_rules FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS guardrail_rules_tenant ON public.guardrail_rules;
    CREATE POLICY guardrail_rules_tenant ON public.guardrail_rules
      USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE public.guardrail_evaluations ENABLE ROW LEVEL SECURITY;
    ALTER TABLE public.guardrail_evaluations FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS guardrail_evaluations_tenant ON public.guardrail_evaluations;
    CREATE POLICY guardrail_evaluations_tenant ON public.guardrail_evaluations
      USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

    COMMENT ON POLICY guardrail_rules_tenant ON public.guardrail_rules IS
      'Restricts reads and writes to the transaction-local app.tenant_id.';
    COMMENT ON POLICY guardrail_evaluations_tenant ON public.guardrail_evaluations IS
      'Restricts reads and writes to the transaction-local app.tenant_id.';
    """)


def downgrade() -> None:
    # Deliberately irreversible: a rollback must not reopen tenant data.
    pass
