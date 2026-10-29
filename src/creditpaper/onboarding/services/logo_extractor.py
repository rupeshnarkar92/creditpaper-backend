"""Logo-driven brand colour extraction.

Three public helpers:
  - find_best_logo_url(html, base_url): pick the highest-scoring <img>
    or favicon on the page.
  - download_logo(url): GET the image bytes, fail safely on any error.
  - extract_palette(bytes): sample the dominant saturated colour and
    two supporting colours.

The Pillow import is lazy so the module imports cleanly on systems
without the dependency (the brand_extractor can still run; the logo
path returns None and falls back to the rule-based parser).
"""

from __future__ import annotations

import colorsys
import logging
import re
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from creditpaper.onboarding.services.website_validator import (  # noqa: F401
    resolve_and_check_host,
    validate_redirect_url,
)

log = logging.getLogger(__name__)


class LogoExtractionError(Exception):
    """Raised when a logo operation fails for a reason worth surfacing to the
    operator. Most failures (404, timeout, host blocked) return None instead
    of raising — the rule-based path takes over silently."""


_LOGO_HINT = re.compile(r"logo|brand", re.I)
_TRACKING_PIXEL = 16  # both width and height below this is a tracking pixel
_SCORE_THRESHOLD = 50


class _LogoScorer(HTMLParser):
    """Parse the HTML once, score every <img>, and a <link rel=icon> as a
    fallback. Return the highest-scoring absolute URL or None."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self._ancestors: list[str] = []
        self.best: tuple[str, int] | None = None  # (absolute_url, score)
        self._best_is_favicon: bool = False
        self._data_buffer: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "img":
            src = a.get("src")
            if not src:
                return
            try:
                width = int(a.get("width", "0") or 0)
                height = int(a.get("height", "0") or 0)
            except (TypeError, ValueError):
                width = height = 0
            score = self._score(a, width, height, src)
            if score >= _SCORE_THRESHOLD and (self.best is None or score > self.best[1]):
                self.best = (urljoin(self.base_url, src), score)
                self._best_is_favicon = False
            return
        if tag == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href")
            if href and "icon" in rel:
                # Favicon fallback — small bonus, no alt/class scoring.
                url = urljoin(self.base_url, href)
                if self.best is None or self.best[1] < 30:
                    self.best = (url, 30)
                    self._best_is_favicon = True
            return
        # Track header/nav for the ancestor bonus.
        if tag in ("header", "nav"):
            self._ancestors.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("header", "nav") and self._ancestors:
            self._ancestors.pop()

    def _score(self, attrs: dict[str, str | None], width: int, height: int, src: str) -> int:
        score = 0
        alt = (attrs.get("alt") or "").strip()
        klass = (attrs.get("class") or "") + " " + (attrs.get("id") or "")
        if _LOGO_HINT.search(alt):
            score += 200
        if _LOGO_HINT.search(klass):
            score += 150
        if any(a in ("header", "nav") for a in self._ancestors):
            score += 50
        if width > 0 and height > 0:
            score += (width * height) // 10_000
            if width < _TRACKING_PIXEL and height < _TRACKING_PIXEL:
                score -= 100
        if src.startswith("data:"):
            score += 30
        return score


def find_best_logo_url(html: str, base_url: str) -> str | None:
    """Return the highest-scoring logo <img> URL on the page, or None.

    Scoring (each addition):
      +200  if alt matches /logo|brand/i
      +150  if class or id matches /logo|brand/i
      + 50  if the <img> is inside a <header> or <nav>
      + (width * height) // 10000     # bigger is better
      - 100  if both width and height are < 16 (tracking pixel)
      + 30  if the src is a data: URL (favicon-style)

    Plus a `<link rel="icon">` fallback at score 30 if no <img> qualifies.
    Returns the highest-scoring URL whose score >= 50.
    """
    parser = _LogoScorer(base_url)
    try:
        parser.feed(html)
    except Exception:
        return None
    if parser.best is None:
        return None
    if parser.best[1] < _SCORE_THRESHOLD and not parser._best_is_favicon:
        return None
    return parser.best[0]


_DEFAULT_TIMEOUT_S = 8.0
_DEFAULT_MAX_BYTES = 2_000_000


def download_logo(
    logo_url: str,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> bytes | None:
    """GET the logo image, validate the host, return the bytes or None on any
    error. The blocklist is shared with website_fetcher.py so private IPs and
    link-local addresses are never reached.
    """
    if not logo_url or not logo_url.startswith(("http://", "https://")):
        return None
    # fmt: off
    from urllib.parse import urlparse
    # fmt: on
    host = urlparse(logo_url).hostname or ""
    block = resolve_and_check_host(host)
    if block:
        return None
    try:
        resp = httpx.get(
            logo_url,
            follow_redirects=True,
            timeout=timeout_s,
        )
    except (httpx.TimeoutException, httpx.HTTPError, OSError):
        return None
    try:
        if resp.status_code != 200:
            return None
        body = resp.content
        if len(body) > max_bytes:
            return None
        return body
    finally:
        resp.close()


# Add at the bottom
def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def extract_palette(
    image_bytes: bytes,
    *,
    max_size: int = 200,
    sat_threshold: float = 0.20,
    light_min: float = 0.10,
    light_max: float = 0.90,
    hue_buckets: int = 30,
) -> tuple[str | None, str | None, str | None]:
    """Return (primary, secondary, accent) hex strings sampled from the image.

    Each may be None if the image has no pixel of the corresponding role
    (e.g. a monochrome logo yields only primary; a black-and-white logo
    yields all None because every pixel is rejected by the saturation/lightness
    filter).
    """
    try:
        from PIL import Image
    except ImportError:
        return None, None, None

    try:
        import io as _io

        img = Image.open(_io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        head = image_bytes[:64].lstrip().lower()
        if head.startswith(b"<?xml") or head.startswith(b"<svg"):
            log.info(
                "logo_extract_palette svg_unsupported: Pillow cannot decode SVG "
                "without cairosvg/svglib (error=%s); rule-based fallback will run",
                e.__class__.__name__,
            )
        return None, None, None

    img.thumbnail((max_size, max_size))
    pixels = list(img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata())

    # Bucket each surviving pixel by (hue_bucket, sat_bucket, light_bucket).
    # The histogram is small (~30 * 10 * 10 = 3000 buckets) but in practice
    # only a few hundred are populated. The most-populated bucket is primary.
    bucket_counts: Counter = Counter()
    bucket_rgb: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    bucket_pixels: dict[tuple[int, int, int], int] = {}

    for r, g, b in pixels:
        h, lit, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        if s < sat_threshold:
            continue
        if lit < light_min or lit > light_max:
            continue
        hue_bucket = int(h * hue_buckets + 0.5) % hue_buckets
        sat_bucket = int(s * 10 + 0.5)
        light_bucket = int(lit * 10 + 0.5)
        key = (hue_bucket, sat_bucket, light_bucket)
        bucket_counts[key] += 1
        # Track the average RGB of the bucket for a representative colour.
        prev = bucket_rgb.get(key)
        if prev is None:
            bucket_rgb[key] = (r, g, b)
            bucket_pixels[key] = 1
        else:
            pr, pg, pb = prev
            n = bucket_pixels[key]
            bucket_rgb[key] = (
                (pr * n + r) // (n + 1),
                (pg * n + g) // (n + 1),
                (pb * n + b) // (n + 1),
            )
            bucket_pixels[key] = n + 1

    if not bucket_counts:
        return None, None, None

    # Sort buckets by count descending. The top 3 are primary, secondary, accent
    # provided their hue_buckets are at least 1 apart.
    sorted_buckets = sorted(bucket_counts.items(), key=lambda kv: -kv[1])

    chosen: list[tuple[int, tuple[int, int, int]]] = []
    for key, _count in sorted_buckets:
        hue_b = key[0]
        if any(
            abs(hue_b - ch_hue) % hue_buckets >= 1 and abs(hue_b - ch_hue) % hue_buckets < hue_buckets - 1
            for ch_hue, _ch_rgb in chosen
        ):
            pass
        # Require at least 12° (1 bucket) hue separation from every already-chosen.
        if all(
            min((hue_b - ch_hue) % hue_buckets, (ch_hue - hue_b) % hue_buckets) >= 1
            for ch_hue, _ch_rgb in chosen
        ):
            chosen.append((hue_b, bucket_rgb[key]))
        if len(chosen) == 3:
            break

    primary = _hex(chosen[0][1]) if len(chosen) >= 1 else None
    secondary = _hex(chosen[1][1]) if len(chosen) >= 2 else None
    accent = _hex(chosen[2][1]) if len(chosen) >= 3 else None
    return primary, secondary, accent
