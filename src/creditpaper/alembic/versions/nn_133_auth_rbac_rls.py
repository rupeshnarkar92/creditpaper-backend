"""Ringfence tenant RBAC and authentication audit records.

Login and invitation routines execute before a request has resolved a tenant.
They are narrow SECURITY DEFINER exceptions; browser roles cannot execute them
and their existing bodies validate an authenticated identity or invitation.
Ordinary table access remains subject to transaction-local app.tenant_id RLS.
"""

from alembic import op

revision = "nn_133_auth_rbac_rls"
down_revision = "nn_132_deals_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tenant = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"

    # These retain the execute grants established by their original migrations.
    for signature in (
        "public.get_user_membership(TEXT)",
        "public.provision_tenant_for_user(TEXT, TEXT, TEXT)",
        "public.accept_invitation(TEXT, TEXT, TEXT)",
        "public.record_auth_event(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, TEXT)",
    ):
        op.execute(f"ALTER FUNCTION {signature} SECURITY DEFINER")

    op.execute("ALTER TABLE public.user_roles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.user_roles FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS user_roles_tenant ON public.user_roles")
    op.execute(f"""CREATE POLICY user_roles_tenant ON public.user_roles
        USING (tenant_id = {tenant}) WITH CHECK (tenant_id = {tenant})""")
    op.execute("""COMMENT ON POLICY user_roles_tenant ON public.user_roles IS
        'Membership reads and writes require transaction-local app.tenant_id.'""")

    op.execute("ALTER TABLE public.roles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.roles FORCE ROW LEVEL SECURITY")
    for policy in (
        "roles_tenant_read",
        "roles_tenant_insert",
        "roles_tenant_update",
        "roles_tenant_delete",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON public.roles")
    op.execute(f"""CREATE POLICY roles_tenant_read ON public.roles FOR SELECT
        USING (tenant_id = {tenant} OR (tenant_id IS NULL AND is_system IS TRUE))""")
    op.execute(f"""CREATE POLICY roles_tenant_insert ON public.roles FOR INSERT
        WITH CHECK (tenant_id = {tenant})""")
    op.execute(f"""CREATE POLICY roles_tenant_update ON public.roles FOR UPDATE
        USING (tenant_id = {tenant}) WITH CHECK (tenant_id = {tenant})""")
    op.execute(f"""CREATE POLICY roles_tenant_delete ON public.roles FOR DELETE
        USING (tenant_id = {tenant})""")
    op.execute("""COMMENT ON POLICY roles_tenant_read ON public.roles IS
        'Tenants can read their roles and immutable platform system roles.'""")

    op.execute("ALTER TABLE public.auth_audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.auth_audit_events FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS auth_audit_events_tenant_read ON public.auth_audit_events")
    op.execute(f"""CREATE POLICY auth_audit_events_tenant_read
        ON public.auth_audit_events FOR SELECT USING (tenant_id::text = ({tenant})::text)""")
    op.execute("""COMMENT ON POLICY auth_audit_events_tenant_read ON public.auth_audit_events IS
        'Tenant audit records are readable only in their tenant transaction; writes use record_auth_event.'""")


def downgrade() -> None:
    # Deliberately irreversible: rollback must not reopen identity or audit data.
    pass
