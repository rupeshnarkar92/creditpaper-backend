"""Email-domain policy and tenant lookup for signup."""

from __future__ import annotations

from creditpaper.auth.domain_validator import BLOCKED_DOMAINS, EMAIL_REGEX
from creditpaper.auth.supabase_admin import StoredProcedureUnavailable, signup_email_status


class DomainAlreadyRegistered(Exception):
    """An active tenant already exists on this email domain."""

    def __init__(self, domain: str) -> None:
        self.domain = domain
        super().__init__(f"An active tenant already exists for domain {domain!r}")


class DomainNotCompany(Exception):
    """The email is on a blocked (personal/disposable) domain."""

    def __init__(self, domain: str) -> None:
        self.domain = domain
        super().__init__(f"Domain {domain!r} is a personal or disposable email domain")


def normalize_domain(email: str) -> str:
    """Return a normalized domain from a syntactically valid email."""
    if not email or not EMAIL_REGEX.match(email):
        raise ValueError("Invalid email format")
    return email.rsplit("@", 1)[1].lower()


def require_company_domain(email: str) -> str:
    """Accept any valid domain except the configured consumer/disposable list."""
    domain = normalize_domain(email)
    if domain in BLOCKED_DOMAINS:
        raise DomainNotCompany(domain)
    return domain


def tenant_exists_for_domain(domain: str) -> bool:
    """True when an active tenant claims this domain; fail closed on DB errors."""
    try:
        status = signup_email_status(domain=domain)
    except StoredProcedureUnavailable as exc:
        raise RuntimeError("Signup domain could not be checked") from exc
    if status is None:
        raise RuntimeError("Signup domain could not be checked")
    return bool(status.get("tenant_domain_registered"))


def guard_domain_for_signup(email: str, exclude_user_id: str | None = None) -> str:
    """Validate a work address and ensure its domain is not already claimed."""
    domain = require_company_domain(email)
    try:
        status = signup_email_status(
            email=email,
            domain=domain,
            exclude_user_id=exclude_user_id,
        )
    except StoredProcedureUnavailable as exc:
        raise RuntimeError("Signup eligibility could not be checked") from exc
    if status is None:
        raise RuntimeError("Signup eligibility could not be checked")
    if status.get("domain_registered"):
        raise DomainAlreadyRegistered(domain)
    return domain
