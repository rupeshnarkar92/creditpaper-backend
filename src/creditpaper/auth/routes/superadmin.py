import asyncio
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from creditpaper.auth.config import get_settings
from creditpaper.auth.database import get_db
from creditpaper.auth.dependencies import CurrentUser, get_current_user
from creditpaper.auth.models.activity_log import ActivityLog
from creditpaper.auth.models.domain import Domain
from creditpaper.auth.models.invitation import Invitation
from creditpaper.auth.models.user import User
from creditpaper.auth.supabase_invite import send_invite_email
from creditpaper.auth.utils.public_domains import is_public_domain
from creditpaper.billing.plans import CURRENCY, PLANS
from creditpaper.creditops import store
from creditpaper.db import cursor
from creditpaper.guardrails.store import guardrail_store
from creditpaper.llm.providers import current_provider
from creditpaper.templates.store import template_store

router = APIRouter(prefix="/superadmin", tags=["superadmin"])
settings = get_settings()


def require_superadmin(user: CurrentUser) -> CurrentUser:
    if not user.is_super_admin:
        raise HTTPException(status_code=403, detail="Not authorized.")
    return user


def _pagination(total: int, page: int, limit: int) -> dict:
    """Page metadata every list endpoint shares.

    `pages` used to be missing while the frontend rendered a pager from it, so
    every superadmin list showed "Page 1 of" nothing and page 2 was
    unreachable.
    """
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "pages": max(1, -(-total // limit)),
    }


async def _record_audit(
    db: AsyncSession,
    actor: CurrentUser,
    action: str,
    description: str,
    domain_id: str | None,
) -> None:
    """Record a superadmin mutation in `activity_log`.

    Best-effort, in its own transaction: an audit write must never turn a
    successful mutation into a 500. `domain_id` is NOT NULL on the table, so
    actions without a domain are skipped rather than fabricated. The actor is
    carried in the description because their Supabase id has no guaranteed row
    in the local `users` table (FK would reject it).
    """
    if not domain_id:
        return
    try:
        db.add(
            ActivityLog(
                domain_id=str(domain_id),
                user_id=None,
                action=action,
                description=f"{actor.email}: {description}",
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()


@router.get("/me")
async def superadmin_me(user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    require_superadmin(user)
    return {
        "id": user.id,
        "email": user.email,
        "is_super_admin": user.is_super_admin,
        "role": user.role,
    }


@router.get("/users")
async def list_users(
    search: str = "",
    role: str = "",
    status: str = "",
    domain_id: str = "",
    order_by: str = Query("-created_at", max_length=40),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    query = select(User)
    count_query = select(func.count()).select_from(User)

    if search:
        search_filter = or_(
            User.email.ilike(f"%{search}%"),
            User.role.ilike(f"%{search}%"),
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)
    if role:
        query = query.where(User.role == role)
        count_query = count_query.where(User.role == role)
    if status == "active":
        query = query.where(User.is_active.is_(True))
        count_query = count_query.where(User.is_active.is_(True))
    elif status == "inactive":
        query = query.where(User.is_active.is_(False))
        count_query = count_query.where(User.is_active.is_(False))
    # Scopes the list to one tenant, for the domain detail page. users.domain_id
    # is a real FK to domains.id, so this is a predicate, not a join.
    if domain_id:
        query = query.where(User.domain_id == domain_id)
        count_query = count_query.where(User.domain_id == domain_id)

    total = (await db.execute(count_query)).scalar() or 0
    offset = (page - 1) * limit
    descending = order_by.startswith("-")
    sort_key = order_by[1:] if descending else order_by
    sort_columns = {
        "email": User.email,
        "role": User.role,
        "status": User.is_active,
        "last_login": User.last_login,
        "created_at": User.created_at,
    }
    sort_column = sort_columns.get(sort_key, User.created_at)
    if sort_key == "domain":
        query = query.outerjoin(Domain, User.domain_id == Domain.id)
        sort_column = Domain.domain
    query = query.order_by(
        (desc(sort_column) if descending else asc(sort_column)),
        User.id.desc(),
    ).offset(offset).limit(limit)
    result = await db.execute(query)
    users = result.scalars().all()

    # One domain lookup per page, not per row — round trips over the Supabase
    # pooler dominate latency (46 users x 1 query = 15s before batching).
    domain_ids = {u.domain_id for u in users if u.domain_id}
    domain_names: dict[str, str] = {}
    if domain_ids:
        domain_names = {
            row[0]: row[1]
            for row in (
                await db.execute(select(Domain.id, Domain.domain).where(Domain.id.in_(domain_ids)))
            ).all()
        }

    user_list = []
    for u in users:
        user_list.append(
            {
                "id": u.id,
                "email": u.email,
                "role": u.role,
                "is_active": u.is_active,
                "is_email_verified": u.is_email_verified,
                "auth_provider": u.auth_provider,
                "domain_id": u.domain_id,
                "domain_name": domain_names.get(u.domain_id),
                "is_super_admin": u.is_super_admin,
                "created_at": str(u.created_at),
                "last_login": str(u.last_login) if u.last_login else None,
            }
        )

    return {
        "users": user_list,
        **_pagination(total, page, limit),
    }


@router.post("/logout")
async def superadmin_logout(
    response: Response,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
):
    """Clear the session cookies.

    Gated (was open to any caller): an unauthenticated POST could expire a
    victim's cookies — a logout CSRF. Only the signed-in user's own cookies
    are touched, so nothing about the caller's identity changes here.
    """
    require_superadmin(user)
    response.delete_cookie(key="access_token", path="/")
    response.delete_cookie(key="creditpaper_session", path="/")
    return {"message": "Logged out."}


# ---------------------------------------------------------------------------
# User detail / edit / delete / invite
# ---------------------------------------------------------------------------


class UpdateUserRequest(BaseModel):
    email: EmailStr | None = None
    role: str | None = None
    is_active: bool | None = None


class SuperAdminInviteRequest(BaseModel):
    email: EmailStr
    role: str = "maker"
    domain_id: str | None = None


@router.get("/users/{user_id}")
async def get_user_detail(
    user_id: str,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    domain_name = None
    if target.domain_id:
        d_result = await db.execute(select(Domain).where(Domain.id == target.domain_id))
        domain_obj = d_result.scalar_one_or_none()
        if domain_obj:
            domain_name = domain_obj.domain

    return {
        "id": target.id,
        "email": target.email,
        "role": target.role,
        "is_active": target.is_active,
        "is_email_verified": target.is_email_verified,
        "auth_provider": target.auth_provider,
        "domain_id": target.domain_id,
        "domain_name": domain_name,
        "is_super_admin": target.is_super_admin,
        "created_at": str(target.created_at),
        "updated_at": str(target.updated_at),
        "last_login": str(target.last_login) if target.last_login else None,
    }


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    body: UpdateUserRequest,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    if target.is_super_admin:
        raise HTTPException(status_code=400, detail="Cannot modify a super admin account.")

    if body.email is not None:
        existing = await db.execute(select(User).where(User.email == body.email, User.id != user_id))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Email already in use.")
        target.email = body.email

    if body.role is not None:
        allowed_roles = {"tenant_admin", "maker", "checker", "viewer"}
        if body.role not in allowed_roles:
            allowed_str = ", ".join(sorted(allowed_roles))
            raise HTTPException(status_code=400, detail=f"Invalid role. Allowed: {allowed_str}")
        target.role = body.role

    if body.is_active is not None:
        target.is_active = body.is_active

    await db.commit()
    await _record_audit(
        db,
        user,
        "user.updated",
        f"Updated user {target.email} (role={body.role!r}, active={body.is_active!r})",
        target.domain_id,
    )

    return {"message": "User updated.", "user_id": target.id}


@router.delete("/users/{user_id}")
async def deactivate_user(
    user_id: str,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    if target.is_super_admin:
        raise HTTPException(status_code=400, detail="Cannot deactivate a super admin account.")

    target.is_active = False
    await db.commit()
    await _record_audit(db, user, "user.deactivated", f"Deactivated user {target.email}", target.domain_id)

    return {"message": "User deactivated.", "user_id": target.id}


@router.post("/users/{user_id}/send-set-password")
async def send_set_password_email(
    user_id: str,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    if not target.email:
        raise HTTPException(status_code=400, detail="User does not have a valid email address.")

    if not target.is_active:
        raise HTTPException(status_code=400, detail="Cannot send set-password email to an inactive user.")

    if target.auth_provider == "google":
        raise HTTPException(
            status_code=400,
            detail="This user signed in via Google and does not need a password.",
        )

    raise HTTPException(
        status_code=400,
        detail=(
            "Password setup email is unavailable in this environment: "
            "no email service is configured. Use 'Forgot Password' on the sign-in page."
        ),
    )


@router.post("/users")
async def invite_user(
    body: SuperAdminInviteRequest,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    if is_public_domain(body.email):
        raise HTTPException(
            status_code=400,
            detail="Cannot invite personal email addresses (Gmail, Yahoo, Outlook, etc.).",
        )

    existing_user = await db.execute(select(User).where(User.email == body.email))
    if existing_user.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="User already exists.")

    existing_invite = await db.execute(
        select(Invitation).where(
            Invitation.email == body.email,
            ~Invitation.accepted,
            Invitation.expires_at > datetime.now(UTC),
        )
    )
    if existing_invite.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Invitation already pending for this email.")

    token = secrets.token_urlsafe(48)
    invitation = Invitation(
        email=body.email,
        domain_id=body.domain_id,
        role=body.role,
        token=token,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db.add(invitation)
    await db.flush()

    invited = send_invite_email(body.email, body.role, token, "CreditPaper", user.email)
    await db.commit()
    await _record_audit(
        db,
        user,
        "user.invited",
        f"Invited {body.email} as {body.role}",
        body.domain_id,
    )

    if not invited:
        return {
            "message": (
                "Invitation created. No email service is configured — "
                "the invitee can sign up with this address directly."
            ),
            "invitation_id": invitation.id,
        }
    return {"message": "Invitation sent.", "invitation_id": invitation.id}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def _safe_growth(current: int, previous: int) -> float:
    if previous == 0:
        return 0.0
    return round(((current - previous) / previous) * 100, 1)


def _check_db_health() -> dict:
    try:
        with cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return {"name": "database", "status": "healthy"}
    except Exception:
        return {"name": "database", "status": "unavailable"}


def _check_api_health() -> dict:
    return {"name": "api", "status": "healthy"}


def _overall_health(services: list[dict]) -> str:
    statuses = {s["status"] for s in services}
    if statuses == {"healthy"}:
        return "healthy"
    if "unavailable" in statuses:
        return "unavailable"
    return "degraded"


def _stage_category(stage: str | None) -> str | None:
    """Map a deal state onto the four dashboard trend categories.

    State-machine vocabulary first (deals/state_machine.py); the substring
    fallback only serves pre-rn_009 stage strings. PENDING_APPROVAL is
    in_review, not approved: awaiting a checker is not an approval.
    """
    if not stage:
        return None
    s = stage.upper()
    if s == "ISSUED":
        return "issued"
    if s == "APPROVED":
        return "approved"
    if s in {
        "PAPER_GENERATED",
        "PAPER_EDITING",
        "PAPER_REVIEW",
        "PENDING_APPROVAL",
        "FACT_REVIEW",
    }:
        return "in_review"
    legacy = stage.lower()
    if "issu" in legacy or "publish" in legacy:
        return "issued"
    if "approv" in legacy:
        return "approved"
    if "review" in legacy or "check" in legacy or "fidelity" in legacy:
        return "in_review"
    return None


def _deal_trend(start: datetime, end: datetime) -> list[dict]:
    """Real daily credit-paper trend from `deals` (generated) and `deal_events`
    (stage transitions). Zero-filled so the four categories always align on the
    same date axis."""
    generated: dict[str, int] = {}
    events: dict[str, dict[str, int]] = {}
    try:
        with cursor() as cur:
            cur.execute(
                """
                SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*)
                FROM deals
                WHERE created_at >= %s AND created_at < %s
                GROUP BY 1 ORDER BY 1
                """,
                [start, end],
            )
            for r in cur.fetchall():
                generated[str(r[0])] = r[1]

            # deal_events is append-only history that stays empty in practice;
            # current_state + state_changed_at is the readable transition record
            # (US_drop_deals_status_stage dropped deals.stage/status for the
            # same reason).
            cur.execute(
                """
                SELECT (state_changed_at AT TIME ZONE 'UTC')::date AS day,
                       current_state, count(*)
                FROM deals
                WHERE state_changed_at >= %s AND state_changed_at < %s
                GROUP BY 1, 2 ORDER BY 1
                """,
                [start, end],
            )
            for r in cur.fetchall():
                cat = _stage_category(r[1])
                key = str(r[0])
                bucket = events.setdefault(key, {"issued": 0, "approved": 0, "in_review": 0})
                if cat:
                    bucket[cat] += r[2]
    except Exception:
        generated = {}
        events = {}

    points: list[dict] = []
    day = start.date()
    while day <= end.date():
        key = str(day)
        g = generated.get(key, 0)
        ev = events.get(key)
        issued = ev["issued"] if ev else 0
        approved = ev["approved"] if ev else 0
        in_review = ev["in_review"] if ev else 0
        points.append(
            {
                "date": day.isoformat(),
                "generated": g,
                "approved": approved,
                "in_review": in_review,
                "issued": issued,
            }
        )
        day += timedelta(days=1)
    return points


async def _user_counts_by_domain(db: AsyncSession, domain_ids: list[str]) -> dict[str, int]:
    """User counts for many domains in ONE grouped query (was one per domain)."""
    if not domain_ids:
        return {}
    rows = (
        await db.execute(
            select(User.domain_id, func.count())
            .where(User.domain_id.in_(domain_ids))
            .group_by(User.domain_id)
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _recent_activity(db: AsyncSession, start: datetime, limit: int = 6) -> list[dict]:
    """Latest activity-log rows within the period, hydrated with user/domain names."""
    query = (
        select(ActivityLog)
        .where(ActivityLog.created_at >= start)
        .order_by(ActivityLog.created_at.desc())
        .limit(limit)
    )
    logs = (await db.execute(query)).scalars().all()

    # Hydrate every row's user/domain names from two lookups, not 2 per row.
    user_ids = {log.user_id for log in logs if log.user_id}
    domain_ids = {log.domain_id for log in logs if log.domain_id}
    emails: dict[str, str] = {}
    domain_names: dict[str, str] = {}
    if user_ids:
        emails = {
            row[0]: row[1]
            for row in (await db.execute(select(User.id, User.email).where(User.id.in_(user_ids)))).all()
        }
    if domain_ids:
        domain_names = {
            row[0]: row[1]
            for row in (
                await db.execute(select(Domain.id, Domain.domain).where(Domain.id.in_(domain_ids)))
            ).all()
        }

    out = []
    for log in logs:
        out.append(
            {
                "id": log.id,
                "action": log.action,
                "description": log.description,
                "user_email": emails.get(log.user_id),
                "domain": domain_names.get(log.domain_id),
                "created_at": str(log.created_at),
            }
        )
    return out


async def _preview_users(db: AsyncSession, limit: int = 5) -> list[dict]:
    """Same query/data source as the full Super Admin Users page."""
    result = await db.execute(select(User).order_by(User.created_at.desc()).limit(limit))
    users = result.scalars().all()
    domain_ids = {u.domain_id for u in users if u.domain_id}
    domain_names: dict[str, str] = {}
    if domain_ids:
        domain_names = {
            row[0]: row[1]
            for row in (
                await db.execute(select(Domain.id, Domain.domain).where(Domain.id.in_(domain_ids)))
            ).all()
        }
    out = []
    for u in users:
        out.append(
            {
                "id": u.id,
                "email": u.email,
                "role": u.role,
                "is_active": u.is_active,
                "domain_name": domain_names.get(u.domain_id),
                "last_login": str(u.last_login) if u.last_login else None,
                "created_at": str(u.created_at),
            }
        )
    return out


async def _preview_domains(db: AsyncSession, limit: int = 5) -> list[dict]:
    """Same query/data source as the full Super Admin Domains page."""
    result = await db.execute(select(Domain).order_by(Domain.created_at.desc()).limit(limit))
    domains = result.scalars().all()
    counts = await _user_counts_by_domain(db, [d.id for d in domains])
    out = []
    for d in domains:
        users = counts.get(d.id, 0)
        out.append(
            {
                "id": d.id,
                "domain": d.domain,
                "company": d.company or "",
                "admin_email": d.admin_email,
                "users": users,
                "status": d.status,
                "created_at": str(d.created_at),
            }
        )
    return out


async def _preview_subscribers(db: AsyncSession, limit: int = 5) -> list[dict]:
    """Same query/data source as the Super Admin Subscribers page (tenant-centric)."""
    result = await db.execute(select(Domain).order_by(Domain.created_at.desc()).limit(limit))
    domains = result.scalars().all()
    counts = await _user_counts_by_domain(db, [d.id for d in domains])
    out = []
    for d in domains:
        users = counts.get(d.id, 0)
        out.append(
            {
                "id": d.id,
                "domain": d.domain,
                "company": d.company or "",
                "plan": None,
                "users": users,
                "status": d.status,
                "mrr": None,
                "created_at": str(d.created_at),
            }
        )
    return out


def _load_pending_papers(page: int = 1, limit: int = 10) -> tuple[list[dict], int]:
    """Latest in-flight deals for the dashboard, from deals.current_state.

    `deals.stage`/`deals.status` were dropped (US_drop_deals_status_stage);
    current_state is the state machine's single source. Terminal states are
    excluded. Blocking psycopg — callers run this via asyncio.to_thread.
    """
    papers: list[dict] = []
    total = 0
    try:
        with cursor() as cur:
            cur.execute(
                f"""
                SELECT de.id, de.name, de.deal_type, de.borrower, de.current_state,
                       de.created_at, de.updated_at, u.email AS owner_email
                FROM deals de
                LEFT JOIN users u ON u.id = de.owner_id::text
                WHERE {_PENDING_STAGE_SQL}
                ORDER BY de.updated_at DESC NULLS LAST
                LIMIT %s OFFSET %s
                """,
                (limit, (page - 1) * limit),
            )
            for r in cur.fetchall():
                papers.append(
                    {
                        "id": str(r[0]),
                        "name": r[1] or "",
                        "deal_type": r[2] or "",
                        "borrower": r[3] or "",
                        "stage": r[4] or "",
                        "created_at": r[5].isoformat() if r[5] else None,
                        "updated_at": r[6].isoformat() if r[6] else None,
                        "owner_email": r[7],
                        "due_date": None,
                        "priority": None,
                    }
                )

            cur.execute(f"SELECT count(*) FROM deals WHERE {_PENDING_STAGE_SQL}")
            total = cur.fetchone()[0] or 0
    except Exception:
        pass
    return papers, total


@router.get("/dashboard")
async def get_dashboard(
    days: int = Query(30, ge=1, le=365),
    pending_page: int = Query(1, ge=1),
    pending_limit: int = Query(10, ge=1, le=50),
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    now = datetime.now(UTC)
    period_start = now - timedelta(days=days)
    prev_period_start = period_start - timedelta(days=days)

    # ---- Users / Domains / Subscribers: one FILTER-aggregate query each ----
    # Ten sequential counts here made this page take ~30s over the pooler.
    total_users, users_current, users_previous = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(User.created_at >= period_start),
                func.count().filter(
                    (User.created_at >= prev_period_start) & (User.created_at < period_start)
                ),
            ).select_from(User)
        )
    ).one()

    (
        total_domains,
        domains_current,
        domains_previous,
        active_domains,
    ) = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(Domain.created_at >= period_start),
                func.count().filter(
                    (Domain.created_at >= prev_period_start) & (Domain.created_at < period_start)
                ),
                func.count().filter(Domain.status == "active"),
            ).select_from(Domain)
        )
    ).one()

    _has_domain = User.domain_id.isnot(None)
    total_subscribers, subscribers_current, subscribers_previous = (
        await db.execute(
            select(
                func.count().filter(_has_domain),
                func.count().filter(_has_domain & (User.created_at >= period_start)),
                func.count().filter(
                    _has_domain & (User.created_at >= prev_period_start) & (User.created_at < period_start)
                ),
            ).select_from(User)
        )
    ).one()

    # ---- Deals / Credit Papers (psycopg — harness domain) ----
    # Raw psycopg blocks the event loop; run both off-loop.
    pending_papers, total_pending = await asyncio.to_thread(
        _load_pending_papers, pending_page, pending_limit
    )

    # ---- Credit Paper Trend (real daily aggregation) ----
    trend = await asyncio.to_thread(_deal_trend, period_start, now)

    # ---- Credits Usage: no persistent usage exists (session-scoped ledger) ----
    credits_usage = {
        "total": 0,
        "note": "AI usage is session-scoped and not persisted to database.",
        "categories": [
            {"key": "document_processing", "label": "Document Processing", "value": 0},
            {"key": "paper_generation", "label": "Paper Generation", "value": 0},
            {"key": "ai_analysis", "label": "AI Analysis", "value": 0},
            {"key": "other", "label": "Other", "value": 0},
        ],
    }

    # ---- Recent Activity (date-scoped) ----
    recent_activity = await _recent_activity(db, period_start, limit=6)

    # ---- Previews (same data sources as the full pages) ----
    preview_users = await _preview_users(db, limit=5)
    preview_domains = await _preview_domains(db, limit=5)
    preview_subscribers = await _preview_subscribers(db, limit=5)

    # Same queries as the totals above — reuse instead of re-asking the pooler.
    total_preview_users = total_users
    total_preview_domains = total_domains
    total_preview_subscribers = total_subscribers

    return {
        "users": {
            "total": total_users,
            "current_period": users_current,
            "previous_period": users_previous,
            "growth_percentage": _safe_growth(users_current, users_previous),
        },
        "domains": {
            "total": total_domains,
            "current_period": domains_current,
            "previous_period": domains_previous,
            "growth_percentage": _safe_growth(domains_current, domains_previous),
        },
        "subscribers": {
            "total": total_subscribers,
            "current_period": subscribers_current,
            "previous_period": subscribers_previous,
            "growth_percentage": _safe_growth(subscribers_current, subscribers_previous),
        },
        "credit_papers": {
            "pending": total_pending,
        },
        "credit_paper_trend": {
            "points": trend,
            "period_start": period_start.isoformat(),
            "now": now.isoformat(),
        },
        "credits_usage": credits_usage,
        "recent_activity": recent_activity,
        "preview_users": {
            "rows": preview_users,
            "total": total_preview_users,
        },
        "preview_domains": {
            "rows": preview_domains,
            "total": total_preview_domains,
        },
        "preview_subscribers": {
            "rows": preview_subscribers,
            "total": total_preview_subscribers,
            "summary": {
                "total_tenants": total_domains,
                "active": active_domains,
                "trial": None,
                "expired": None,
                "total_mrr": None,
                "arr": None,
            },
        },
        "pending_credit_papers": pending_papers,
        "date_range": {
            "days": days,
            "period_start": period_start.isoformat(),
            "now": now.isoformat(),
        },
    }


# ---------------------------------------------------------------------------
# Domains / Customers
# ---------------------------------------------------------------------------


def _count_deals_for_domain(domain_id: str) -> int:
    """Real credit-paper count: deals owned by users who belong to this domain.

    Synchronous on purpose: it runs on the sync pool via `asyncio.to_thread`,
    and declaring it `async def` used to hand `to_thread` a coroutine object
    that nobody awaited — the domain detail response 500ed in the encoder.
    """
    try:
        with cursor() as cur:
            # deals.owner_id is uuid, users.id is varchar — cast on the join
            # (the uncast form raises UndefinedFunction and always fell to 0).
            cur.execute(
                """
                SELECT count(*) FROM deals de
                JOIN users u ON u.id = de.owner_id::text
                WHERE u.domain_id = %s
                """,
                [domain_id],
            )
            return cur.fetchone()[0] or 0
    except Exception:
        return 0


def _paper_counts_by_domain(domain_ids: list[str]) -> dict[str, int]:
    """Per-domain credit-paper counts in ONE query (was one round trip each).

    deals.owner_id is uuid, users.id is varchar — cast on the join. Runs on
    the sync pool off-loop: the asyncpg dialect cannot expand IN-lists (it
    binds the whole list as one `record` operand). Failures degrade to 0 like
    the single-domain helper instead of 500ing the page.
    """
    if not domain_ids:
        return {}
    try:
        with cursor() as cur:
            cur.execute(
                """
                SELECT u.domain_id, count(*)
                FROM deals de
                JOIN users u ON u.id = de.owner_id::text
                WHERE u.domain_id = ANY(%s)
                GROUP BY u.domain_id
                """,
                [domain_ids],
            )
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception:
        return {}


async def _domain_row(db: AsyncSession, d: Domain) -> dict:
    """Build the full domain response row with real per-domain counts."""
    users = (
        await db.execute(select(func.count()).select_from(User).where(User.domain_id == d.id))
    ).scalar() or 0
    papers = await asyncio.to_thread(_count_deals_for_domain, d.id)
    return {
        "id": d.id,
        "domain": d.domain,
        "company": d.company or "",
        "admin_email": d.admin_email,
        "users": users,
        "subscribers": users,
        "credit_papers": papers,
        "status": d.status,
        "created_at": str(d.created_at),
    }


async def _domain_summary(db: AsyncSession) -> dict:
    """Real summary stats computed from the domains/users tables.

    One FILTER-aggregate query per table — two round trips over the pooler
    instead of ten sequential counts.
    """
    now_utc = datetime.now(UTC)
    month_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_month_start = (month_start - timedelta(days=1)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    (
        total_domains,
        active,
        suspended,
        domains_created_this_month,
        domains_created_last_month,
        domains_before_month,
    ) = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(Domain.status == "active"),
                func.count().filter(Domain.status == "suspended"),
                func.count().filter(Domain.created_at >= month_start),
                func.count().filter(
                    (Domain.created_at >= prev_month_start) & (Domain.created_at < month_start)
                ),
                func.count().filter(Domain.created_at < month_start),
            ).select_from(Domain)
        )
    ).one()

    _has_domain = User.domain_id.isnot(None)
    (
        total_users,
        users_added_this_month,
        users_added_last_month,
        users_before_month,
    ) = (
        await db.execute(
            select(
                func.count().filter(_has_domain),
                func.count().filter(_has_domain & (User.created_at >= month_start)),
                func.count().filter(
                    _has_domain & (User.created_at >= prev_month_start) & (User.created_at < month_start)
                ),
                func.count().filter(_has_domain & (User.created_at < month_start)),
            ).select_from(User)
        )
    ).one()

    avg_users_per_domain = round(total_users / total_domains, 1) if total_domains else 0.0
    avg_domains_before_month = (
        round(users_before_month / domains_before_month, 1) if domains_before_month else None
    )

    return {
        "total_domains": total_domains,
        "total_users": total_users,
        "active": active,
        "suspended": suspended,
        "avg_users_per_domain": avg_users_per_domain,
        "mrr": None,
        "period": {
            "domains_this_month": domains_created_this_month,
            "domains_last_month": domains_created_last_month,
            "users_this_month": users_added_this_month,
            "users_last_month": users_added_last_month,
            "avg_users_per_domain_prev_month": avg_domains_before_month,
        },
    }


@router.get("/domains")
async def list_domains(
    search: str = "",
    status: str = "",
    order_by: str = Query("-created_at", max_length=40),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    query = select(Domain)
    count_query = select(func.count()).select_from(Domain)

    if search:
        clause = (
            Domain.domain.ilike(f"%{search}%")
            | Domain.admin_email.ilike(f"%{search}%")
            | Domain.company.ilike(f"%{search}%")
        )
        query = query.where(clause)
        count_query = count_query.where(clause)

    if status:
        query = query.where(Domain.status == status)
        count_query = count_query.where(Domain.status == status)

    total = (await db.execute(count_query)).scalar() or 0
    offset = (page - 1) * limit
    descending = order_by.startswith("-")
    sort_key = order_by[1:] if descending else order_by
    sort_column = {
        "domain": Domain.domain,
        "company": Domain.company,
        "admin_email": Domain.admin_email,
        "status": Domain.status,
        "created_at": Domain.created_at,
    }.get(sort_key, Domain.created_at)
    query = query.order_by(
        (desc(sort_column) if descending else asc(sort_column)),
        Domain.id.desc(),
    ).offset(offset).limit(limit)
    result = await db.execute(query)
    domains = result.scalars().all()

    # Two grouped queries for the whole page instead of 2N per-row queries.
    domain_ids = [d.id for d in domains]
    user_counts = await _user_counts_by_domain(db, domain_ids)
    paper_counts = await asyncio.to_thread(_paper_counts_by_domain, domain_ids)

    domain_list = [
        {
            "id": d.id,
            "domain": d.domain,
            "company": d.company or "",
            "admin_email": d.admin_email,
            "users": user_counts.get(d.id, 0),
            "subscribers": user_counts.get(d.id, 0),
            "credit_papers": paper_counts.get(d.id, 0),
            "status": d.status,
            "created_at": str(d.created_at),
        }
        for d in domains
    ]

    return {
        "domains": domain_list,
        "summary": await _domain_summary(db),
        **_pagination(total, page, limit),
    }


@router.get("/domains/summary")
async def get_domains_summary(
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)
    return await _domain_summary(db)


@router.post("/domains")
async def create_domain(
    body: dict,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    domain = (body.get("domain") or "").strip().lower()
    admin_email = (body.get("admin_email") or "").strip()
    company = (body.get("company") or "").strip()
    status = (body.get("status") or "active").strip()

    if not domain or not admin_email:
        raise HTTPException(status_code=422, detail="Domain and admin email are required.")

    existing = (await db.execute(select(Domain).where(Domain.domain == domain))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="This domain is already registered.")

    if status not in ("active", "suspended"):
        status = "active"

    new_domain = Domain(
        domain=domain,
        admin_email=admin_email,
        company=company or None,
        status=status,
    )
    db.add(new_domain)
    await db.commit()
    await db.refresh(new_domain)
    await _record_audit(db, user, "domain.created", f"Created domain {domain}", new_domain.id)

    return await _domain_row(db, new_domain)


@router.patch("/domains/{domain_id}")
async def update_domain(
    domain_id: str,
    body: dict,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    result = await db.execute(select(Domain).where(Domain.id == domain_id))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found.")

    for field in ("domain", "admin_email", "company"):
        if field in body and body[field] is not None:
            value = str(body[field]).strip()
            if field == "domain":
                value = value.lower()
                dup = await db.execute(select(Domain).where(Domain.domain == value, Domain.id != domain_id))
                if dup.scalar_one_or_none():
                    raise HTTPException(status_code=409, detail="This domain is already registered.")
            setattr(domain, field, None if field == "company" and not value else value)

    if body.get("status") in ("active", "suspended"):
        domain.status = body["status"]

    await db.commit()
    await db.refresh(domain)
    await _record_audit(db, user, "domain.updated", f"Updated domain {domain.domain}", domain.id)
    return await _domain_row(db, domain)


@router.delete("/domains/{domain_id}")
async def delete_domain(
    domain_id: str,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    result = await db.execute(select(Domain).where(Domain.id == domain_id))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found.")

    member_count = (
        await db.execute(select(func.count()).select_from(User).where(User.domain_id == domain_id))
    ).scalar() or 0
    if member_count > 0:
        raise HTTPException(
            status_code=409, detail="Cannot delete a domain that still has users. Deactivate users first."
        )

    domain_name = domain.domain
    await db.delete(domain)
    await db.commit()
    await _record_audit(db, user, "domain.deleted", f"Deleted domain {domain_name}", domain_id)
    return {"id": domain_id, "deleted": True}


@router.get("/domains/{domain_id}")
async def get_domain(
    domain_id: str,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    result = await db.execute(select(Domain).where(Domain.id == domain_id))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found.")

    row = await _domain_row(db, domain)

    # One FILTER-aggregate instead of three counts: a detail page that is
    # already slow over the pooler should not pay three more round trips.
    invitations_total, invitations_pending, invitations_accepted = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(Invitation.accepted.is_(False)),
                func.count().filter(Invitation.accepted.is_(True)),
            )
            .select_from(Invitation)
            .where(Invitation.domain_id == domain_id)
        )
    ).one()

    row["invitations"] = {
        "total": invitations_total or 0,
        "pending": invitations_pending or 0,
        "accepted": invitations_accepted or 0,
    }
    return row


# ---------------------------------------------------------------------------
# Subscribers (users with a domain)
# ---------------------------------------------------------------------------


@router.get("/subscribers")
async def list_subscribers(
    search: str = "",
    status: str = "",
    order_by: str = Query("-created_at", max_length=40),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    query = select(Domain)
    count_query = select(func.count()).select_from(Domain)

    if search:
        clause = (
            Domain.domain.ilike(f"%{search}%")
            | Domain.admin_email.ilike(f"%{search}%")
            | Domain.company.ilike(f"%{search}%")
        )
        query = query.where(clause)
        count_query = count_query.where(clause)

    if status:
        query = query.where(Domain.status == status)
        count_query = count_query.where(Domain.status == status)

    total = (await db.execute(count_query)).scalar() or 0
    offset = (page - 1) * limit
    descending = order_by.startswith("-")
    sort_key = order_by[1:] if descending else order_by
    sort_column = {
        "domain": Domain.domain,
        "company": Domain.company,
        "status": Domain.status,
        "created_at": Domain.created_at,
    }.get(sort_key, Domain.created_at)
    query = query.order_by(
        (desc(sort_column) if descending else asc(sort_column)),
        Domain.id.desc(),
    ).offset(offset).limit(limit)
    result = await db.execute(query)
    domains = result.scalars().all()

    # One grouped query for the page's user counts, not one per domain.
    counts = await _user_counts_by_domain(db, [d.id for d in domains])

    rows = []
    for d in domains:
        users = counts.get(d.id, 0)
        rows.append(
            {
                "id": d.id,
                "domain": d.domain,
                "company": d.company or "",
                "admin_email": d.admin_email,
                "plan": None,
                "users": users,
                "status": d.status,
                "renewal_date": None,
                "mrr": None,
                "created_at": str(d.created_at),
            }
        )

    total_tenants, active = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(Domain.status == "active"),
            ).select_from(Domain)
        )
    ).one()

    return {
        "subscribers": rows,
        # Trial/Expired/MRR/ARR are `None`: no billing/subscription schema exists yet
        # (same convention as `_domain_summary`'s `mrr`). Rendered as disclosure, never fabricated.
        "summary": {
            "total_tenants": total_tenants,
            "active": active,
            "trial": None,
            "expired": None,
            "total_mrr": None,
            "arr": None,
        },
        **_pagination(total, page, limit),
    }


# ---------------------------------------------------------------------------
# Credit Papers (deals)
# ---------------------------------------------------------------------------


# deals.stage/status were dropped (US_drop_deals_status_stage); current_state is
# the state machine's single source. Terminal states are excluded from pending.
_PENDING_STAGE_SQL = "current_state IS NOT NULL AND current_state NOT IN ('ISSUED', 'ARCHIVED')"


@router.get("/credit-papers")
async def list_credit_papers(
    search: str = "",
    status: str = "",
    stage: str = "",
    priority: str = "",
    deal_type: str = "",
    tenant_id: str | None = Query(None),
    date_from: str = "",
    date_to: str = "",
    order_by: str = "",
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
):
    """Deals awaiting action. Reads go through get_sa_deals_list (creditops.store).

    `priority` stays a no-op parameter: deals has no priority column, so the
    filter is accepted and never fabricated. `stage` takes precedence over the
    legacy `status` name, and an explicit stage replaces the pending scope - the
    behaviour the existing console depends on.
    """
    require_superadmin(user)
    if tenant_id:
        try:
            uuid.UUID(tenant_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid tenant id.") from None
    try:
        payload = await asyncio.to_thread(
            store.get_sa_deals_list,
            search=search,
            state=stage or status,
            tenant_id=tenant_id or None,
            deal_type=deal_type,
            date_from=date_from or None,
            date_to=date_to or None,
            order_by=order_by,
            page=page,
            limit=limit,
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Could not load deals.") from None
    facets = payload.get("facets") or {}
    return {
        "credit_papers": payload.get("rows") or [],
        **_pagination(
            payload.get("total", 0),
            payload.get("page", page),
            payload.get("limit", limit),
        ),
        "facets": {
            "stages": facets.get("states") or [],
            "deal_types": facets.get("deal_types") or [],
            "priorities": [],
        },
        "summary": payload.get("summary") or {},
    }


# ---------------------------------------------------------------------------
# Audit Logs
# ---------------------------------------------------------------------------


@router.get("/audit-logs")
async def list_audit_logs(
    search: str = "",
    date_from: str = "",
    date_to: str = "",
    order_by: str = Query("-created_at", max_length=40),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)

    query = select(ActivityLog)
    count_query = select(func.count()).select_from(ActivityLog)

    if search:
        query = query.where(
            ActivityLog.action.ilike(f"%{search}%") | ActivityLog.description.ilike(f"%{search}%")
        )
        count_query = count_query.where(
            ActivityLog.action.ilike(f"%{search}%") | ActivityLog.description.ilike(f"%{search}%")
        )

    # Inclusive lower bound, exclusive upper: `date_to=2026-10-10` covers that
    # whole day without a time component to get wrong.
    if date_from:
        query = query.where(ActivityLog.created_at >= date_from)
        count_query = count_query.where(ActivityLog.created_at >= date_from)
    if date_to:
        query = query.where(ActivityLog.created_at < date_to)
        count_query = count_query.where(ActivityLog.created_at < date_to)

    total = (await db.execute(count_query)).scalar() or 0
    offset = (page - 1) * limit
    descending = order_by.startswith("-")
    sort_key = order_by[1:] if descending else order_by
    sort_column = {
        "created_at": ActivityLog.created_at,
        "action": ActivityLog.action,
        "description": ActivityLog.description,
    }.get(sort_key, ActivityLog.created_at)
    query = query.order_by(
        (desc(sort_column) if descending else asc(sort_column)),
        ActivityLog.id.desc(),
    ).offset(offset).limit(limit)
    result = await db.execute(query)
    logs = result.scalars().all()

    # Two lookups for the page instead of two per row.
    user_ids = {log.user_id for log in logs if log.user_id}
    domain_ids = {log.domain_id for log in logs if log.domain_id}
    user_emails: dict[str, str] = {}
    domain_names: dict[str, str] = {}
    if user_ids:
        user_emails = {
            row[0]: row[1]
            for row in (await db.execute(select(User.id, User.email).where(User.id.in_(user_ids)))).all()
        }
    if domain_ids:
        domain_names = {
            row[0]: row[1]
            for row in (
                await db.execute(select(Domain.id, Domain.domain).where(Domain.id.in_(domain_ids)))
            ).all()
        }

    out = []
    for log in logs:
        out.append(
            {
                "id": log.id,
                "action": log.action,
                "description": log.description,
                "user_email": user_emails.get(log.user_id),
                "domain": domain_names.get(log.domain_id),
                "created_at": str(log.created_at),
            }
        )

    return {
        "audit_logs": out,
        "total": total,
        "page": page,
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# System Health
# ---------------------------------------------------------------------------


@router.get("/system-health")
async def get_system_health(user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    require_superadmin(user)
    services = [_check_api_health(), await asyncio.to_thread(_check_db_health)]
    return {
        "status": _overall_health(services),
        "services": services,
    }


# ---------------------------------------------------------------------------
# Templates & Guardrails (cross-tenant, superadmin read-only)
# ---------------------------------------------------------------------------


async def _tenants_for_page(db: AsyncSession, tenant_ids: set[str]) -> dict[str, dict]:
    """One lookup for a page: tenant (domain) id -> {id, domain, company}."""
    if not tenant_ids:
        return {}
    rows = (
        await db.execute(select(Domain.id, Domain.domain, Domain.company).where(Domain.id.in_(tenant_ids)))
    ).all()
    return {r[0]: {"id": r[0], "domain": r[1], "company": r[2]} for r in rows}


@router.get("/templates")
async def list_templates_cross_tenant(
    tenant_id: str | None = Query(None),
    q: str | None = Query(None),
    order_by: str = Query("-updated_at", max_length=40),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)
    offset = (page - 1) * limit
    try:
        rows, total = await asyncio.to_thread(
            template_store.list_all_templates,
            tenant_id=tenant_id or None,
            q=q or None,
            limit=limit,
            offset=offset,
            order_by=order_by,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tenant id.") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Could not load templates.") from None

    tenants = await _tenants_for_page(db, {r["tenant_id"] for r in rows if r.get("tenant_id")})
    try:
        usage_rows = await asyncio.to_thread(store.get_sa_template_usage)
    except Exception:
        raise HTTPException(status_code=500, detail="Could not load template usage.") from None
    usage = {str(r["template_id"]): int(r["deal_count"]) for r in usage_rows}
    out = [
        {
            "id": r["id"],
            "tenant_id": r["tenant_id"],
            "tenant": tenants.get(r["tenant_id"]),
            "name": r["name"],
            "description": r["description"],
            "paper_types": r["paper_types"],
            "sections": r["sections"],
            "is_default": r["is_default"],
            "is_active": r["is_active"],
            "source": r["source"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            # Live (non-deleted) deals pointing at this template. Zero is a real
            # count, not an absence of data.
            "usage_count": usage.get(str(r["id"]), 0),
        }
        for r in rows
    ]
    return {"templates": out, **_pagination(total, page, limit)}


@router.get("/guardrails")
async def list_guardrails_cross_tenant(
    tenant_id: str | None = Query(None),
    q: str | None = Query(None),
    order_by: str = Query("-updated_at", max_length=40),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    require_superadmin(user)
    offset = (page - 1) * limit
    try:
        rows, total = await asyncio.to_thread(
            guardrail_store.list_all_rules,
            tenant_id=tenant_id or None,
            q=q or None,
            limit=limit,
            offset=offset,
            order_by=order_by,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tenant id.") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Could not load guardrails.") from None

    tenants = await _tenants_for_page(db, {r["tenant_id"] for r in rows if r.get("tenant_id")})
    out = [
        {
            "id": r["id"],
            "tenant_id": r["tenant_id"],
            "tenant": tenants.get(r["tenant_id"]),
            "kind": r["kind"],
            "payload": r["payload"],
            "severity": r["severity"],
            "weight": r["weight"],
            "scope_type": r["scope_type"],
            "scope_value": r["scope_value"],
            "is_active": r["is_active"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]
    return {"guardrails": out, **_pagination(total, page, limit)}


# ---------------------------------------------------------------------------
# Platform settings (read-only, superadmin)
# ---------------------------------------------------------------------------

_SETTINGS_NOTE = "Platform settings are managed via environment configuration (.env). No secrets are exposed."


def _bool_env(name: str) -> bool | None:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def _int_env(name: str) -> int | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _app_title() -> str | None:
    """The FastAPI app title — the only application-name source in the code.

    Imported lazily: `api.main` imports this module at startup, so a module-level
    import here would be circular.
    """
    try:
        from creditpaper.api.main import app

        return app.title or None
    except Exception:
        return None


@router.get("/settings")
async def get_platform_settings(user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    """Read-only platform configuration for the superadmin settings page.

    Non-secret values only: no API key, token, DSN or password is serialised
    here. A value with no configured source comes back `null` (rendered as
    "Not available") — never a guess.
    """
    require_superadmin(user)
    st = get_settings()
    signup = _bool_env("SIGNUP_ENABLED")
    domain_verification = _bool_env("DOMAIN_VERIFICATION_REQUIRED")
    return {
        "application_name": os.environ.get("APP_NAME") or _app_title(),
        "frontend_url": st.FRONTEND_URL or None,
        "backend_url": os.environ.get("BACKEND_URL") or None,
        # No kill-switch exists: the signup routes are mounted unconditionally,
        # and an env override wins when one is ever added.
        "signup_enabled": True if signup is None else signup,
        # Company-domain validation runs unconditionally in the signup flow
        # (guard_domain_for_signup / blocked_personal_domain).
        "domain_verification_required": True if domain_verification is None else domain_verification,
        # Two-factor policy is per-firm (`tenant_mfa_policy`); no platform-wide source.
        "mfa_required": None,
        "ai_provider": current_provider().value,
        "storage_retention_days": _int_env("STORAGE_RETENTION_DAYS"),
        "note": _SETTINGS_NOTE,
    }


# ---------------------------------------------------------------------------
# Billing summary + invitations (superadmin)
# ---------------------------------------------------------------------------


def _load_billing_summary() -> dict:
    """Grouped billing counts. Sync psycopg — callers run this via to_thread."""
    with cursor() as cur:
        cur.execute(
            "SELECT COALESCE(tier, 'starter'), COALESCE(status, 'trial'), count(*)"
            " FROM billing_accounts GROUP BY 1, 2"
        )
        account_rows = cur.fetchall()
        cur.execute(
            "SELECT COALESCE(currency, %s), count(*), COALESCE(sum(amount), 0)"
            " FROM billing_invoices GROUP BY 1",
            (CURRENCY,),
        )
        invoice_rows = cur.fetchall()

    by_tier: dict[str, int] = {tier: 0 for tier in PLANS}
    by_status: dict[str, int] = {"trial": 0, "active": 0, "past_due": 0, "cancelled": 0}
    for tier, status, count in account_rows:
        by_tier[tier] = by_tier.get(tier, 0) + int(count)
        by_status[status] = by_status.get(status, 0) + int(count)
    total_accounts = sum(by_tier.values())

    note = None
    if not invoice_rows:
        invoices = {"total_count": 0, "total_amount": 0.0, "currency": CURRENCY}
    elif len(invoice_rows) == 1:
        currency, count, amount = invoice_rows[0]
        invoices = {"total_count": int(count), "total_amount": float(amount), "currency": currency}
    else:
        # Mixed currencies would make a summed amount meaningless; report the
        # count as real and decline to add unlike units together.
        invoices = {
            "total_count": int(sum(r[1] for r in invoice_rows)),
            "total_amount": None,
            "currency": None,
        }
        note = "Invoices exist in more than one currency; amounts are not summed."

    if total_accounts == 0:
        note = "No billing accounts exist yet"

    return {
        "plans": [
            {
                "tier": plan.tier,
                "monthly": float(plan.monthly) if plan.monthly is not None else None,
                "annual": float(plan.annual) if plan.annual is not None else None,
                "currency": CURRENCY,
            }
            for plan in PLANS.values()
        ],
        "accounts": {
            "total": total_accounts,
            "by_tier": by_tier,
            "by_status": by_status,
        },
        "invoices": invoices,
        "note": note,
    }


@router.get("/billing/summary")
async def get_billing_summary(user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    require_superadmin(user)
    return await asyncio.to_thread(_load_billing_summary)


@router.get("/invitations")
async def list_invitations(
    domain_id: str = "",
    order_by: str = Query("-created_at", max_length=40),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    """Cross-tenant invitations. The signup `token` column is never serialised.

    `domain_id` scopes the list to one tenant for the domain detail page;
    invitations.domain_id is a real FK to domains.id.
    """
    require_superadmin(user)

    count_query = select(func.count()).select_from(Invitation)
    query = select(Invitation, Domain.domain).join(Domain, Invitation.domain_id == Domain.id, isouter=True)
    if domain_id:
        count_query = count_query.where(Invitation.domain_id == domain_id)
        query = query.where(Invitation.domain_id == domain_id)

    total = (await db.execute(count_query)).scalar() or 0
    offset = (page - 1) * limit
    descending = order_by.startswith("-")
    sort_key = order_by[1:] if descending else order_by
    sort_column = {
        "email": Invitation.email,
        "role": Invitation.role,
        "accepted": Invitation.accepted,
        "expires_at": Invitation.expires_at,
        "created_at": Invitation.created_at,
    }.get(sort_key, Invitation.created_at)
    rows = (
        await db.execute(
            query.order_by(
                (desc(sort_column) if descending else asc(sort_column)),
                Invitation.id.desc(),
            ).offset(offset).limit(limit)
        )
    ).all()
    out = [
        {
            "id": inv.id,
            "email": inv.email,
            "role": inv.role,
            "accepted": inv.accepted,
            "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
            "domain": domain,
        }
        for inv, domain in rows
    ]
    return {"invitations": out, **_pagination(total, page, limit)}
