"""Move billing persistence behind documented database functions.

Revision ID: nn_0049_billing_stored_functions
Revises: US_runtime_doc_update
"""

from alembic import op

revision = "nn_0049_billing_stored_functions"
down_revision = "US_runtime_doc_update"
branch_labels = None
depends_on = None


_ACCOUNT_COLUMNS = """
    tenant_id uuid, tier text, billing_interval text, status text,
    current_period_start timestamptz, current_period_end timestamptz,
    stripe_customer_id text, stripe_subscription_id text, card_brand text,
    card_last4 varchar(4), card_exp_month smallint, card_exp_year smallint,
    created_at timestamptz, updated_at timestamptz
"""


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.billing_get_or_create_account(p_tenant_id uuid)
        RETURNS TABLE ({_ACCOUNT_COLUMNS})
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, public AS $$
        BEGIN
            IF p_tenant_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id', true), '')::uuid THEN
                RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.billing_accounts AS a (tenant_id) VALUES (p_tenant_id)
            ON CONFLICT ON CONSTRAINT billing_accounts_pkey DO NOTHING;
            RETURN QUERY SELECT a.tenant_id, a.tier, a.billing_interval, a.status,
                a.current_period_start, a.current_period_end, a.stripe_customer_id,
                a.stripe_subscription_id, a.card_brand, a.card_last4, a.card_exp_month,
                a.card_exp_year, a.created_at, a.updated_at
            FROM public.billing_accounts a WHERE a.tenant_id = p_tenant_id;
        END $$;

        CREATE OR REPLACE FUNCTION public.billing_find_account_by_customer(p_customer_id text)
        RETURNS TABLE ({_ACCOUNT_COLUMNS})
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            SELECT a.tenant_id, a.tier, a.billing_interval, a.status,
                a.current_period_start, a.current_period_end, a.stripe_customer_id,
                a.stripe_subscription_id, a.card_brand, a.card_last4, a.card_exp_month,
                a.card_exp_year, a.created_at, a.updated_at
            FROM public.billing_accounts a WHERE a.stripe_customer_id = p_customer_id
        $$;

        CREATE OR REPLACE FUNCTION public.billing_update_account(p_tenant_id uuid, p_patch jsonb)
        RETURNS TABLE ({_ACCOUNT_COLUMNS})
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, public AS $$
        DECLARE v_unknown text;
        BEGIN
            IF p_tenant_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id', true), '')::uuid THEN
                RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE = '42501';
            END IF;
            SELECT key INTO v_unknown FROM jsonb_object_keys(p_patch) AS key
            WHERE key <> ALL (ARRAY['tier','billing_interval','status','current_period_start',
                'current_period_end','stripe_customer_id','stripe_subscription_id','card_brand',
                'card_last4','card_exp_month','card_exp_year']) LIMIT 1;
            IF v_unknown IS NOT NULL THEN
                RAISE EXCEPTION 'field is not updatable: %', v_unknown USING ERRCODE = '22023';
            END IF;
            UPDATE public.billing_accounts AS a SET
                tier = CASE WHEN p_patch ? 'tier' THEN p_patch->>'tier' ELSE a.tier END,
                billing_interval = CASE WHEN p_patch ? 'billing_interval' THEN p_patch->>'billing_interval' ELSE a.billing_interval END,
                status = CASE WHEN p_patch ? 'status' THEN p_patch->>'status' ELSE a.status END,
                current_period_start = CASE WHEN p_patch ? 'current_period_start' THEN (p_patch->>'current_period_start')::timestamptz ELSE a.current_period_start END,
                current_period_end = CASE WHEN p_patch ? 'current_period_end' THEN (p_patch->>'current_period_end')::timestamptz ELSE a.current_period_end END,
                stripe_customer_id = CASE WHEN p_patch ? 'stripe_customer_id' THEN p_patch->>'stripe_customer_id' ELSE a.stripe_customer_id END,
                stripe_subscription_id = CASE WHEN p_patch ? 'stripe_subscription_id' THEN p_patch->>'stripe_subscription_id' ELSE a.stripe_subscription_id END,
                card_brand = CASE WHEN p_patch ? 'card_brand' THEN p_patch->>'card_brand' ELSE a.card_brand END,
                card_last4 = CASE WHEN p_patch ? 'card_last4' THEN p_patch->>'card_last4' ELSE a.card_last4 END,
                card_exp_month = CASE WHEN p_patch ? 'card_exp_month' THEN (p_patch->>'card_exp_month')::smallint ELSE a.card_exp_month END,
                card_exp_year = CASE WHEN p_patch ? 'card_exp_year' THEN (p_patch->>'card_exp_year')::smallint ELSE a.card_exp_year END,
                updated_at = now()
            WHERE a.tenant_id = p_tenant_id;
            RETURN QUERY SELECT a.tenant_id, a.tier, a.billing_interval, a.status,
                a.current_period_start, a.current_period_end, a.stripe_customer_id,
                a.stripe_subscription_id, a.card_brand, a.card_last4, a.card_exp_month,
                a.card_exp_year, a.created_at, a.updated_at
            FROM public.billing_accounts a WHERE a.tenant_id = p_tenant_id;
        END $$;

        CREATE OR REPLACE FUNCTION public.billing_count_events(
            p_tenant_id uuid, p_start timestamptz, p_end timestamptz
        ) RETURNS bigint LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path = pg_catalog, public AS $$
            SELECT count(*) FROM public.billable_events
            WHERE tenant_id = p_tenant_id AND occurred_at >= p_start AND occurred_at < p_end
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        $$;

        CREATE OR REPLACE FUNCTION public.billing_record_event(
            p_id uuid, p_tenant_id uuid, p_deal_id text, p_metadata jsonb
        ) RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER
        SET search_path = pg_catalog, public AS $$
        DECLARE v_inserted integer;
        BEGIN
            IF p_tenant_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id', true), '')::uuid THEN
                RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.billable_events (id, tenant_id, deal_id, event_type, metadata)
            VALUES (p_id, p_tenant_id, p_deal_id, 'paper_first_approved_render', p_metadata)
            ON CONFLICT ON CONSTRAINT uq_billable_once_per_deal DO NOTHING;
            GET DIAGNOSTICS v_inserted = ROW_COUNT;
            RETURN v_inserted = 1;
        END $$;

        CREATE OR REPLACE FUNCTION public.billing_list_invoices(p_tenant_id uuid, p_limit integer)
        RETURNS TABLE (id uuid, number text, description text, amount numeric, currency varchar(3),
            status text, pdf_url text, issued_at timestamptz)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT i.id, i.number, i.description, i.amount, i.currency, i.status, i.pdf_url, i.issued_at
            FROM public.billing_invoices i
            WHERE i.tenant_id = p_tenant_id
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            ORDER BY i.issued_at DESC LIMIT greatest(0, least(p_limit, 1000))
        $$;

        CREATE OR REPLACE FUNCTION public.billing_upsert_invoice(
            p_id uuid, p_tenant_id uuid, p_stripe_invoice_id text, p_number text,
            p_description text, p_amount numeric, p_currency varchar(3), p_status text,
            p_pdf_url text, p_issued_at timestamptz
        ) RETURNS void LANGUAGE plpgsql SECURITY INVOKER
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF p_tenant_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id', true), '')::uuid THEN
                RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.billing_invoices
                (id, tenant_id, stripe_invoice_id, number, description, amount, currency, status, pdf_url, issued_at)
            VALUES (p_id, p_tenant_id, p_stripe_invoice_id, p_number, p_description, p_amount,
                p_currency, p_status, p_pdf_url, p_issued_at)
            ON CONFLICT (stripe_invoice_id) DO UPDATE SET number = EXCLUDED.number,
                description = EXCLUDED.description, amount = EXCLUDED.amount,
                currency = EXCLUDED.currency, status = EXCLUDED.status, pdf_url = EXCLUDED.pdf_url;
        END $$;

        COMMENT ON FUNCTION public.billing_get_or_create_account(uuid) IS
            'Creates the tenant billing account when absent and returns its complete account row. Parameter: tenant UUID. Return: one billing account row.';
        COMMENT ON FUNCTION public.billing_find_account_by_customer(text) IS
            'Stripe webhook lookup across tenants. Parameter: Stripe customer ID. Return: matching account row or no row. Runtime-only exception; no browser execute grant.';
        COMMENT ON FUNCTION public.billing_update_account(uuid,jsonb) IS
            'Applies an allowlisted billing-account patch for the transaction tenant. Parameters: tenant UUID and patch object. Return: updated account row.';
        COMMENT ON FUNCTION public.billing_count_events(uuid,timestamptz,timestamptz) IS
            'Counts billable events in a half-open time range for the transaction tenant. Return: bigint count.';
        COMMENT ON FUNCTION public.billing_record_event(uuid,uuid,text,jsonb) IS
            'Records one idempotent first-approved-render event. Return: true only when inserted.';
        COMMENT ON FUNCTION public.billing_list_invoices(uuid,integer) IS
            'Lists newest tenant invoices with a capped row limit. Return: invoice rows.';
        COMMENT ON FUNCTION public.billing_upsert_invoice(uuid,uuid,text,text,text,numeric,varchar,text,text,timestamptz) IS
            'Creates or refreshes Stripe invoice metadata for the transaction tenant. Return: void.';

        ALTER TABLE public.billing_accounts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.billing_accounts FORCE ROW LEVEL SECURITY;
        CREATE POLICY billing_accounts_tenant ON public.billing_accounts
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
        ALTER TABLE public.billing_invoices ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.billing_invoices FORCE ROW LEVEL SECURITY;
        CREATE POLICY billing_invoices_tenant ON public.billing_invoices
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
        ALTER TABLE public.billable_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.billable_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY billable_events_tenant ON public.billable_events
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

        REVOKE ALL ON FUNCTION public.billing_get_or_create_account(uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.billing_find_account_by_customer(text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.billing_update_account(uuid,jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.billing_count_events(uuid,timestamptz,timestamptz) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.billing_record_event(uuid,uuid,text,jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.billing_list_invoices(uuid,integer) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.billing_upsert_invoice(uuid,uuid,text,text,text,numeric,varchar,text,text,timestamptz) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.billing_get_or_create_account(uuid),
            public.billing_find_account_by_customer(text), public.billing_update_account(uuid,jsonb),
            public.billing_count_events(uuid,timestamptz,timestamptz),
            public.billing_record_event(uuid,uuid,text,jsonb), public.billing_list_invoices(uuid,integer),
            public.billing_upsert_invoice(uuid,uuid,text,text,text,numeric,varchar,text,text,timestamptz)
            TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    raise RuntimeError("Do not restore direct billing-table access; restore a verified backup")
