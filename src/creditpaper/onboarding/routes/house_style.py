from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.db import read_cursor

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


def _json(value, default):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return default if value is None else value


def _timestamp(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0


@router.get("/house-style", response_model=dict)
def get_house_style(tenant_id: str = Depends(resolve_tenant_id)):
    """Return the complete House Style screen in one pooler round-trip."""
    with read_cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT profile FROM brand_profile WHERE tenant_id = %s),
                (SELECT profile FROM voice_profile WHERE tenant_id = %s),
                od.id, od.filename, od.status, od.is_credit_paper,
                od.approval_detected, od.confidence, od.reason, od.size,
                od.tags, od.extracted_sections, od.extracted_style,
                od.created_at, od.updated_at
            FROM (SELECT %s::uuid AS tenant_id) t
            LEFT JOIN onboarding_documents od
              ON od.tenant_id = t.tenant_id
            ORDER BY od.created_at
            """,
            (tenant_id, tenant_id, tenant_id),
        )
        rows = cur.fetchall()

    brand = rows[0][0] if rows else None
    voice = rows[0][1] if rows else None
    documents = []
    for row in rows:
        (
            _brand,
            _voice,
            doc_id,
            filename,
            status,
            is_credit_paper,
            approval_detected,
            confidence,
            reason,
            size,
            tags,
            sections,
            style,
            created_at,
            updated_at,
        ) = row
        if doc_id is None:
            continue
        documents.append(
            {
                "id": str(doc_id),
                "filename": filename,
                "status": status,
                "is_credit_paper": is_credit_paper,
                "approval_detected": approval_detected,
                "confidence": confidence,
                "reason": reason,
                "size": size,
                "content_type": None,
                "tags": tags or [],
                "extracted_sections": _json(sections, []),
                "extracted_style": _json(style, {}),
                "created_at": created_at.isoformat() if created_at else None,
                "updated_at": updated_at.isoformat() if updated_at else None,
            }
        )

    validated = [d for d in documents if d["status"] == "validated" and d["approval_detected"]]
    sources = {s.get("document_id") for s in (voice or {}).get("sources", [])}
    current = {d["id"] for d in validated}
    stale_reason = None
    if voice and sources != current:
        stale_reason = "papers_changed"
    elif voice and any(_timestamp(d["updated_at"]) > _timestamp(voice.get("updated_at")) for d in validated):
        stale_reason = "papers_changed"

    return {
        "brand": brand,
        "papers": {
            "documents": documents,
            "validated_count": len(validated),
            "required": 1,
            "max_documents": 3,
            "can_continue": bool(validated),
        },
        "voice_status": {
            "has_voice": voice is not None,
            "voice": voice,
            "validated_count": len(validated),
            "can_generate": bool(validated),
            "generating": False,
            "generation_error": None,
            "corpus_preview": None,
            "stale": stale_reason is not None,
            "stale_reason": stale_reason,
        },
    }
