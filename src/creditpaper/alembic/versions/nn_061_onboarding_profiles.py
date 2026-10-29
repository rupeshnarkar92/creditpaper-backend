"""Move onboarding state and profile persistence behind stored functions."""

from alembic import op

revision = "nn_061_onboarding_profiles"
down_revision = "nn_060_slot_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.onboarding_state_row(p_tenant_id uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT s.state FROM public.onboarding_state s WHERE s.tenant_id=p_tenant_id $$;
        CREATE OR REPLACE FUNCTION public.save_onboarding_state(p_tenant_id uuid,p_state jsonb) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.onboarding_state(tenant_id,state,updated_at) VALUES(p_tenant_id,p_state,now()) ON CONFLICT(tenant_id) DO UPDATE SET state=EXCLUDED.state,updated_at=now() $$;
        CREATE OR REPLACE FUNCTION public.brand_profile_row(p_tenant_id uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT b.profile FROM public.brand_profile b WHERE b.tenant_id=p_tenant_id $$;
        CREATE OR REPLACE FUNCTION public.save_brand_profile(p_tenant_id uuid,p_profile jsonb) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.brand_profile(tenant_id,profile,updated_at) VALUES(p_tenant_id,p_profile,now()) ON CONFLICT(tenant_id) DO UPDATE SET profile=EXCLUDED.profile,updated_at=now() $$;
        CREATE OR REPLACE FUNCTION public.section_order_row(p_tenant_id uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT s.sections FROM public.section_orders s WHERE s.tenant_id=p_tenant_id $$;
        CREATE OR REPLACE FUNCTION public.save_section_order(p_tenant_id uuid,p_sections jsonb) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.section_orders(tenant_id,sections,updated_at) VALUES(p_tenant_id,p_sections,now()) ON CONFLICT(tenant_id) DO UPDATE SET sections=EXCLUDED.sections,updated_at=now() $$;
        CREATE OR REPLACE FUNCTION public.voice_profile_row(p_tenant_id uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT v.profile FROM public.voice_profile v WHERE v.tenant_id=p_tenant_id $$;
        CREATE OR REPLACE FUNCTION public.save_voice_profile(p_tenant_id uuid,p_profile jsonb) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.voice_profile(tenant_id,profile,updated_at) VALUES(p_tenant_id,p_profile,now()) ON CONFLICT(tenant_id) DO UPDATE SET profile=EXCLUDED.profile,updated_at=now() $$;
        CREATE OR REPLACE FUNCTION public.delete_voice_profile(p_tenant_id uuid) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ DELETE FROM public.voice_profile WHERE tenant_id=p_tenant_id RETURNING true $$;
        CREATE OR REPLACE FUNCTION public.ensure_document_slots(p_tenant_id uuid,p_deal_id text,p_keys text[]) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.document_slots(tenant_id,deal_id,slot_key,status) SELECT p_tenant_id,p_deal_id,k,'empty' FROM unnest(p_keys) k ON CONFLICT(deal_id,slot_key) DO NOTHING $$;
        CREATE OR REPLACE FUNCTION public.set_version_extraction_status(p_tenant_id uuid,p_version_id uuid,p_status text,p_error text) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ UPDATE public.deal_document_versions SET extraction_status=p_status,extraction_error=p_error WHERE id=p_version_id AND tenant_id=p_tenant_id $$;
        REVOKE ALL ON FUNCTION public.onboarding_state_row(uuid),public.save_onboarding_state(uuid,jsonb),public.brand_profile_row(uuid),public.save_brand_profile(uuid,jsonb),public.section_order_row(uuid),public.save_section_order(uuid,jsonb),public.voice_profile_row(uuid),public.save_voice_profile(uuid,jsonb),public.delete_voice_profile(uuid),public.ensure_document_slots(uuid,text,text[]),public.set_version_extraction_status(uuid,uuid,text,text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.onboarding_state_row(uuid),public.save_onboarding_state(uuid,jsonb),public.brand_profile_row(uuid),public.save_brand_profile(uuid,jsonb),public.section_order_row(uuid),public.save_section_order(uuid,jsonb),public.voice_profile_row(uuid),public.save_voice_profile(uuid,jsonb),public.delete_voice_profile(uuid),public.ensure_document_slots(uuid,text,text[]),public.set_version_extraction_status(uuid,uuid,text,text) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.onboarding_state_row(uuid),public.save_onboarding_state(uuid,jsonb),public.brand_profile_row(uuid),public.save_brand_profile(uuid,jsonb),public.section_order_row(uuid),public.save_section_order(uuid,jsonb),public.voice_profile_row(uuid),public.save_voice_profile(uuid,jsonb),public.delete_voice_profile(uuid),public.ensure_document_slots(uuid,text,text[]),public.set_version_extraction_status(uuid,uuid,text,text)"
    )
