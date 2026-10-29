"""Lookup table: platform-level reference data with self-referencing parents.

Revision ID: rn_001_lookup_tables
Revises: 0042_merge_tenant_gateway

Chains from the shared database's stamped head (the mcp-integrations merge
0042_merge_tenant_gateway); its prerequisite files 0040_tenant_llm_byok /
0041_ai_gateway_control_plane / 0042_merge_tenant_provision_and_gateway came
from origin/devel_mcp_integrations verbatim so the graph resolves locally.

Platform data (no tenant_id, no RLS): one row per lookup entry, ``parent_id``
self-references ``lookups.id`` (NULL = top level). Access is stored-procedure
only — no inline SQL in Python service/store code (CLAUDE.md).

Functions added:
  - lookup_fetch(p_type, p_lookup_key)   -- 'parent': full top-level list (key ignored);
                                            'child': direct children of the key row
  - lookup_create(p_look_value, p_lookup_description, p_parent_id)
  - lookup_update(p_id, p_look_value, p_lookup_description, p_parent_id)
                                        -- full replace; parent NULL = top level
  - lookup_delete(p_id)                 -- refused while children exist

Errors are JSONB ``{"error": "<message>"}`` (tenant-members SP convention).
Revision IDs use the per-developer prefix scheme (rn_/us_/nn_) and must stay
within alembic's VARCHAR(32) version_num column.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "rn_001_lookup_tables"
down_revision: str | None = "0042_merge_tenant_gateway"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LOOKUPS_DDL = """
CREATE TABLE lookups (
    id                 BIGSERIAL PRIMARY KEY,
    look_value         TEXT NOT NULL,
    lookup_description TEXT NOT NULL,
    parent_id          BIGINT REFERENCES lookups(id) ON DELETE RESTRICT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_lookups_no_self_parent CHECK (parent_id IS NULL OR parent_id <> id)
);

CREATE UNIQUE INDEX uq_lookups_value ON lookups (lower(look_value));
CREATE INDEX ix_lookups_parent ON lookups (parent_id);
"""


# ---------------------------------------------------------------------------
# lookup_fetch — the read API. 'parent' always returns the full top-level
# list; 'child' resolves the key (trimmed, case-insensitive look_value) and
# returns that row's direct children only.
# ---------------------------------------------------------------------------

_LOOKUP_FETCH_FN = """
CREATE OR REPLACE FUNCTION lookup_fetch(
    p_type       TEXT,
    p_lookup_key TEXT DEFAULT ''
) RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_type      TEXT := lower(coalesce(p_type, ''));
    v_key       TEXT := trim(coalesce(p_lookup_key, ''));
    v_parent_id BIGINT;
    v_result    JSONB;
BEGIN
    IF v_type = 'parent' THEN
        SELECT coalesce(jsonb_agg(
            jsonb_build_object(
                'id',                 l.id,
                'look_value',         l.look_value,
                'lookup_description', l.lookup_description,
                'parent_id',          l.parent_id
            ) ORDER BY l.id
        ), '[]'::jsonb)
        INTO v_result
        FROM lookups l
        WHERE l.parent_id IS NULL;
        RETURN v_result;
    ELSIF v_type = 'child' THEN
        IF v_key = '' THEN
            RETURN jsonb_build_object('error', 'Lookup key is required');
        END IF;

        SELECT l.id INTO v_parent_id
        FROM lookups l
        WHERE lower(l.look_value) = lower(v_key)
        LIMIT 1;

        IF v_parent_id IS NULL THEN
            RETURN jsonb_build_object('error', 'Lookup key not found');
        END IF;

        SELECT coalesce(jsonb_agg(
            jsonb_build_object(
                'id',                 l.id,
                'look_value',         l.look_value,
                'lookup_description', l.lookup_description,
                'parent_id',          l.parent_id
            ) ORDER BY l.id
        ), '[]'::jsonb)
        INTO v_result
        FROM lookups l
        WHERE l.parent_id = v_parent_id;
        RETURN v_result;
    END IF;

    RETURN jsonb_build_object('error', 'Invalid type — use parent or child');
END;
$$;
"""


# ---------------------------------------------------------------------------
# lookup_create — insert one row; refuses empty/duplicate values and a
# parent key that does not resolve.
# ---------------------------------------------------------------------------

_LOOKUP_CREATE_FN = """
CREATE OR REPLACE FUNCTION lookup_create(
    p_look_value         TEXT,
    p_lookup_description TEXT,
    p_parent_id          BIGINT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_value TEXT := trim(coalesce(p_look_value, ''));
    v_desc  TEXT := trim(coalesce(p_lookup_description, ''));
    v_row   lookups%ROWTYPE;
BEGIN
    IF v_value = '' THEN
        RETURN jsonb_build_object('error', 'Look value is required');
    END IF;
    IF v_desc = '' THEN
        RETURN jsonb_build_object('error', 'Lookup description is required');
    END IF;
    IF EXISTS (SELECT 1 FROM lookups l WHERE lower(l.look_value) = lower(v_value)) THEN
        RETURN jsonb_build_object('error', 'Look value already exists');
    END IF;
    IF p_parent_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM lookups l WHERE l.id = p_parent_id) THEN
        RETURN jsonb_build_object('error', 'Parent lookup not found');
    END IF;

    INSERT INTO lookups (look_value, lookup_description, parent_id)
    VALUES (v_value, v_desc, p_parent_id)
    RETURNING * INTO v_row;

    RETURN jsonb_build_object(
        'id',                 v_row.id,
        'look_value',         v_row.look_value,
        'lookup_description', v_row.lookup_description,
        'parent_id',          v_row.parent_id
    );
END;
$$;
"""


# ---------------------------------------------------------------------------
# lookup_update — full replace of value/description/parent (parent NULL
# moves the row to top level). Refuses duplicates (excluding self),
# self-parent and ancestor cycles (recursive walk up from the new parent).
# ---------------------------------------------------------------------------

_LOOKUP_UPDATE_FN = """
CREATE OR REPLACE FUNCTION lookup_update(
    p_id                  BIGINT,
    p_look_value          TEXT,
    p_lookup_description  TEXT,
    p_parent_id           BIGINT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_value TEXT := trim(coalesce(p_look_value, ''));
    v_desc  TEXT := trim(coalesce(p_lookup_description, ''));
    v_cycle BOOLEAN;
    v_row   lookups%ROWTYPE;
BEGIN
    SELECT * INTO v_row FROM lookups l WHERE l.id = p_id;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('error', 'Lookup not found');
    END IF;
    IF v_value = '' THEN
        RETURN jsonb_build_object('error', 'Look value is required');
    END IF;
    IF v_desc = '' THEN
        RETURN jsonb_build_object('error', 'Lookup description is required');
    END IF;
    IF EXISTS (
        SELECT 1 FROM lookups l
        WHERE lower(l.look_value) = lower(v_value) AND l.id <> p_id
    ) THEN
        RETURN jsonb_build_object('error', 'Look value already exists');
    END IF;

    IF p_parent_id IS NOT NULL THEN
        IF p_parent_id = p_id THEN
            RETURN jsonb_build_object('error', 'Lookup cannot be its own parent');
        END IF;
        IF NOT EXISTS (SELECT 1 FROM lookups l WHERE l.id = p_parent_id) THEN
            RETURN jsonb_build_object('error', 'Parent lookup not found');
        END IF;

        WITH RECURSIVE up AS (
            SELECT l.id, l.parent_id
            FROM lookups l
            WHERE l.id = p_parent_id
            UNION ALL
            SELECT l.id, l.parent_id
            FROM lookups l
            JOIN up u ON l.id = u.parent_id
        )
        SELECT EXISTS (SELECT 1 FROM up WHERE id = p_id) INTO v_cycle;

        IF v_cycle THEN
            RETURN jsonb_build_object('error', 'Cannot set a row as its own ancestor');
        END IF;
    END IF;

    UPDATE lookups
    SET look_value         = v_value,
        lookup_description = v_desc,
        parent_id          = p_parent_id,
        updated_at         = now()
    WHERE id = p_id
    RETURNING * INTO v_row;

    RETURN jsonb_build_object(
        'id',                 v_row.id,
        'look_value',         v_row.look_value,
        'lookup_description', v_row.lookup_description,
        'parent_id',          v_row.parent_id
    );
END;
$$;
"""


# ---------------------------------------------------------------------------
# lookup_delete — refused while any row still points at this one.
# ---------------------------------------------------------------------------

_LOOKUP_DELETE_FN = """
CREATE OR REPLACE FUNCTION lookup_delete(
    p_id BIGINT
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_row lookups%ROWTYPE;
BEGIN
    SELECT * INTO v_row FROM lookups l WHERE l.id = p_id;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('error', 'Lookup not found');
    END IF;
    IF EXISTS (SELECT 1 FROM lookups l WHERE l.parent_id = p_id) THEN
        RETURN jsonb_build_object('error', 'Lookup has child rows');
    END IF;

    DELETE FROM lookups WHERE id = p_id;
    RETURN jsonb_build_object('deleted', true);
END;
$$;
"""


_DOWNGRADE_FNS = """
DROP FUNCTION IF EXISTS lookup_fetch(TEXT, TEXT);
DROP FUNCTION IF EXISTS lookup_create(TEXT, TEXT, BIGINT);
DROP FUNCTION IF EXISTS lookup_update(BIGINT, TEXT, TEXT, BIGINT);
DROP FUNCTION IF EXISTS lookup_delete(BIGINT);
DROP TABLE IF EXISTS lookups;
"""


def upgrade() -> None:
    op.execute(_LOOKUPS_DDL)
    op.execute(_LOOKUP_FETCH_FN)
    op.execute(_LOOKUP_CREATE_FN)
    op.execute(_LOOKUP_UPDATE_FN)
    op.execute(_LOOKUP_DELETE_FN)


def downgrade() -> None:
    op.execute(_DOWNGRADE_FNS)
