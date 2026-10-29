from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import CurrentUser, require_permission
from creditpaper.onboarding.utils.store import (
    load_brand_profile,
    load_section_order,
    mark_step_completed,
)

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.get("/state", response_model=dict)
def get_state(
    tenant_id: str = Depends(resolve_tenant_id),
    _perm: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    import json as _json

    from creditpaper.db import tenant_cursor, tenant_uuid

    tid = tenant_uuid(tenant_id)
    DEFAULT = {"current_step": "brand", "completed_steps": [], "status": "not_started", "updated_at": None}

    with tenant_cursor(str(tid)) as cur:
        # Only existence is needed for the profiles, not their potentially large JSON payloads.
        cur.execute(
            "SELECT (SELECT state FROM onboarding_state WHERE tenant_id = %s), "
            "EXISTS (SELECT 1 FROM brand_profile WHERE tenant_id = %s), "
            "EXISTS (SELECT 1 FROM voice_profile WHERE tenant_id = %s)",
            (tid, tid, tid),
        )
        state_value, has_brand, has_voice = cur.fetchone()

        cur.execute(
            """SELECT id, filename, status, is_credit_paper, approval_detected,
                      confidence, reason, size, content_type, tags,
                      extracted_sections, extracted_style, created_at, updated_at
               FROM onboarding_documents WHERE tenant_id = %s ORDER BY created_at""",
            (tid,),
        )
        doc_rows = cur.fetchall()

    state = state_value if state_value is not None else {**DEFAULT}
    # The documents are already required by the response; count the same rows we return.
    vcount = sum(row[2] == "validated" and row[4] is True for row in doc_rows)

    docs = []
    for row in doc_rows:
        (
            id_,
            filename,
            status,
            is_cp,
            approval,
            conf,
            reason,
            size,
            ctype,
            tags,
            sections,
            style,
            created,
            updated,
        ) = row
        if isinstance(sections, str):
            try:
                sections = _json.loads(sections)
            except Exception:
                sections = []
        if isinstance(style, str):
            try:
                style = _json.loads(style)
            except Exception:
                style = {}
        docs.append(
            {
                "id": str(id_),
                "filename": filename,
                "status": status,
                "is_credit_paper": is_cp,
                "approval_detected": approval,
                "confidence": conf,
                "reason": reason,
                "size": size,
                "content_type": ctype,
                "tags": tags or [],
                "extracted_sections": sections or [],
                "extracted_style": style or {},
                "created_at": created.isoformat() if created else None,
                "updated_at": updated.isoformat() if updated else None,
            }
        )

    completed = set(state.get("completed_steps", []))
    if has_brand:
        completed.add("brand")
    if vcount >= 1:
        completed.add("papers")
    if has_voice:
        completed.add("voice")
        completed.add("preview")

    order = ["brand", "papers", "voice", "preview", "complete"]
    next_step = None
    for s in order:
        if s not in completed:
            next_step = s
            break

    can_access = {
        "brand": True,
        "papers": has_brand,
        "voice": vcount >= 1,
        "preview": has_voice,
        "complete": has_voice,
    }

    return {
        "tenant_id": tenant_id,
        "state": {
            **state,
            "completed_steps": sorted(completed, key=lambda x: order.index(x) if x in order else 99),
            "current_step": next_step or "complete",
        },
        "progress": {
            "has_brand": has_brand,
            "validated_count": vcount,
            "required": 1,
            "has_voice": has_voice,
            "can_access": can_access,
            "next_step": next_step or "complete",
        },
        "documents": docs,
    }


@router.post("/complete", response_model=dict)
def post_complete(
    background_tasks: BackgroundTasks,
    tenant_id: str = Depends(resolve_tenant_id),
    _admin: CurrentUser = Depends(require_permission("settings.update")),  # noqa: B008
):
    # Onboarding never dead-ends (flow B, cold start): a firm with no approved papers finishes
    # on the base profile and learns its voice later from Settings.
    mark_step_completed(tenant_id, "complete")
    # Template creation runs after the step is marked so a transient template
    # failure never blocks onboarding completion; the helper is idempotent
    # (skips when a default already exists) so a retry heals cleanly.
    background_tasks.add_task(_create_default_template_from_onboarding, tenant_id)
    return {"ok": True, "status": "complete"}


def _create_default_template_from_onboarding(tenant_id: str) -> None:
    """Aggregate the tenant's brand + voice + section order into a default
    template row. Idempotent — does nothing if a default already exists.
    Predefined/system template only when tenant has no paper (single/multiple upload path keeps custom).
    """
    from creditpaper.templates.store import template_store

    if template_store.get_default(tenant_id) is not None:
        return  # already exists

    # Determine if tenant has any paper — if none, use predefined/system template
    from creditpaper.db import read_cursor
    from creditpaper.db import tenant_uuid as _tu
    from creditpaper.paper.defaults_store import load_current as _load_current

    try:
        with read_cursor() as cur:
            cur.execute("SELECT count(*) FROM onboarding_documents WHERE tenant_id=%s", (_tu(tenant_id),))
            (doc_count,) = cur.fetchone()
    except Exception:
        doc_count = 1  # on error treat as has paper to avoid wrong predefined

    brand = load_brand_profile(tenant_id) or {}
    sections = load_section_order(tenant_id) or []

    # No paper → predefined/system template from DB (platform_defaults) dynamically
    if doc_count == 0 and not sections:
        try:
            platform = _load_current("template")
            payload = platform.get("payload") or {}
            plat_sections = payload.get("sections") or []
            if plat_sections:
                template_store.create(
                    tenant_id=tenant_id,
                    name="Predefined Template",
                    description="System generated template for tenants without papers",
                    paper_types=[],
                    sections=plat_sections,
                    is_default=True,
                    source="predefined",
                )
                return
        except Exception:
            pass  # fallback to empty onboarding template below

    name = brand.get("brand", {}).get("name") or "Default Template"
    template_store.create(
        tenant_id=tenant_id,
        name=name,
        description="Default template created during onboarding",
        paper_types=[],
        sections=sections,
        is_default=True,
        # The firm's own section order makes this its onboarding template; only a
        # template with nothing tenant-specific in it is "predefined".
        source="onboarding" if doc_count > 0 or sections else "predefined",
    )
