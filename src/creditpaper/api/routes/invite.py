"""Invite accept flow - validate token and set password.

All invitation state lives in the stored procedures from migration rn_003
(``validate_invitation`` / ``accept_invitation``); this router only invokes
them, pre-hashes the password for the local profile column, and maps statuses
to the HTTP copy the AcceptInvitation page renders.
"""

from __future__ import annotations

import base64 as _b64

# Local PBKDF2 for public.users.password_hash - login verifies against the
# auth.users bcrypt hash written by the stored procedure.
import hashlib
import os as _os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from creditpaper.db import cursor


def _hash_password(password: str) -> str:
    salt = _os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return f"pbkdf2_sha256$100000${_b64.b64encode(salt).decode()}${_b64.b64encode(dk).decode()}"


router = APIRouter(prefix="/auth", tags=["auth"])

_STATUS_MESSAGES = {
    "INVALID": "This invitation link is not valid.",
    "ALREADY_USED": "This invitation has already been used. Sign in instead.",
    "EXPIRED": "This invitation has expired. Ask your admin to send a new one.",
}


class SetPasswordRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)


def _result_or_400(row: tuple | None) -> dict:
    result = row[0] if row else None
    if not isinstance(result, dict):
        raise HTTPException(status_code=400, detail=_STATUS_MESSAGES["INVALID"])
    return result


@router.get("/validate-invite")
def validate_invite(token: str):
    with cursor() as cur:
        cur.execute("SELECT validate_invitation(%s)", (token,))
        result = _result_or_400(cur.fetchone())
    if result.get("status") != "OK":
        detail = _STATUS_MESSAGES.get(result.get("status", ""), _STATUS_MESSAGES["INVALID"])
        raise HTTPException(status_code=400, detail=detail)
    return {"email": result.get("email"), "role": result.get("role")}


@router.post("/set-password")
def set_password(req: SetPasswordRequest):
    password_hash = _hash_password(req.password)
    with cursor() as cur:
        cur.execute(
            "SELECT accept_invitation(%s, %s, %s)",
            (req.token, req.password, password_hash),
        )
        result = _result_or_400(cur.fetchone())

    status = result.get("status")
    if status == "OK":
        return {"message": "Account created. You can now sign in."}
    if status == "EMAIL_EXISTS":
        raise HTTPException(
            status_code=400, detail="An account with this email already exists. Sign in instead."
        )
    if status == "ROLE_NOT_FOUND":
        raise HTTPException(status_code=400, detail=f"Role not found: {result.get('role')}")
    detail = _STATUS_MESSAGES.get(status or "", _STATUS_MESSAGES["INVALID"])
    raise HTTPException(status_code=400, detail=detail)
