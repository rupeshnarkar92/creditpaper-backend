"""Server-side fetch of a firm's stored logo, guarded against SSRF.

The paper never hot-links the firm's website; the API fetches the logo URL stored in
the brand profile and serves the bytes itself. The URL came from scraping a
third-party page, so it is untrusted input and every hop is checked:

  - scheme allowlist: https and http only;
  - the host is resolved once and EVERY address must be public (private, loopback,
    link-local, reserved, multicast, unspecified and IPv4-mapped forms of those are
    refused); the connection is then pinned to the vetted address (TLS still verifies
    the certificate against the original host name via SNI), so a second DNS answer
    cannot redirect the request to an internal address (DNS rebinding);
  - redirects are not followed automatically; up to three are followed by hand and
    each target passes the same checks;
  - 5 s timeout for the whole fetch, 2 MB cap enforced while streaming;
  - content type must be an image type (png, jpeg, gif, webp, svg).

The existing onboarding `download_logo` does not meet this bar (it follows redirects
without re-checking them, reads the whole body before the size check and does not
check the content type), so it is not reused here.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 5.0
MAX_REDIRECTS = 3
ALLOWED_SCHEMES = frozenset({"https", "http"})
ALLOWED_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"})
CACHE_TTL_S = 3600.0
_CACHE_MAX = 256


@dataclass(frozen=True)
class Logo:
    content: bytes
    content_type: str

    @property
    def etag(self) -> str:
        return '"' + hashlib.sha256(self.content).hexdigest()[:32] + '"'


class LogoRefused(Exception):
    """The URL or the response failed a guard."""


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is not None:
            return is_public_address(mapped)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def _resolve_public(host: str, port: int) -> str:
    """Resolve `host`; return one vetted address, refusing if ANY address is not public."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_address(literal):
            raise LogoRefused("address not public")
        return str(literal)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise LogoRefused("host does not resolve") from exc
    addresses: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        if not is_public_address(ip):
            raise LogoRefused("host resolves to a non-public address")
        addresses.append(str(ip))
    if not addresses:
        raise LogoRefused("host does not resolve")
    return addresses[0]


def check_url(url: str) -> tuple[str, str, int]:
    """Validate scheme and host syntax. Returns (scheme, host, port)."""
    if not isinstance(url, str) or len(url) > 2048:
        raise LogoRefused("bad url")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise LogoRefused("scheme not allowed")
    if parts.username or parts.password:
        raise LogoRefused("credentials in url")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise LogoRefused("host not allowed")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise LogoRefused("bad port") from exc
    return scheme, host, port


def _pinned_url(url: str, address: str) -> str:
    parts = urlsplit(url)
    ip_host = f"[{address}]" if ":" in address else address
    netloc = ip_host if parts.port is None else f"{ip_host}:{parts.port}"
    return parts._replace(netloc=netloc).geturl()


def _sniff_ok(content_type: str, head: bytes) -> bool:
    h = head.lstrip()[:512].lower()
    if content_type == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if content_type == "image/gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if content_type == "image/svg+xml":
        return h.startswith(b"<?xml") or h.startswith(b"<svg") or b"<svg" in h
    return False


def _fetch(url: str, *, transport: httpx.BaseTransport | None = None) -> Logo:
    deadline = time.monotonic() + TIMEOUT_S
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        scheme, host, port = check_url(current)
        address = _resolve_public(host, port)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LogoRefused("timeout")
        headers = {"Host": host if urlsplit(current).port is None else f"{host}:{port}"}
        headers["User-Agent"] = "CreditPaper/1.0 logo"
        headers["Accept"] = ", ".join(sorted(ALLOWED_CONTENT_TYPES))
        extensions = {"sni_hostname": host} if scheme == "https" else {}
        with httpx.Client(
            timeout=httpx.Timeout(remaining), follow_redirects=False, transport=transport, trust_env=False
        ) as client:
            request = client.build_request(
                "GET", _pinned_url(current, address), headers=headers, extensions=extensions
            )
            response = client.send(request, stream=True)
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise LogoRefused("redirect without location")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise LogoRefused(f"status {response.status_code}")
                content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                if content_type not in ALLOWED_CONTENT_TYPES:
                    raise LogoRefused("content type not an allowed image type")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                    raise LogoRefused("too large")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise LogoRefused("too large")
                    if time.monotonic() > deadline:
                        raise LogoRefused("timeout")
                if not body or not _sniff_ok(
                    content_type, bytes(body[: 64 if content_type != "image/svg+xml" else 512])
                ):
                    raise LogoRefused("body does not match content type")
                return Logo(bytes(body), content_type)
            finally:
                response.close()
    raise LogoRefused("too many redirects")


_cache: dict[str, tuple[float, Logo | None]] = {}
_cache_lock = threading.Lock()


def fetch_logo(url: str, *, transport: httpx.BaseTransport | None = None) -> Logo | None:
    """The logo at `url`, or None if it cannot be fetched safely. Never raises.

    Results (including failures, so a dead URL is not retried per request) are cached
    in-process for an hour.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    try:
        logo: Logo | None = _fetch(url, transport=transport)
    except LogoRefused as exc:
        log.info("logo fetch refused: %s", exc)
        logo = None
    except (httpx.HTTPError, OSError, ValueError) as exc:
        log.info("logo fetch failed: %s", exc.__class__.__name__)
        logo = None
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[url] = (now, logo)
    return logo


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
