"""Email domain validation for company email enforcement.

Builds on auth.utils.public_domains to provide the domain_validator interface
required by the Supabase Auth implementation plan.
"""

import re

from creditpaper.auth.utils.public_domains import PUBLIC_DOMAINS

# Extended blocked domains — personal email providers + disposable
BLOCKED_DOMAINS: set[str] = PUBLIC_DOMAINS | {
    # Additional privacy-focused
    "proton.me",
    "tutamail.com",
    # Regional
    "mail.ru",
    "rambler.ru",
    # Disposable
    "guerrillamail.com",
    "tempmail.com",
    "throwaway.email",
    "trashmail.com",
    "mailinator.com",
}

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def extract_domain(email: str) -> str:
    """Extract domain from email address.

    Args:
        email: Full email address (e.g., "john@acme.com")

    Returns:
        Domain portion (e.g., "acme.com")

    Raises:
        ValueError: If email format is invalid
    """
    if not email or not EMAIL_REGEX.match(email):
        raise ValueError(f"Invalid email format: {email}")

    return email.split("@")[1].lower()


def is_valid_company_domain(email: str) -> bool:
    """Check if email uses a company domain (not a personal email provider).

    Args:
        email: Full email address

    Returns:
        True if domain is allowed (company/organizational email)
    """
    try:
        domain = extract_domain(email)
        return domain not in BLOCKED_DOMAINS
    except ValueError:
        return False
