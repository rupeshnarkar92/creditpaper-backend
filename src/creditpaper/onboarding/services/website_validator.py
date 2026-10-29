from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = {"https"}
BLOCKED_HOSTS = {"localhost"}
MAX_REDIRECTS = 5


def _is_ip_blocked(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        return False


def _is_host_blocked(host: str) -> bool:
    h = host.lower().strip().rstrip(".")
    if h in BLOCKED_HOSTS:
        return True
    if h == "localhost":
        return True
    if re.match(r"^127\.\d+\.\d+\.\d+$", h):
        return True
    if h.startswith("10."):
        return True
    if h.startswith("192.168."):
        return True
    if re.match(r"^172\.(1[6-9]|2\d|3[0-1])\.", h):
        return True
    if h.startswith("169.254."):
        return True
    if h == "0.0.0.0" or h == "::1" or h == "::":
        return True
    if _is_ip_blocked(h):
        return True
    return False


def validate_website_url(raw: str) -> tuple[str, str | None]:
    if not raw or not raw.strip():
        return "", "INVALID_WEBSITE_URL"
    url = raw.strip()
    if len(url) > MAX_URL_LENGTH:
        return "", "INVALID_WEBSITE_URL"
    if url.lower().startswith("javascript:"):
        return "", "INVALID_WEBSITE_URL"
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except Exception:
        return "", "INVALID_WEBSITE_URL"
    host = parsed.hostname or ""
    if not host:
        return "", "INVALID_WEBSITE_URL"
    if _is_host_blocked(host):
        return "", "WEBSITE_BLOCKED"
    if parsed.scheme not in ALLOWED_SCHEMES:
        return "", "INVALID_WEBSITE_URL"
    if parsed.port is not None:
        if parsed.port not in (80, 443):
            pass
    if not re.match(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", host) and not _is_ip_blocked(host):
        if "." not in host:
            return "", "INVALID_WEBSITE_URL"
    normalized = f"{parsed.scheme}://{host}"
    if parsed.port:
        normalized += f":{parsed.port}"
    path = parsed.path or "/"
    normalized += path
    if parsed.query:
        normalized += f"?{parsed.query}"
    return normalized, None


def validate_redirect_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ALLOWED_SCHEMES:
        return False
    host = parsed.hostname or ""
    if not host or _is_host_blocked(host):
        return False
    return True


def resolve_and_check_host(host: str) -> str | None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return "WEBSITE_UNREACHABLE"
    for _family, _, _, _, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return "WEBSITE_BLOCKED"
        except ValueError:
            continue
    return None
