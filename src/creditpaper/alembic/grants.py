"""Post-migration runtime grants with explicit least-privilege exceptions.

Ordinary application tables retain the historical CRUD default. Audit/cache
stores and platform tables use their narrower mutation contracts and are
reasserted after every migration so new tables cannot inherit broad grants.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_ROLE = "creditpaper_runtime"
_APPEND_ONLY = {
    "deal_recommendations",
    "fact_labels",
    "ingest_parse_artifacts",
    "ledger_assertions",
    "ledger_approvals",
    "ledger_chunks",
    "ledger_edge_retractions",
    "ledger_edges",
    "ledger_embeddings",
    "ledger_entities",
    "ledger_entity_links",
    "ledger_entity_link_retractions",
    "ledger_identifiers",
    "ledger_match_proposals",
    "ledger_outbox",
    "ledger_reviews",
    "ledger_resolutions",
    "ledger_supersessions",
    "processing_job_dead_letters",
    "tenant_llm_usage_events",
    "tenant_integration_audit_events",
}
_READ_ONLY = {"platform_defaults"}
_NO_DELETE = {"contradiction_register", "ingest_proposals", "ingest_confirmations"}

# Worker lease state is mutable, but job history is not hard-deleted at runtime.
_NO_MUTATE_DELETE = {"processing_jobs"}
_NO_UPDATE = {"deal_document_versions", "ingest_fact_cache"}


def _privileges_for(table: str) -> tuple[str, ...]:
    if table in _READ_ONLY:
        return ("SELECT",)
    if table in _APPEND_ONLY:
        return ("SELECT", "INSERT")
    if table in _NO_DELETE:
        return ("SELECT", "INSERT", "UPDATE")
    if table in _NO_UPDATE:
        return ("SELECT", "INSERT", "DELETE")
    if table in _NO_MUTATE_DELETE:
        return ("SELECT", "INSERT", "UPDATE")
    return ("SELECT", "INSERT", "UPDATE", "DELETE")


def _quote_table(table: str) -> str:
    # Table names come from pg_tables, but quote the identifier rather than
    # depending on the catalog remaining limited to simple lowercase names.
    return '"' + table.replace('"', '""') + '"'


def _grant(connection, table: str, privileges: tuple[str, ...]) -> None:
    qualified = f"public.{_quote_table(table)}"
    connection.exec_driver_sql(f"REVOKE ALL ON {qualified} FROM {_ROLE}")
    if privileges:
        connection.exec_driver_sql(f"GRANT {', '.join(privileges)} ON {qualified} TO {_ROLE}")
        log.info("Granted %s on %s to %s", ", ".join(privileges), table, _ROLE)


def ensure_runtime_grants(connection) -> None:  # noqa: ANN001
    """Reconcile exact runtime privileges for protected tables and CRUD otherwise."""
    # PostgREST roles must never inherit direct access through PostgreSQL's
    # default PUBLIC function EXECUTE grant or a stale explicit table grant.
    # Repeat this after every migration so newly created objects fail closed.
    connection.exec_driver_sql(
        """
        DO $security$
        DECLARE obj record;
        DECLARE role_name text;
        BEGIN
            FOR obj IN SELECT tablename AS name FROM pg_tables WHERE schemaname = 'public' LOOP
                EXECUTE format('REVOKE ALL ON TABLE public.%%I FROM PUBLIC', obj.name);
                FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                        EXECUTE format('REVOKE ALL ON TABLE public.%%I FROM %%I', obj.name, role_name);
                    END IF;
                END LOOP;
            END LOOP;
            FOR obj IN SELECT sequencename AS name FROM pg_sequences WHERE schemaname = 'public' LOOP
                EXECUTE format('REVOKE ALL ON SEQUENCE public.%%I FROM PUBLIC', obj.name);
                FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                        EXECUTE format('REVOKE ALL ON SEQUENCE public.%%I FROM %%I', obj.name, role_name);
                    END IF;
                END LOOP;
            END LOOP;
            FOR obj IN
                SELECT p.oid::regprocedure AS signature
                FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'
            LOOP
                EXECUTE format('REVOKE ALL ON FUNCTION %%s FROM PUBLIC', obj.signature);
                FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                        EXECUTE format('REVOKE ALL ON FUNCTION %%s FROM %%I', obj.signature, role_name);
                    END IF;
                END LOOP;
            END LOOP;
        END $security$;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
        """
    )
    result = connection.exec_driver_sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    tables = [row[0] for row in result]

    for table in tables:
        if table in {"alembic_version", "ledger_fact_history"}:
            continue
        _grant(connection, table, _privileges_for(table))

    # Processing-status columns and the classifier-managed slot are changed only
    # by fixed, tenant/deal-scoped database functions (CLAUDE.md: no inline hot
    # path SQL). Do not grant broad UPDATE on the backing document tables.
    functions = (
        (
            "set_document_processing_status",
            "(uuid,text,uuid,text,text)",
        ),
        (
            "set_document_kind_if_unset",
            "(uuid,text,uuid,text)",
        ),
    )
    for name, signature in functions:
        connection.exec_driver_sql(f"REVOKE ALL ON FUNCTION public.{name}{signature} FROM PUBLIC")
        connection.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION public.{name}{signature} TO {_ROLE}")
