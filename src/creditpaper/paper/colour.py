"""Colour contrast guard for the paper theme. Pure functions, no I/O.

WCAG 2.x relative luminance and contrast ratio, and a darkening step that works in
OKLCH lightness so a brand colour keeps its hue (and as much chroma as sRGB allows)
while it is brought up to the contrast a paper needs on white:

  - heading colour: 4.5:1 (text)
  - accent colour:  3:1 (rules, table header tint, cover band; non-text)
"""

from __future__ import annotations

import math
import re

WHITE = "#FFFFFF"
HEADING_MIN_CONTRAST = 4.5
ACCENT_MIN_CONTRAST = 3.0

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_hex(value: object) -> tuple[int, int, int] | None:
    """`#RRGGBB` (or `RRGGBB`) to an RGB tuple; anything else is None."""
    if not isinstance(value, str):
        return None
    m = _HEX.match(value.strip())
    if not m:
        return None
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def normalise_hex(value: object) -> str | None:
    rgb = parse_hex(value)
    return to_hex(rgb) if rgb else None


# --- WCAG --------------------------------------------------------------------


def _channel_to_linear(c8: float) -> float:
    c = c8 / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_channel_to_linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str = WHITE) -> float:
    ra, rb = parse_hex(a), parse_hex(b)
    if ra is None or rb is None:
        raise ValueError(f"not a hex colour: {a!r} / {b!r}")
    la, lb = relative_luminance(ra), relative_luminance(rb)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# --- OKLab / OKLCH (Björn Ottosson) ------------------------------------------


def _linear_to_channel(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def hex_to_oklch(value: str) -> tuple[float, float, float]:
    rgb = parse_hex(value)
    if rgb is None:
        raise ValueError(f"not a hex colour: {value!r}")
    r, g, b = (_channel_to_linear(c) for c in rgb)
    l_ = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m_ = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s_ = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in (l_, m_, s_))
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    A = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    B = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    C = math.hypot(A, B)
    H = math.degrees(math.atan2(B, A)) % 360.0
    return L, C, H


def _oklch_to_linear_rgb(L: float, C: float, H: float) -> tuple[float, float, float]:
    A = C * math.cos(math.radians(H))
    B = C * math.sin(math.radians(H))
    l_ = L + 0.3963377774 * A + 0.2158037573 * B
    m_ = L - 0.1055613458 * A - 0.0638541728 * B
    s_ = L - 0.0894841775 * A - 1.2914855480 * B
    l3, m3, s3 = l_**3, m_**3, s_**3
    r = 4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3
    g = -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3
    b = -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3
    return r, g, b


def _in_gamut(rgb: tuple[float, float, float], eps: float = 1e-6) -> bool:
    return all(-eps <= c <= 1 + eps for c in rgb)


def oklch_to_hex(L: float, C: float, H: float) -> str:
    """OKLCH to sRGB hex. Out-of-gamut colours have their chroma reduced (hue and
    lightness kept) until they fit, rather than being clipped per channel, which
    would shift the hue."""
    L = min(max(L, 0.0), 1.0)
    lin = _oklch_to_linear_rgb(L, C, H)
    if not _in_gamut(lin):
        lo, hi = 0.0, C
        for _ in range(30):
            mid = (lo + hi) / 2
            if _in_gamut(_oklch_to_linear_rgb(L, mid, H)):
                lo = mid
            else:
                hi = mid
        lin = _oklch_to_linear_rgb(L, lo, H)
    channels = tuple(round(min(max(_linear_to_channel(min(max(c, 0.0), 1.0)), 0.0), 1.0) * 255) for c in lin)
    return to_hex((channels[0], channels[1], channels[2]))


def ensure_contrast(value: str, minimum: float, background: str = WHITE) -> str:
    """Return `value` if it meets `minimum` contrast on `background`, else the same
    hue darkened in OKLCH lightness until it does. Always returns uppercase hex."""
    hexed = normalise_hex(value)
    if hexed is None:
        raise ValueError(f"not a hex colour: {value!r}")
    if contrast_ratio(hexed, background) >= minimum:
        return hexed
    L, C, H = hex_to_oklch(hexed)
    step = 0.005
    candidate = hexed
    while L > 0:
        L = max(L - step, 0.0)
        candidate = oklch_to_hex(L, C, H)
        if contrast_ratio(candidate, background) >= minimum:
            return candidate
    return "#000000"


def tint(value: str, amount: float = 0.10, background: str = WHITE) -> str:
    """`amount` of `value` laid over `background` (sRGB mix): the accent tint used
    for table header fills."""
    fg, bg = parse_hex(value), parse_hex(background)
    if fg is None or bg is None:
        raise ValueError(f"not a hex colour: {value!r} / {background!r}")
    mixed = tuple(round(amount * f + (1 - amount) * b) for f, b in zip(fg, bg, strict=True))
    return to_hex((mixed[0], mixed[1], mixed[2]))


def hue_distance(a: str, b: str) -> float:
    """Degrees between two colours' OKLCH hues (0..180)."""
    ha, hb = hex_to_oklch(a)[2], hex_to_oklch(b)[2]
    d = abs(ha - hb) % 360.0
    return min(d, 360.0 - d)
