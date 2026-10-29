"""Password-login database calls. Plaintext passwords never reach the database.

SEC-14: `get_login_user` is gone. It returned `to_jsonb(users)`, hash included,
and nothing called it once Supabase owned authentication — so the
`get_login_user(TEXT)` function is dropped in `nn_0048_drop_get_login_user`.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, text
from sqlalchemy.ext.asyncio import AsyncSession

from creditpaper.auth.models.user import User


def user_from_payload(payload: dict[str, Any]) -> User:
    """Build a detached user; updates are performed only by the login function."""
    values = {
        column.name: payload[column.name] for column in User.__table__.columns if column.name in payload
    }
    for column in User.__table__.columns:
        if isinstance(column.type, DateTime) and isinstance(values.get(column.name), str):
            values[column.name] = datetime.fromisoformat(values[column.name])
    return User(**values)


async def finish_login(
    db: AsyncSession, user: User, *, verified: bool, max_attempts: int, lock_seconds: int
) -> dict[str, Any]:
    result = await db.execute(
        text(
            "SELECT finish_password_login(:user_id, :expected_hash, :verified, :max_attempts, :lock_seconds)"
        ),
        {
            "user_id": user.id,
            "expected_hash": user.password_hash,
            "verified": verified,
            "max_attempts": max_attempts,
            "lock_seconds": lock_seconds,
        },
    )
    payload = result.scalar_one()
    # Commit failures must propagate before cookies or tokens are issued.
    await db.commit()
    return payload
