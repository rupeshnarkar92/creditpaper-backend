from creditpaper.db import cursor

with cursor() as cur:
    cur.execute(
        """
        INSERT INTO users (
            id, email, password_hash, is_email_verified, is_active,
            auth_provider, created_at, updated_at, is_super_admin, role, last_login,
            failed_login_attempts
        )
        VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s
        )
        ON CONFLICT (id) DO UPDATE SET
            email = EXCLUDED.email,
            password_hash = EXCLUDED.password_hash,
            is_email_verified = EXCLUDED.is_email_verified,
            is_active = EXCLUDED.is_active,
            is_super_admin = EXCLUDED.is_super_admin,
            role = EXCLUDED.role,
            updated_at = EXCLUDED.updated_at
        RETURNING id, email, role, is_super_admin
        """,
        (
            "159774df-860f-46f2-b443-7678fab5613f",
            "superadmin@creditpaper.com",
            "$argon2id$v=19$m=65536,t=3,p=4$xKcTSUtOHQsMYO9Irb/j+Q$oBIWWATPIqqOPUv2Xlwo7BTWKgivC+xU5fc6111KgT8",
            True,
            True,
            "email",
            "2026-08-30 22:20:32.580834+05:30",
            "2026-09-02 22:41:58.798754+05:30",
            True,
            "user",
            "2026-09-02 22:41:58.724069+05:30",
            0,
        ),
    )
    print("Inserted:", cur.fetchone())
