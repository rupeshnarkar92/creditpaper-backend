"""Persist a slot confirmation and its extraction jobs together.

Additive: deploy before the new confirmation caller. No backfill. To roll back,
restore the previous caller first; existing queued jobs must be allowed to drain.
"""

from alembic import op

revision = "nn_146_atomic_slot_confirmation"
down_revision = "nn_145_bundle_check_inputs"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION public.write_slot_confirmation(p_tenant uuid,p_deal text,p_actor uuid,
      p_changes jsonb,p_now timestamptz) RETURNS jsonb
    LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    DECLARE sid uuid := (p_changes->'confirmation'->>'slot_id')::uuid;
      actual_files jsonb; expected_files jsonb; j jsonb; jid uuid; ids jsonb := '[]';
    BEGIN
      IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid
      THEN RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501'; END IF;
      PERFORM 1 FROM public.deals WHERE tenant_id=p_tenant AND id=p_deal FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Deal not found' USING ERRCODE='P0002'; END IF;
      PERFORM 1 FROM public.document_slots WHERE tenant_id=p_tenant AND deal_id=p_deal
        AND id=sid AND status='proposed' FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Slot proposal changed' USING ERRCODE='40001'; END IF;
      SELECT COALESCE(jsonb_agg(jsonb_build_object('document_id',document_id,'version_id',version_id)
        ORDER BY document_id,version_id),'[]') INTO actual_files
        FROM public.document_slot_files WHERE tenant_id=p_tenant AND slot_id=sid AND role='assigned';
      SELECT COALESCE(jsonb_agg(jsonb_build_object('document_id',(x->>'document_id')::uuid,
        'version_id',(x->>'version_id')::uuid)
        ORDER BY (x->>'document_id')::uuid,(x->>'version_id')::uuid),'[]') INTO expected_files
        FROM jsonb_array_elements(p_changes->'confirmation'->'files') x;
      IF actual_files IS DISTINCT FROM expected_files THEN
        RAISE EXCEPTION 'Slot proposal changed' USING ERRCODE='40001';
      END IF;
      PERFORM public.write_slot_changes(p_tenant,p_deal,p_actor,p_changes,p_now);
      FOR j IN SELECT value FROM jsonb_array_elements(p_changes->'extraction_jobs') LOOP
        IF NOT EXISTS (SELECT 1 FROM public.document_slot_files f
          JOIN public.deal_document_versions v ON v.tenant_id=f.tenant_id AND v.id=f.version_id
          WHERE f.tenant_id=p_tenant AND f.slot_id=sid AND f.role='assigned'
            AND f.document_id=(j->>'document_id')::uuid AND f.version_id=(j->>'version_id')::uuid
            AND v.deal_id=p_deal AND v.document_id=f.document_id)
          OR COALESCE(j->>'extractor','')='' THEN
          RAISE EXCEPTION 'Invalid confirmation job' USING ERRCODE='22023';
        END IF;
        INSERT INTO public.processing_jobs(tenant_id,deal_id,kind,resource_id,payload)
          VALUES(p_tenant,p_deal,'document',(j->>'version_id')||':'||(j->>'extractor'),j)
          ON CONFLICT DO NOTHING RETURNING id INTO jid;
        IF jid IS NULL THEN
          SELECT id INTO STRICT jid FROM public.processing_jobs
            WHERE tenant_id=p_tenant AND deal_id=p_deal AND kind='document'
              AND resource_id=(j->>'version_id')||':'||(j->>'extractor')
              AND status IN ('pending','running');
        END IF;
        ids := ids || jsonb_build_array(jid);
      END LOOP;
      RETURN ids;
    END $$;
    COMMENT ON FUNCTION public.write_slot_confirmation(uuid,text,uuid,jsonb,timestamptz) IS
      'Tenant, deal, actor, changes JSON (confirmation slot/files snapshot and extraction_jobs), timestamp -> ordered durable job UUIDs. Locks deal, rejects stale proposals, atomically applies slot/reset/audit writes and queues extraction. Failure rolls back all writes; caller must not enqueue again.';
    REVOKE ALL ON FUNCTION public.write_slot_confirmation(uuid,text,uuid,jsonb,timestamptz) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.write_slot_confirmation(uuid,text,uuid,jsonb,timestamptz) TO creditpaper_runtime;
    DO $$ DECLARE r text; BEGIN
      FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
          EXECUTE format('REVOKE ALL ON FUNCTION public.write_slot_confirmation(uuid,text,uuid,jsonb,timestamptz) FROM %I',r);
        END IF;
      END LOOP;
    END $$;
    """)


def downgrade():
    op.execute("DROP FUNCTION public.write_slot_confirmation(uuid,text,uuid,jsonb,timestamptz)")
