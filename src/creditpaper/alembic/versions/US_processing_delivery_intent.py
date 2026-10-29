"""Record successor delivery intent atomically with workflow transitions."""

from alembic import op

revision = "US_processing_delivery_intent"
down_revision = "US_broker_lease_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE public.processing_jobs
          ADD COLUMN delivery_requested_at timestamptz;

        CREATE FUNCTION public.request_processing_delivery()
        RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        BEGIN
          IF NEW.status='pending' THEN
            UPDATE public.processing_jobs SET delivery_requested_at=clock_timestamp()
              WHERE tenant_id=NEW.tenant_id AND id=NEW.id;
            PERFORM pg_notify('creditpaper_processing_ready',NEW.tenant_id::text);
          ELSIF TG_OP='UPDATE' AND NEW.status IN ('complete','failed')
                AND OLD.status IS DISTINCT FROM NEW.status THEN
            -- Child jobs already exist durably before the parent finishes.
            -- Their wake-up intent commits or rolls back WITH this completion.
            UPDATE public.processing_jobs SET delivery_requested_at=clock_timestamp()
              WHERE tenant_id=NEW.tenant_id AND deal_id=NEW.deal_id AND status='pending';
            PERFORM pg_notify('creditpaper_processing_ready',NEW.tenant_id::text);
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER processing_delivery_intent
          AFTER INSERT OR UPDATE OF status ON public.processing_jobs
          FOR EACH ROW EXECUTE FUNCTION public.request_processing_delivery();
        REVOKE ALL ON FUNCTION public.request_processing_delivery() FROM PUBLIC;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER processing_delivery_intent ON public.processing_jobs")
    op.execute("DROP FUNCTION public.request_processing_delivery()")
    op.execute("ALTER TABLE public.processing_jobs DROP COLUMN delivery_requested_at")
