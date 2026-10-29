"""Reassert forced RLS for legacy billing and generated-prose tables.

These tables already use tenant-validating stored functions and tenant_cursor
callers. Some hosted databases were stamped past the original additive
migrations, so this idempotent migration closes that historical drift.
"""

from alembic import op

revision = "nn_134_complete_tenant_rls"
down_revision = "nn_133_auth_rbac_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tenant = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    for table in (
        "billing_accounts",
        "billing_invoices",
        "billable_events",
        "paper_prose",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant ON public.{table}")
        op.execute(f"""CREATE POLICY {table}_tenant ON public.{table}
            USING (tenant_id = {tenant}) WITH CHECK (tenant_id = {tenant})""")
        op.execute(f"""COMMENT ON POLICY {table}_tenant ON public.{table} IS
            'Reads and writes require transaction-local app.tenant_id.'""")


def downgrade() -> None:
    # Deliberately irreversible: rollback must not reopen tenant billing/prose.
    pass
