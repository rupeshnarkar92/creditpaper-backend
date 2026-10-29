"""Seed default RBAC roles and permissions.

Run with: python -m creditpaper.seed.seed_rbac
"""

import sys
import uuid

from creditpaper.db import cursor

PERMISSIONS = [
    ("papers.create", "papers", "create", "Create papers"),
    ("papers.read", "papers", "read", "Read papers"),
    ("papers.update", "papers", "update", "Update papers"),
    ("papers.delete", "papers", "delete", "Delete papers"),
    ("papers.approve", "papers", "approve", "Approve papers"),
    ("papers.comment", "papers", "comment", "Comment on papers"),
    ("users.invite", "users", "invite", "Invite users"),
    ("users.read", "users", "read", "Read users"),
    ("users.update", "users", "update", "Update users"),
    ("users.remove", "users", "remove", "Remove users"),
    ("settings.read", "settings", "read", "Read settings"),
    ("settings.update", "settings", "update", "Update settings"),
    ("templates.create", "templates", "create", "Create templates"),
    ("templates.read", "templates", "read", "Read templates"),
    ("templates.update", "templates", "update", "Update templates"),
    ("templates.delete", "templates", "delete", "Delete templates"),
    ("billing.read", "billing", "read", "Read billing"),
    ("billing.manage", "billing", "manage", "Manage billing"),
]

DEFAULT_ROLES = [
    ("tenant_admin", "Tenant Admin", "Full admin access", True, True),
    ("maker", "Maker", "Create and edit papers", False, True),
    ("checker", "Checker", "Edit, review and approve papers", False, False),
    ("viewer", "Viewer", "Read-only access", False, False),
]

ROLE_PERMISSIONS = {
    "tenant_admin": [p[0] for p in PERMISSIONS],
    "maker": [
        "papers.create",
        "papers.read",
        "papers.update",
        "papers.comment",
        "templates.read",
        "settings.read",
    ],
    # Checkers may edit (DECISIONS 2026-09-15, 2026-09-25, restored 2026-10-07); delete is not granted.
    "checker": [
        "papers.create",
        "papers.read",
        "papers.update",
        "papers.approve",
        "papers.comment",
        "templates.create",
        "templates.read",
        "templates.update",
    ],
    "viewer": ["papers.read", "templates.read"],
}


def seed_rbac() -> None:
    """Seed default roles and permissions using raw SQL."""
    permission_ids: dict[str, str] = {}
    role_ids: dict[str, str] = {}

    with cursor() as cur:
        # Create permissions (idempotent — fetch existing id on conflict)
        for name, resource, action, description in PERMISSIONS:
            perm_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO permissions (id, name, resource, action, description)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name) DO NOTHING
                RETURNING id
                """,
                (perm_id, name, resource, action, description),
            )
            row = cur.fetchone()
            if row:
                permission_ids[name] = row[0]
            else:
                cur.execute("SELECT id FROM permissions WHERE name = %s", (name,))
                permission_ids[name] = cur.fetchone()[0]

        # Create platform-wide roles (tenant_id = NULL). Postgres does not
        # treat NULLs as equal in UNIQUE constraints, so ON CONFLICT cannot
        # catch re-runs — use WHERE NOT EXISTS instead.
        for name, display_name, description, is_system, is_default in DEFAULT_ROLES:
            role_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO roles (id, tenant_id, name, display_name, description, is_system, is_default)
                SELECT %s, NULL, %s, %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM roles WHERE tenant_id IS NULL AND name = %s
                )
                RETURNING id
                """,
                (role_id, name, display_name, description, is_system, is_default, name),
            )
            row = cur.fetchone()
            if row:
                role_ids[name] = row[0]
            else:
                cur.execute(
                    "SELECT id FROM roles WHERE tenant_id IS NULL AND name = %s",
                    (name,),
                )
                role_ids[name] = cur.fetchone()[0]

        # Assign permissions to platform-wide roles
        for role_name, perm_names in ROLE_PERMISSIONS.items():
            role_id = role_ids[role_name]
            for perm_name in perm_names:
                perm_id = permission_ids[perm_name]
                cur.execute(
                    """
                    INSERT INTO role_permissions (role_id, permission_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (role_id, perm_id),
                )

        # Backfill: tenant-scoped roles (created at signup/invite) inherit
        # permissions from the platform-wide role of the same name.
        cur.execute(
            """
            INSERT INTO role_permissions (role_id, permission_id)
            SELECT tsr.id, rp.permission_id
            FROM roles tsr
            JOIN roles gr ON gr.name = tsr.name AND gr.tenant_id IS NULL
            JOIN role_permissions rp ON rp.role_id = gr.id
            WHERE tsr.tenant_id IS NOT NULL
            ON CONFLICT DO NOTHING
            """
        )

        # Reconcile: revoke grants a named role no longer has in
        # ROLE_PERMISSIONS (insert-only seeding never removes stale rows;
        # platform roles with tenant_id IS NULL cannot rely on ON CONFLICT).
        for role_name, perm_names in ROLE_PERMISSIONS.items():
            cur.execute(
                """
                DELETE FROM role_permissions rp
                USING roles r
                WHERE rp.role_id = r.id
                  AND r.name = %s
                  AND NOT EXISTS (
                    SELECT 1 FROM permissions p
                    WHERE p.id = rp.permission_id
                      AND p.name = ANY(%s)
                  )
                """,
                (role_name, perm_names),
            )

    # Operational startup confirmation, not debug output — stderr joins the
    # server's own log stream (uvicorn logs there too).
    print(f"Seeded {len(PERMISSIONS)} permissions, {len(DEFAULT_ROLES)} roles", file=sys.stderr)  # noqa: T201


if __name__ == "__main__":
    seed_rbac()
