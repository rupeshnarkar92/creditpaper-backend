"""Move slot-file deletes behind a stored function."""

from alembic import op

revision = "nn_104_slot_file_delete"
down_revision = "nn_103_slot_file_upsert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.delete_document_slot_files(p_tenant_id uuid,p_deal_id text,p_document_id uuid,p_slot_id uuid,p_role text) RETURNS integer LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ DECLARE n integer; BEGIN DELETE FROM public.document_slot_files f USING public.document_slots s WHERE s.id=f.slot_id AND f.tenant_id=p_tenant_id AND f.document_id=p_document_id AND s.deal_id=p_deal_id AND s.tenant_id=p_tenant_id AND (p_slot_id IS NULL OR f.slot_id=p_slot_id) AND (p_role IS NULL OR f.role=p_role); GET DIAGNOSTICS n=ROW_COUNT; RETURN n; END $$; REVOKE ALL ON FUNCTION public.delete_document_slot_files(uuid,text,uuid,uuid,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.delete_document_slot_files(uuid,text,uuid,uuid,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.delete_document_slot_files(uuid,text,uuid,uuid,text)")
