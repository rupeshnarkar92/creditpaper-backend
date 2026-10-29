"""Ignore late heartbeat state changes after a folder batch is sealed."""

from alembic import op

revision = "nn_138_batch_seal_guard"
down_revision = "nn_137_ingestion_gate"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE OR REPLACE FUNCTION public.deal_upload_batch_write(p_tenant uuid,p_deal text,p_id uuid,
      p_total integer,p_failed integer,p_sealed boolean) RETURNS void
    LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    BEGIN
      PERFORM 1 FROM public.deals WHERE tenant_id=p_tenant AND id=p_deal FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Deal not found'; END IF;
      IF EXISTS(SELECT 1 FROM public.processing_jobs WHERE tenant_id=p_tenant
        AND deal_id=p_deal AND kind='generation' AND status IN ('pending','running'))
      THEN RAISE EXCEPTION 'Paper generation is active'; END IF;
      INSERT INTO public.deal_upload_batches(id,tenant_id,deal_id,total,failed,sealed)
      VALUES(p_id,p_tenant,p_deal,p_total,p_failed,p_sealed)
      ON CONFLICT(id) DO UPDATE SET updated_at=clock_timestamp(),
        failed=CASE WHEN deal_upload_batches.sealed THEN deal_upload_batches.failed ELSE EXCLUDED.failed END,
        sealed=deal_upload_batches.sealed OR EXCLUDED.sealed
      WHERE deal_upload_batches.tenant_id=p_tenant AND deal_upload_batches.deal_id=p_deal;
    END $$;
    """)


def downgrade():
    pass
