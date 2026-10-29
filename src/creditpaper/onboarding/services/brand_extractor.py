# ruff: noqa: E741, F841
from __future__ import annotations

import logging
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

from creditpaper.onboarding.services import logo_extractor  # noqa: E402

download_logo = logo_extractor.download_logo  # alias for test monkey-patching

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
RGB_RE = re.compile(r"^rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*(?:,\s*[\d.]+)?\s*\)$", re.I)
HSL_RE = re.compile(r"^hsla?\(\s*\d+\s*,\s*[\d.]+%?\s*,\s*[\d.]+%?\s*(?:,\s*[\d.]+)?\s*\)$", re.I)

_FONT_KEYWORDS = frozenset(
    {
        "sans-serif",
        "serif",
        "monospace",
        "cursive",
        "fantasy",
        "inherit",
        "initial",
        "unset",
        "revert",
        "revert-layer",
        "system-ui",
        "ui-sans-serif",
        "ui-serif",
        "ui-monospace",
        "ui-rounded",
        "emoji",
        "math",
        "fangsong",
    }
)

NAMED_COLORS: dict[str, str] = {
    "black": "#000000",
    "white": "#FFFFFF",
    "red": "#FF0000",
    "green": "#008000",
    "blue": "#0000FF",
    "teal": "#008080",
    "navy": "#000080",
    "gold": "#FFD700",
    "orange": "#FFA500",
    "purple": "#800080",
    "grey": "#808080",
    "gray": "#808080",
}

NEUTRAL_BLACKLIST = {
    "#000000",
    "#FFFFFF",
    "#F5F5F5",
    "#F4F4F4",
    "#F8F8F8",
    "#FAFAFA",
    "#EEEEEE",
    "#EDEDED",
    "#DDDDDD",
    "#CCCCCC",
    "#E5E5E5",
    "#F0F0F0",
}

SYSTEM_COLORS = {
    "#28A745",
    "#DC3545",
    "#FFC107",
    "#17A2B8",
    "#FF0000",
    "#00C851",
    "#FF4444",
}

STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "have",
    "will",
    "about",
    "more",
    "your",
    "our",
    "are",
    "was",
    "has",
    "been",
    "were",
    "their",
    "what",
    "when",
    "where",
    "which",
    "also",
    "into",
    "over",
    "than",
    "then",
    "only",
    "home",
    "contact",
    "services",
    "privacy",
    "terms",
    "cookie",
    "login",
}

CREDIT_TERMS = [
    "facility",
    "sponsor",
    "security",
    "drawdown",
    "valuation",
    "covenant",
    "tranche",
    "equity",
    "leverage",
    "guarantor",
    "borrower",
    "lvr",
    "interest",
    "margin",
    "tenor",
    "maturity",
    "repayment",
    "amortisation",
    "residual stock",
    "construction",
    "development",
    "headroom",
    "approval",
    "credit",
]

CSS_VAR_PRIMARY = re.compile(r"--\s*(?:brand[-_])?primary(?:[-_]color)?", re.I)
CSS_VAR_SECONDARY = re.compile(r"--\s*(?:brand[-_])?secondary(?:[-_]color)?", re.I)
CSS_VAR_ACCENT = re.compile(r"--\s*(?:brand[-_])?accent(?:[-_]color)?", re.I)


def _hex_to_rgb(hex6: str) -> tuple[int, int, int]:
    h = hex6.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hsl(r: int, g: int, b: int) -> tuple[float, float, float]:
    r /= 255
    g /= 255
    b /= 255
    mx = max(r, g, b)
    mn = min(r, g, b)
    l = (mx + mn) / 2  # noqa: E741
    if mx == mn:
        return 0, 0, l
    d = mx - mn
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)  # noqa: E741
    if mx == r:
        h = (g - b) / d + (6 if g < b else 0)
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    h /= 6
    return h, s, l


def _is_neutral(hex6: str) -> bool:
    if hex6.upper() in NEUTRAL_BLACKLIST:
        return True
    try:
        r, g, b = _hex_to_rgb(hex6)
    except Exception:
        return False
    _, s, lv = _rgb_to_hsl(r, g, b)
    if s < 0.15:
        return True
    if lv > 0.96 or lv < 0.04:
        return True
    return False


def _is_system_color(hex6: str) -> bool:
    return hex6.upper() in SYSTEM_COLORS


def _hsl_to_hex(h: float, s: float, lightness: float) -> str:
    h = h % 360 / 360
    s = max(0, min(1, s))
    lv = max(0, min(1, lightness))
    if s == 0:
        v = int(round(lv * 255))
        return f"#{v:02X}{v:02X}{v:02X}"

    def hue2rgb(p, q, t):
        if t < 0:
            t += 1
        if t > 1:
            t -= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    q = lv * (1 + s) if lv < 0.5 else lv + s - lv * s
    p = 2 * lv - q
    r = hue2rgb(p, q, h + 1 / 3)
    g = hue2rgb(p, q, h)
    b = hue2rgb(p, q, h - 1 / 3)
    return f"#{int(round(r * 255)):02X}{int(round(g * 255)):02X}{int(round(b * 255)):02X}"


def _normalize_color(raw: str) -> str | None:
    c = raw.strip().lower()
    if c in NAMED_COLORS:
        return NAMED_COLORS[c]
    if HEX_RE.match(raw.strip()):
        h = raw.strip()
        if len(h) == 4:
            h = "#" + "".join(ch * 2 for ch in h[1:])
        if len(h) == 5:
            h = "#" + "".join(ch * 2 for ch in h[1:4])
        if len(h) == 9:
            h = h[:7]
        return h.upper()
    if RGB_RE.match(raw.strip()):
        nums = re.findall(r"[\d.]+", raw)
        if len(nums) >= 3:
            r, g, b = int(float(nums[0])), int(float(nums[1])), int(float(nums[2]))
            if all(0 <= x <= 255 for x in (r, g, b)):
                return f"#{r:02X}{g:02X}{b:02X}"
    if HSL_RE.match(raw.strip()):
        nums = re.findall(r"[\d.]+", raw)
        if len(nums) >= 3:
            h_val = float(nums[0]) % 360
            s_val = float(nums[1].replace("%", "")) / 100 if "%" in raw else float(nums[1]) / 100
            l_val = float(nums[2].replace("%", "")) / 100 if "%" in raw else float(nums[2]) / 100
            return _hsl_to_hex(h_val, s_val, l_val)
    return None


def _color_distance(a: str, b: str) -> float:
    try:
        ra, ga, ba = _hex_to_rgb(a)
        rb, gb, bb = _hex_to_rgb(b)
    except Exception:
        return 999
    return ((ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2) ** 0.5


def _cluster_colors(candidates: list[dict]) -> list[dict]:
    clustered: list[dict] = []
    for cand in candidates:
        merged = False
        for cl in clustered:
            if _color_distance(cand["hex"], cl["hex"]) < 30:
                cl["count"] += cand["count"]
                cl["score"] = max(cl["score"], cand["score"])
                cl["sources"].extend(cand["sources"])
                merged = True
                break
        if not merged:
            clustered.append(cand)
    return clustered


class _MetaParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.title = ""
        self._in_title = False
        self.meta: dict[str, str] = {}
        self.og: dict[str, str] = {}
        self.favicon: str | None = None
        self.icons: list[str] = []
        self.fonts: list[str] = []
        self.text_chunks: list[str] = []
        self._in_style = False
        self._style_content = ""
        self.inline_styles: list[str] = []
        self.style_blocks: list[str] = []
        self.stylesheet_urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            content = a.get("content", "")
            if name and content:
                if name.startswith("og:"):
                    self.og[name] = content
                else:
                    self.meta[name] = content
        elif tag == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href", "")
            if href and ("icon" in rel or "favicon" in rel):
                full = urljoin(self.base_url, href)
                self.icons.append(full)
                if not self.favicon:
                    self.favicon = full
            if href and "stylesheet" in rel:
                self.stylesheet_urls.append(urljoin(self.base_url, href))
        elif tag == "style":
            self._in_style = True
            self._style_content = ""
        style_attr = a.get("style")
        if style_attr:
            self.inline_styles.append(style_attr)
            self.style_blocks.append(style_attr)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "style":
            self._in_style = False
            self.style_blocks.append(self._style_content)
            self._extract_fonts(self._style_content)
            self._style_content = ""

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_style:
            self._style_content += data
        else:
            stripped = data.strip()
            if stripped and len(stripped) > 1:
                self.text_chunks.append(stripped)

    def seed_fonts_from_external(self, css: str, vars_map: dict[str, str] | None = None) -> None:
        """Prepend explicit body-font-family values from the external stylesheet.

        Inline ``<style>`` blocks usually contain the framework defaults
        (e.g. ``font-family: Inter`` in a Tailwind config). The external
        page stylesheet is more authoritative — typically ``font-family:
        Plus Jakarta Sans, system-ui, sans-serif``. This helper injects the
        external result at the front of ``self.fonts`` so ``parser.fonts[0]``
        returns the real page font, not the framework default.
        """
        if vars_map:
            css = _CSS_VAR_REF.sub(
                lambda m: vars_map.get(m.group(1).strip().lower(), m.group(0)),
                css,
            )
        # Strip the :root{...} block to find the *body* declarations.
        body_css = re.sub(r":root\s*\{[^}]*\}", "", css, flags=re.I | re.S)

        # Find every body font-family declaration and pick the most "real"
        # font from each. A real font is the first name in a stack that is
        # not a CSS keyword AND not a generic-fallback name (Apple*, Segoe*,
        # Noto*, ui-*, system-*, emoji/symbol). Fallback declarations
        # (e.g. ``var(--default-font-family, ui-sans-serif, system-ui,
        # sans-serif, "Apple Color Emoji", ...)``) are ignored.
        _FALLBACK_PREFIXES = (
            "apple ",
            "segoe ",
            "noto ",
            "ui-",
            "system-",
            "blinkmac",
            "helvetica neue",
            "android",
            "roboto",
            "sfmono-",
            "fangsong",
            "emoji",
            "symbol",
        )

        def _real_font(stack: str) -> str | None:
            for fam in stack.split(","):
                fam = fam.strip().strip("\"'")
                if not fam:
                    continue
                low = fam.lower()
                if low in _FONT_KEYWORDS:
                    continue
                # Skip CSS variable placeholders (var(--x)); the external-CSS
                # seed resolves them. Per-inline-style this loop has no
                # vars_map yet, so the literal var(--...) text is meaningless.
                if low.startswith("var(--"):
                    continue
                if any(low.startswith(p) for p in _FALLBACK_PREFIXES):
                    continue
                return fam
            return None

        # First try explicit body declarations. If none have a real font,
        # the page uses a pure fallback stack — don't seed anything (let the
        # var-resolved value win downstream).
        chosen: str | None = None
        for m in re.finditer(r"font-family\s*:\s*([^;]+)", body_css, re.I):
            stack = m.group(1)
            # Monospace stacks are for code blocks, not the page body font.
            if "monospace" in stack.lower() or "mono" in stack.lower():
                continue
            real = _real_font(stack)
            if real:
                chosen = real
                break
        if chosen:
            self.fonts.insert(0, chosen)
            return
        # Last resort: pick the first non-keyword in the post-var body.
        for m in re.finditer(r"font-family\s*:\s*([^;]+)", body_css, re.I):
            for fam in m.group(1).split(","):
                fam = fam.strip().strip("\"'")
                if not fam or fam.lower() in _FONT_KEYWORDS or fam.lower().startswith("var(--"):
                    continue
                if any(fam.lower().startswith(p) for p in _FALLBACK_PREFIXES):
                    continue
                self.fonts.insert(0, fam)
                return

    def _extract_fonts(self, css: str, vars_map: dict[str, str] | None = None):
        # Normalise var(--x) so font-family stacks can be walked to a real family.
        if vars_map:
            css = _CSS_VAR_REF.sub(
                lambda m: vars_map.get(m.group(1).strip().lower(), m.group(0)),
                css,
            )
        for m in re.finditer(r"font-family\s*:\s*([^;]+)", css, re.I):
            for fam in m.group(1).split(","):
                fam = fam.strip().strip("\"'")
                if not fam or fam.lower() in _FONT_KEYWORDS or fam.lower().startswith("var(--"):
                    continue
                self.fonts.append(fam)


# Matches `:root{--x: #abc;}` and similar variable definitions.
# Allows a custom-property name and any value up to the closing brace.
_CSS_VAR_DEF = re.compile(
    r"(--[a-zA-Z_][\w-]*)\s*:\s*([^;}]+(?:\s*!important)?)",
    re.I,
)
# Matches `var(--x, fallback)`.
_CSS_VAR_REF = re.compile(r"var\(\s*(--[a-zA-Z_][\w-]*)(?:\s*,\s*([^)]+))?\s*\)", re.I)


def _parse_css_vars(css: str) -> dict[str, str]:
    """Walk every :root / :where(...) / @property block and extract --x: y pairs.

    Last-write-wins; that matches CSS cascade semantics for top-level
    custom-property declarations.
    """
    out: dict[str, str] = {}
    for m in _CSS_VAR_DEF.finditer(css):
        name, value = m.group(1).strip().lower(), m.group(2).strip()
        out[name] = value.rstrip().rstrip(";").strip()
    return out


def _resolve_css_var(value: str, vars_map: dict[str, str], *, depth: int = 0) -> str | None:
    """Resolve `var(--x, fallback)` against the var map, walking chains up to
    depth 5 to avoid cycles. Returns the literal value (which may itself be a
    font-family stack, a colour, a length — the caller knows what to do with it).
    """
    if depth > 5:
        return None
    m = _CSS_VAR_REF.search(value)
    if not m:
        return value
    name = m.group(1).strip().lower()
    fallback = m.group(2)
    resolved = vars_map.get(name)
    if resolved is None:
        return fallback.strip() if fallback else None
    return _resolve_css_var(resolved, vars_map, depth=depth + 1) or fallback


def _fetch_external_stylesheets(
    urls: list[str],
    *,
    max_total_bytes: int = 400_000,
    timeout_s: float = 8.0,
) -> str:
    """Best-effort fetch of every stylesheet URL. Returns concatenated CSS.

    CDNs and assets.calendly.com are skipped. Total response body is capped
    to keep one bad page from blowing up extraction. Network errors fall
    through silently — the inline styles still get parsed.
    """
    import httpx

    parts: list[str] = []
    used = 0
    for url in urls:
        if used >= max_total_bytes:
            break
        if "assets.calendly.com" in url or "googleapis.com" in url or "gstatic.com" in url:
            continue
        try:
            r = httpx.get(url, follow_redirects=True, timeout=timeout_s)
            if r.status_code != 200:
                continue
            body = r.content
            if used + len(body) > max_total_bytes:
                body = body[: max_total_bytes - used]
            parts.append(body.decode("utf-8", errors="ignore"))
            used += len(body)
        except Exception:
            continue
    return "\n".join(parts)


def _extract_raw_candidates(
    parser: _MetaParser, html: str, external_css: str = "", vars_map: dict[str, str] | None = None
) -> list[dict]:
    candidates: list[dict] = []
    bg_count: dict[str, int] = {}
    color_pat = re.compile(
        r"(#[0-9a-fA-F]{3,8}\b|rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+[^)]*\)|hsla?\(\s*\d+\s*,[^)]+\)|\b(?:red|blue|green|teal|navy|gold|orange|purple|black|white|grey|gray)\b)",
        re.I,
    )

    def _clean_colour_mix(css: str) -> str:
        """Strip ``color-mix()`` calls AND Tailwind v4 alpha-blended utility
        classes so the brand colour (full opacity) wins by count.

        Tailwind v4 expands e.g. ``.bg-primary/20`` to:

            .bg-primary\\/20 { background-color: #aac7d833; }

        The ``#aac7d8`` is the brand colour; the trailing ``33`` is
        20% alpha. Hundreds of these utility classes crush the actual
        brand colour in the count ranking. The cleanest fix is to
        drop the entire rule body — keep the selector for readability
        but throw away the declaration.

        Also strip WordPress admin-bar preset colours (prefix ``--wp-``)
        and the WordPress default admin-text colour ``#abb8c3`` which the
        WP block editor inlines in every page; it is an admin default,
        not a brand colour.

        And strip the WordPress default ``linear-gradient(135deg, gray-100 0%, gray-300 100%)``
        gradient which is inlined as ``rgb(238,238,238) 0%, rgb(169,184,195) 100%``
        by every WP page. ``#a9b8c3`` is the gray-300 endpoint and would
        otherwise win the colour-count race against the real brand colour.
        """
        css = re.sub(r"color-mix\([^)]*\)", "", css, flags=re.I | re.S)
        # Tailwind v4 alpha utility rules
        css = re.sub(r"\\/\d+\s*\{[^}]*\}", "", css)
        # WordPress preset variables: --wp-...: #hex
        css = re.sub(r"--wp-[\w-]+:\s*#[0-9a-fA-F]{3,8}\b", "", css)
        # The literal WordPress admin-text colour as it appears in inline
        # style blocks. --wp-admin-theme-color is the variable form; the
        # literal form (when the page inlines the resolved value) is
        # ``#abb8c3``. It's not a brand colour.
        css = re.sub(r"#[Aa][Bb][Bb]8[Cc]3\b", "", css)
        # WordPress default gradient: linear-gradient(135deg, gray-100, gray-300).
        # The endpoint ``rgb(169, 184, 195)`` (Tailwind gray-300 / #a9b8c3) is the
        # colour that was winning the count race.
        css = re.sub(
            r"linear-gradient\(\s*135deg\s*,\s*rgb\(\s*238\s*,\s*238\s*,\s*238\s*\)\s*0%\s*,\s*rgb\([^)]+\)\s*100%\s*\)",
            "",
            css,
            flags=re.I | re.S,
        )
        return css

    for css in [*parser.style_blocks, external_css]:
        _ = css.lower()
        css = _clean_colour_mix(css)
        for m in color_pat.finditer(css):
            raw = m.group(0)
            hexv = _normalize_color(raw)
            if not hexv:
                continue
            tail = css[max(0, m.start() - 30) : m.start()].lower()
            prefix = css[max(0, m.start() - 60) : m.start()].lower()
            suffix = css[m.end() : m.end() + 40].lower()
            context = prefix + suffix
            score = 10
            sources: list[str] = ["css"]
            if re.search(r"--\s*(?:brand[-_])?primary(?:[-_]color)?\s*:\s*$", tail):
                score += 100
                sources.append("css-var-primary")
            if re.search(r"--\s*(?:brand[-_])?secondary(?:[-_]color)?\s*:\s*$", tail):
                score += 90
                sources.append("css-var-secondary")
            if re.search(r"--\s*(?:brand[-_])?accent(?:[-_]color)?\s*:\s*$", tail):
                score += 90
                sources.append("css-var-accent")
            if "button" in context or "cta" in context:
                score += 60
                sources.append("button")
            if "header" in context or "nav" in context or "navbar" in context:
                score += 50
                sources.append("header")
            if "logo" in context:
                score += 40
                sources.append("logo")
            if re.search(r":root|theme|design-token|brand", prefix):
                score += 20
                sources.append("design-token")
            is_header = "header" in context or "nav" in context or "button" in context or "logo" in context
            is_in_background = (
                "background" in prefix and "background-color" not in prefix.replace("background", "", 1)
            ) and not is_header
            if is_in_background:
                bg_count[hexv] = bg_count.get(hexv, 0) + 1
                sources.append("background")
            if re.search(r"body\s*\{\s*color|p\s*\{\s*color", prefix):
                score -= 80
                sources.append("text-only")
            if _is_neutral(hexv):
                score -= 100
                sources.append("neutral")
            if _is_system_color(hexv) and "accent" not in sources:
                score -= 50
                sources.append("system-color")

            candidates.append({"hex": hexv, "raw": raw, "score": score, "count": 1, "sources": sources})

    inline_count: dict[str, int] = {}
    for v in candidates:
        inline_count[v["hex"]] = inline_count.get(v["hex"], 0) + 1
    for cand in candidates:
        cand["count"] = inline_count[cand["hex"]]
        cand["score"] += min(20, (cand["count"] - 1) * 5)
        bg = bg_count.get(cand["hex"], 0)
        if bg >= 3 and not _is_neutral(cand["hex"]):
            cand["score"] += 50
            cand["sources"].append("background-dominant")

    return candidates


def _classify_colors(
    candidates: list[dict],
    vars_map: dict[str, str] | None = None,
) -> tuple[str | None, str | None, str | None, dict]:
    if not candidates:
        return None, None, None, {"primary": 0, "secondary": 0, "accent": 0}

    # Debug: show top 3 before clustering
    top3 = sorted(candidates, key=lambda x: -x["score"])[:3]
    log.info("classify_colors top3: %s", [(c["hex"], c["score"]) for c in top3])

    clustered = _cluster_colors(candidates)
    clustered.sort(key=lambda x: x["score"], reverse=True)

    var_primary = None
    var_secondary = None
    var_accent = None

    # First: if vars_map is available in the closure, use the literal
    # --primary / --secondary / --accent values directly. These are
    # unambiguous; the cluster-first heuristic is fooled by colours that
    # appear in multiple CSS var declarations.
    if vars_map:
        for var_name, var_target in (
            ("--primary", "primary"),
            ("--secondary", "secondary"),
            ("--accent", "accent"),
        ):
            value = vars_map.get(var_name)
            if value and HEX_RE.match(value.strip()):
                # Normalise to uppercase
                norm = value.strip().upper()
                if not norm.startswith("#"):
                    norm = "#" + norm
                # short hex → long hex
                if len(norm) == 4:
                    norm = "#" + "".join(ch * 2 for ch in norm[1:])
                if len(norm) == 9:
                    norm = norm[:7]
                if var_target == "primary":
                    var_primary = norm
                elif var_target == "secondary":
                    var_secondary = norm
                elif var_target == "accent":
                    var_accent = norm

    # Fallback: if the var_map lookup didn't find a primary, use the
    # cluster-first heuristic (the legacy behaviour).
    for c in clustered:
        src = ",".join(c["sources"])
        if "css-var-primary" in src and not var_primary:
            var_primary = c["hex"]
        if "css-var-secondary" in src and not var_secondary:
            var_secondary = c["hex"]
        if "css-var-accent" in src and not var_accent:
            var_accent = c["hex"]

    primary = var_primary
    secondary = var_secondary
    accent = var_accent

    _ = [c for c in clustered if c["hex"] not in {primary, secondary, accent}]

    def pick_best(exclude: set[str]) -> dict | None:
        for c in clustered:
            if c["hex"] in exclude:
                continue
            if c["score"] < 10:
                continue
            if _is_neutral(c["hex"]) and c["score"] < 40:
                continue
            return c
        return None

    if not primary:
        best = pick_best({secondary, accent} if secondary or accent else set())
        if best:
            primary = best["hex"]
    if not secondary:
        best = pick_best({primary, accent})
        if best:
            secondary = best["hex"]
    if not accent:
        for c in clustered:
            if c["hex"] in {primary, secondary}:
                continue
            if _is_system_color(c["hex"]):
                continue
            if c["score"] < 15:
                continue
            if _is_neutral(c["hex"]) and c["score"] < 40:
                continue
            accent = c["hex"]
            break

    if primary and secondary and _color_distance(primary, secondary) < 20:
        secondary = None
    if primary and accent and _color_distance(primary, accent) < 20:
        accent = None
    if secondary and accent and _color_distance(secondary, accent) < 20:
        accent = None

    conf = {}
    for cand in clustered:
        norm_score = max(0, min(100, cand["score"] + 50))
        conf[cand["hex"]] = round(norm_score / 100, 2)
    primary_conf = conf.get(primary, 0) if primary else 0
    secondary_conf = conf.get(secondary, 0) if secondary else 0
    accent_conf = conf.get(accent, 0) if accent else 0

    if primary and primary_conf < 0.3:
        primary = None
    if secondary and secondary_conf < 0.3:
        secondary = None
    if accent and accent_conf < 0.3:
        accent = None

    if not primary and not secondary and not accent:
        return None, None, None, {"primary": 0, "secondary": 0, "accent": 0}

    return (
        primary,
        secondary,
        accent,
        {"primary": primary_conf, "secondary": secondary_conf, "accent": accent_conf},
    )


def extract_brand(html: str, url: str, duration_ms: int = 0) -> dict:
    t0 = time.monotonic()
    parser = _MetaParser(url)
    try:
        parser.feed(html)
    except Exception:
        pass

    brand_name = (
        parser.og.get("og:site_name") or parser.meta.get("application-name") or parser.title or ""
    ).strip()
    brand_name = re.sub(r"\s*[\|\-–—]\s*.*$", "", brand_name).strip()[:80]
    brand_name = re.sub(r"<[^>]+>", "", brand_name).strip()
    if not brand_name:
        host = urlparse(url).hostname or ""
        brand_name = host.replace("www.", "").split(".")[0].title() if host else "Your Firm"

    # Fetch external stylesheets (CDN-skipped, 400 KB total cap) and resolve
    # their CSS custom properties so the rule-based parser sees real colours and
    # fonts, not literal `var(--x)` references. WordPress/Next.js/React-exported
    # sites put their brand variables in :root{} blocks of a bundled CSS file.
    external_css = _fetch_external_stylesheets(parser.stylesheet_urls)
    vars_map = _parse_css_vars(external_css)

    # Inline <style> blocks usually contain framework defaults (e.g. Inter
    # in a Tailwind config). The external page stylesheet is more authoritative
    # (e.g. Plus Jakarta Sans). Prepend its body-font to parser.fonts so it
    # wins over the inline default.
    parser.seed_fonts_from_external(external_css, vars_map)

    candidates = _extract_raw_candidates(parser, html, external_css, vars_map)
    primary, secondary, accent, conf = _classify_colors(candidates, vars_map)
    log.info("brand_extract rule_based primary=%s secondary=%s accent=%s", primary, secondary, accent)

    # --- logo-priority path ---
    # The page's logo <img> is the most reliable signal of the actual brand
    # colour. Run it first; if the logo returns a primary, it wins. Secondary/
    # accent come from the logo if available, else fall through to the rule-based
    # path (the LLM tiebreaker was removed; see DECISIONS.md).
    from creditpaper.onboarding.services import logo_extractor as _le  # local import to avoid cycles

    logo_url = _le.find_best_logo_url(html, url)
    logo_primary = logo_secondary = logo_accent = None
    if logo_url:
        logo_bytes = download_logo(logo_url)
        if logo_bytes:
            logo_primary, logo_secondary, logo_accent = _le.extract_palette(logo_bytes)
            if logo_primary:
                log.info("brand_extract logo_override primary=%s url=%s", logo_primary, logo_url)
    if logo_primary:
        primary = logo_primary
        conf["primary"] = 0.8
    if logo_secondary:
        secondary = logo_secondary
        conf["secondary"] = 0.75
    if logo_accent:
        accent = logo_accent
        conf["accent"] = 0.7

    if not primary:
        # Nothing found on the site: the default paper theme's heading colour (a stored
        # platform default). None when that record cannot be read.
        from creditpaper.paper.theme import default_theme_colour

        primary = default_theme_colour("heading")
        conf["primary"] = 0.4
    status = "completed"
    if not secondary or not accent:
        status = "partial"

    heading = parser.fonts[0] if parser.fonts else "Inter"
    body = parser.fonts[1] if len(parser.fonts) > 1 else heading
    heading = re.sub(r"[^a-zA-Z0-9\s-]", "", heading).strip()[:40] or "Inter"
    body = re.sub(r"[^a-zA-Z0-9\s-]", "", body).strip()[:40] or heading

    # Use the real logo URL (from header/nav <img>) for display. Only fall
    # back to the favicon if no <img> qualified.
    if logo_url:
        logo_url = logo_url.strip()[:500]
        if logo_url.startswith("http://"):
            logo_url = None
    else:
        logo_url = parser.favicon
        if logo_url:
            logo_url = logo_url.strip()[:500]
            if logo_url.startswith("http://"):
                logo_url = None

    visible_text = " ".join(parser.text_chunks[:200]).lower()
    words = re.findall(r"[a-z][a-z-]{2,}", visible_text)
    freq: dict[str, int] = {}
    for w in words:
        if w.lower() in STOP_WORDS or len(w) < 3:
            continue
        freq[w] = freq.get(w, 0) + 1
    vocab: list[str] = []
    for term in CREDIT_TERMS:
        if term.lower() in visible_text:
            vocab.append(term)
    for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:20]:
        if w not in vocab and len(vocab) < 8:
            if len(w) >= 3 and w.lower() not in STOP_WORDS:
                vocab.append(w)
    vocab = vocab[:8]
    if not vocab:
        vocab = ["facility", "sponsor", "security", "drawdown"]
    vocab = [re.sub(r"[^a-zA-Z0-9\s-]", "", v).strip()[:30] for v in vocab]
    vocab = [v for v in vocab if v]
    seen_v = set()
    deduped = []
    for v in vocab:
        low = v.lower()
        if low not in seen_v:
            seen_v.add(low)
            deduped.append(v)

    duration = int((time.monotonic() - t0) * 1000)
    log.info(
        "brand_extract website=%s candidates=%d primary=%s secondary=%s accent=%s conf=%s duration_ms=%d",
        url,
        len(candidates),
        primary,
        secondary,
        accent,
        conf,
        duration,
    )

    return {
        "status": status,
        "website": url,
        "duration_ms": duration_ms or duration,
        "brand": {
            "name": brand_name,
            "logo": {"url": logo_url, "alt": brand_name} if logo_url else None,
            "colors": {"primary": primary, "secondary": secondary, "accent": accent},
            "typography": {"heading": heading, "body": body},
            "vocabulary": deduped,
        },
        "_debug": {"candidates": len(candidates), "confidence": conf},
    }
