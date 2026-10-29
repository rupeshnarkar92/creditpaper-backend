"""Password history; raw passwords stay in Python and hashes are database-backed."""

from __future__ import annotations

import uuid

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from creditpaper.auth.supabase_admin import (
    password_history_count,
    password_history_hashes,
    record_password_history_procedure,
)

HISTORY_LIMIT = 5

_hasher = PasswordHasher()


def record_password(user_id: str, password: str) -> int:
    """Hash and store the new password; atomically trim old hashes."""
    digest = _hasher.hash(password)
    return record_password_history_procedure(
        event_id=str(uuid.uuid4()),
        user_id=user_id,
        password_hash=digest,
        keep_count=HISTORY_LIMIT,
    )


def password_was_used(user_id: str, password: str) -> bool:
    """Verify the candidate against the user's latest stored Argon2 hashes."""
    try:
        hashes = password_history_hashes(user_id, HISTORY_LIMIT)
    except Exception:
        return False
    for digest in hashes:
        try:
            _hasher.verify(digest, password)
            return True
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            continue
    return False


def history_count(user_id: str) -> int:
    """How many password hashes are currently remembered."""
    return password_history_count(user_id)
