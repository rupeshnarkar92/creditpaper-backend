"""One round trip for ordered, atomic slot mutations and audit events."""
from alembic import op

revision = "nn_141_slot_write_batch"
down_revision = "nn_140_partial_ingestion"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION public.write_slot_changes(p_tenant uuid,p_deal text,p_actor uuid,
      p_changes jsonb,p_now timestamptz) RETURNS void
    LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    DECLARE x jsonb;
    BEGIN
      IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid
      THEN RAISE EXCEPTION 'Tenant context mismatch'; END IF;
      PERFORM 1 FROM public.deals WHERE tenant_id=p_tenant AND id=p_deal FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Deal not found'; END IF;
      FOR x IN SELECT value FROM jsonb_array_elements(p_changes->'remove_files') LOOP
        PERFORM public.delete_document_slot_files(p_tenant,p_deal,(x->>'document_id')::uuid,
          (x->>'slot_id')::uuid,x->>'role');
      END LOOP;
      FOR x IN SELECT value FROM jsonb_array_elements(p_changes->'add_files') LOOP
        PERFORM public.upsert_document_slot_file(p_tenant,p_deal,(x->>'slot_id')::uuid,
          (x->>'document_id')::uuid,(x->>'version_id')::uuid,x->>'role',
          (x->>'confidence')::numeric,x->>'match_reason');
      END LOOP;
      FOR x IN SELECT value FROM jsonb_array_elements(p_changes->'slot_updates') LOOP
        PERFORM public.update_document_slot(p_tenant,p_deal,(x->>'id')::uuid,x->>'status',
          (x->>'confidence')::numeric,x->>'match_reason',x->>'error',p_now);
      END LOOP;
      FOR x IN SELECT value FROM jsonb_array_elements(p_changes->'reset_versions') LOOP
        PERFORM public.reset_document_extraction(p_tenant,p_deal,(x#>>'{}')::uuid);
      END LOOP;
      IF p_changes->>'archive_document' IS NOT NULL THEN
        PERFORM public.archive_slot_document(p_tenant,p_deal,(p_changes->>'archive_document')::uuid,p_now);
      END IF;
      FOR x IN SELECT value FROM jsonb_array_elements(p_changes->'audit') LOOP
        INSERT INTO public.deal_audit_events(tenant_id,deal_id,actor_id,action,entity_type,
          entity_id,event_metadata,correlation_id,created_at)
        VALUES(p_tenant,p_deal,p_actor,x->>'action',COALESCE(x->>'entity_type','document_slot'),
          x->>'entity_id',COALESCE(NULLIF(x->'metadata','null'::jsonb),'{}'::jsonb),NULL,p_now);
      END LOOP;
    END $$;
    COMMENT ON FUNCTION public.write_slot_changes(uuid,text,uuid,jsonb,timestamptz) IS
      'Apply ordered removals, file upserts, slot updates, resets, archive and audit atomically. Inputs tenant context, deal, actor, changes JSON, timestamp; returns void. Existing mutation routines retain their predicates.';
    REVOKE ALL ON FUNCTION public.write_slot_changes(uuid,text,uuid,jsonb,timestamptz) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.write_slot_changes(uuid,text,uuid,jsonb,timestamptz) TO creditpaper_runtime;
    """)


def downgrade():
    op.execute("DROP FUNCTION public.write_slot_changes(uuid,text,uuid,jsonb,timestamptz)")
