"""Column-aware verification of table rows: which column is the value in?

A funding-table row - "Land $2,000,000 $5,000,000 $7,000,000" - states three
money figures. ``verify`` alone can prove that the value is ONE of them, never
which: the verdict is FLAGGED_ROLE. A row is also shorter than the anchor's
six-word minimum, so it is refused (TOO_SHORT) before it is read at all. This
module proves the column deterministically, from the page, and keeps the
verifier's zero-false-pass standard: whatever it cannot prove leaves the verdict
exactly as it was (FLAGGED_ROLE or TOO_SHORT). It never guesses.

The reader supplies page text only (no word boxes), so columns are split by
ORDER: the n-th figure of a row sits under the n-th column of its header. That
is only trusted when the counts agree exactly.

1. A table row. ``is_row_quote``: a label of at least one word, then two or more
   figures separated by whitespace only, and nothing after them. On the page the
   row is the anchored span read the same way (``_row_at``): the label must be
   wholly inside the span and start a row (a line start, or straight after the
   previous row's last figure), and the row's figures are the run of figures
   starting inside the span and continuing on the page while only whitespace
   separates them (a quote that stops early still sees the whole row). Figures
   may sit on one line with the label or one per line below it (OCR layout).
2. The field. ``field_column`` reads the citation path: ``...[<key>].<column>``
   with the column in FIELD_COLUMNS (``senior``, ``equity``, ``total``,
   ``mezzanine``). The row's label must name the key (``_label_names_key``:
   the same words in order, "and", "of", "the" and "&" ignored - the key is
   the label slugified): a quote of the wrong row is not proof.
3. The header. The nearest header block above the row on the same page: lines
   made only of header tokens - column words (``_ROLE_WORDS``), year labels
   ("2024", "FY24"), "%", a currency ("$", "A$", "AUD"), a scale ("$'000",
   "$m", "(millions)", "'000"), neutral words ("development", "amount", "GST",
   ...) and leading stub words ("Item", "Description", ...). The stub (label)
   column's heading need not be recognised: any words before the first value
   column are the stub heading and are ignored, provided the line alone then
   states two or more value columns, one of them a role ("Item description Senior
   Equity Total"). A word after the first value column that maps to no column
   is an unmapped value heading: not a header (fail-safe). A header line is
   never directly followed by a figure-only line (that line is a row label,
   such as "Total" or "Senior Debt" in a one-cell-per-line layout). Between the
   header and the row only rows may stand: figure lines, label-plus-figures
   lines, blank lines, and single label lines directly followed by a figure
   line. Anything else - a sub-heading, a paragraph, an unrecognised header -
   ends the search with no header (fail-safe), so a row never borrows the
   header of another table above it. Every run of figures between the header
   and the row has at most as many figures as the header has columns.
4. Columns. Header tokens are read in order (top to bottom, left to right).
   Adjacent words of one role on one line are one column ("Senior Debt",
   "Total Development Cost"); "mezzanine/junior" before "debt/loan/facility" is
   one mezzanine column. A year label or "%" is a column of its own. The row's
   figure count must EQUAL the column count, and exactly one column must carry
   the field's role; otherwise no mapping (fail-safe).
5. Scale. A scale written in a header applies to the cells under that header
   only: after a column's words on the same line it scales that column; on a
   units line with one scale per column, each its own; alone (or before the
   columns) it scales every column of that header. A column with no scale on a
   page that states a scale anywhere keeps D13's page-level ambiguity: a bare
   cell there matches nothing. A cell written in accounting brackets
   ("(500,000)") matches nothing either: its sign is not proven.
6. The check. The value (a number, or a string that is one money/number figure
   and nothing else) is compared as an exact Decimal with the cell of its
   column times the column's scale: MATCH. Otherwise, if it equals another
   cell of the row (at that cell's own scale): WRONG_COLUMN. Otherwise:
   NO_MATCH. The verifier decides the currency (the cell's own, or its
   header's "$") and every other leg.

A short row (fewer than anchor.MIN_QUOTE_WORDS words, at least MIN_ROW_WORDS)
is accepted by the verifier only when all of this holds, its anchor is EXACT,
and its figures equal the page's figures exactly (N2).
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Final

from creditpaper.ingest.values import Figure, FigureKind, figures, page_scaled, value_text

# A label word and two figures.
MIN_ROW_WORDS: Final = 3
# A row label longer than this is prose, not a label.
MAX_LABEL_WORDS: Final = 10

# Field-path column -> header role.
FIELD_COLUMNS: Final[dict[str, str]] = {
    "senior": "senior",
    "equity": "equity",
    "total": "total",
    "mezzanine": "mezzanine",
}

# Header words naming a column -> the column's role.
_ROLE_WORDS: Final[dict[str, str]] = {
    "senior": "senior",
    "debt": "senior",
    "loan": "senior",
    "facility": "senior",
    "equity": "equity",
    "contribution": "equity",
    "contributions": "equity",
    "total": "total",
    "cost": "total",
    "costs": "total",
    "budget": "total",
    "mezzanine": "mezzanine",
    "mezz": "mezzanine",
    "junior": "mezzanine",
}
# "Mezzanine Debt", "Junior Loan": one mezzanine column, not two.
_MEZZ_HEADS: Final = frozenset({"mezzanine", "mezz", "junior"})
_DEBT_WORDS: Final = frozenset({"debt", "loan", "facility"})
# Words that qualify a column without naming one.
_NEUTRAL: Final = frozenset({"development", "project", "amount", "gst", "incl", "excl", "inc", "ex"})
# Words that name the label (stub) column; allowed only before the first column.
_STUB: Final = frozenset(
    {"item", "items", "description", "particulars", "category", "line", "component", "details"}
    | {"use", "uses", "source", "sources"}
)
# Punctuation a stub heading may carry ("Item No.", "Cost - head"); before the columns only.
_STUB_PUNCT: Final = frozenset({".", "-", "/", "&", "'", "#", "*", "\N{EN DASH}"})
_LABEL_STOP: Final = frozenset({"and", "of", "the", "&"})

_MAGNITUDE = r"(?:'000|\N{RIGHT SINGLE QUOTATION MARK}000|000s?|mn|m|bn|b|k|millions?|thousands?|billions?)"
_CURRENCY = r"(?:AUD|A\$|AU\$|US\$|NZ\$|USD|NZD|\$)"
# One header token. Order matters: a scale before a currency, a year before a word.
_HEADER_TOKEN = re.compile(
    r"(?P<scale>(?i:\(?\s*" + _CURRENCY + r"\s?" + _MAGNITUDE + r"\s*\)?(?![A-Za-z0-9])"
    r"|\(?\s*(?:'000|\N{RIGHT SINGLE QUOTATION MARK}000|000s)\s*\)?(?![A-Za-z0-9])"
    r"|\(\s*(?:in\s+)?(?:millions|thousands|billions)\s*\)"
    r"|(?<![A-Za-z])(?:in\s+)?(?:millions|thousands|billions)(?![A-Za-z])))"
    r"|(?P<year>(?i:FY\s?(?:\d{4}|\d{2}))(?![A-Za-z0-9])|(?<![0-9])(?:19|20)\d{2}(?![0-9A-Za-z]))"
    r"|(?P<currency>\(?\s*" + _CURRENCY + r"\s*\)?(?![A-Za-z0-9]))"
    r"|(?P<pct>\(?%\)?)"
    r"|(?P<word>[A-Za-z]+)"
    r"|(?P<sep>[\s|(),:]+)"
    r"|(?P<other>.)"
)
_EXPONENTS: Final[dict[str, int]] = {"k": 3, "m": 6, "mn": 6, "b": 9, "bn": 9}


class ColumnOutcome(str, Enum):
    MATCH = "match"
    WRONG_COLUMN = "wrong_column"
    NO_MATCH = "no_match"


@dataclass(frozen=True)
class _Column:
    role: str
    scale: int | None = None  # a power of ten, or None when the header states none
    currency: str | None = None  # "$" (bare: the deal currency) or a code


@dataclass(frozen=True)
class ColumnCheck:
    """What the table layer proved about one value in one row."""

    outcome: ColumnOutcome
    role: str
    # The cell under the field's column, its scale (power of ten) and the
    # currency its header writes ("$" bare, a code, or None).
    cell: Figure
    scale: int
    header_currency: str | None
    # The row label as the page writes it, and the raw extent of the header block
    # and of the row (for the currency scan of the lines they cover).
    label: str
    header_span: tuple[int, int]
    row_span: tuple[int, int]


# --- the field ---------------------------------------------------------------
_FIELD = re.compile(r"\[(?P<key>[^\[\]]+)\]\.(?P<column>[a-z_]+)$")


def field_column(field: str | None) -> tuple[str, str] | None:
    """``(key, role)`` for a field path ``...[<key>].<column>``, or None."""
    if not field:
        return None
    m = _FIELD.search(field)
    if m is None or m["column"] not in FIELD_COLUMNS:
        return None
    return m["key"], FIELD_COLUMNS[m["column"]]


def _words_of(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.casefold()) if t and t not in _LABEL_STOP]


def _label_names_key(label: str, key: str) -> bool:
    """Does the row label name the collection key? The same words, in order.

    Exact words only: a prefix rule ("stat" for "statutory") would also let
    "land" name "Landscaping" - a wrong row proven by the property test.
    """
    words = _words_of(label)
    return bool(words) and words == _words_of(key)


# --- the row -----------------------------------------------------------------
def _gap_is_space(text: str, a: int, b: int) -> bool:
    return not text[a:b].strip()


def _label_words(label: str) -> int:
    return sum(1 for t in label.split() if any(ch.isalnum() for ch in t))


def is_row_quote(quote: str) -> bool:
    """A label (at least one word), then two or more figures, whitespace between, nothing after."""
    figs = figures(quote)
    if len(figs) < 2:
        return False
    label = quote[: figs[0].start]
    if not 1 <= _label_words(label) <= MAX_LABEL_WORDS:
        return False
    if any(not _gap_is_space(quote, a.end, b.start) for a, b in zip(figs, figs[1:], strict=False)):
        return False
    return not quote[figs[-1].end :].strip(" \t\r\n.;,")


@functools.lru_cache(maxsize=32)
def _page_figures(page_text: str) -> tuple[Figure, ...]:
    return tuple(figures(page_text))


@dataclass(frozen=True)
class _Row:
    label: str
    label_start: int
    cells: tuple[Figure, ...]


def _row_at(page_text: str, start: int, end: int) -> _Row | None:
    """The table row the span [start, end) holds, read on the page; None if not a row."""
    figs = _page_figures(page_text)
    inside = [i for i, f in enumerate(figs) if start <= f.start and f.end <= end]
    if not inside:
        return None
    first = inside[0]
    # The span holds one run of figures: label, figures, nothing after.
    if inside != list(range(first, first + len(inside))):
        return None
    if any(not _gap_is_space(page_text, figs[i].end, figs[i + 1].start) for i in inside[:-1]):
        return None
    if page_text[figs[inside[-1]].end : end].strip(" \t\r\n.;,"):
        return None
    label_text = page_text[start : figs[first].start]
    if not 1 <= _label_words(label_text) <= MAX_LABEL_WORDS:
        return None
    # The label starts a row: a line start, or straight after the previous figure.
    before = page_text[:start].rstrip(" \t")
    after_figure = first > 0 and figs[first - 1].end == len(before)
    if before and not before.endswith(("\n", "\r")) and not after_figure:
        return None
    # The row runs on past the span while only whitespace separates its figures.
    last = inside[-1]
    while last + 1 < len(figs) and _gap_is_space(page_text, figs[last].end, figs[last + 1].start):
        last += 1
    cells = figs[first : last + 1]
    if len(cells) < 2:
        return None
    return _Row(label=" ".join(label_text.split()), label_start=start, cells=cells)


# --- lines -------------------------------------------------------------------
class _Kind(str, Enum):
    BLANK = "blank"
    FIGURES = "figures"  # figures only
    ROW = "row"  # a label, then figures
    HEADER = "header"
    WORDS = "words"  # a short line of words, no figures
    OTHER = "other"


@dataclass(frozen=True)
class _Line:
    start: int
    end: int
    kind: _Kind
    tokens: tuple[tuple[str, str], ...] = ()  # (group, text) header tokens


def _header_tokens(text: str) -> tuple[tuple[str, str], ...] | None:
    """The header tokens of a line, or None when any token is not a header token."""
    out: list[tuple[str, str]] = []
    column_seen = False
    for m in _HEADER_TOKEN.finditer(text):
        group = m.lastgroup or "other"
        if group == "sep":
            continue
        tok = m.group().strip(" ()")
        if group == "word":
            low = tok.casefold()
            if low in _ROLE_WORDS:
                group = "role"
            elif low in _NEUTRAL:
                group = "neutral"
            elif low in _STUB:
                group = "stub"
            elif not column_seen:
                group = "free"  # the stub heading, whatever it says: ignored
            else:
                return None  # an unmapped value-column heading
            tok = low
        elif group == "other":
            if column_seen or tok not in _STUB_PUNCT:
                return None
            continue
        if group in ("role", "year", "pct"):
            column_seen = True
        out.append((group, tok))
    return tuple(out)


def _classify(page_text: str, start: int, end: int, figs: tuple[Figure, ...]) -> _Line:
    text = page_text[start:end]
    if not text.strip():
        return _Line(start, end, _Kind.BLANK)
    tokens = _header_tokens(text)
    if tokens and any(g == "free" for g, _ in tokens):
        # Free stub words make a header only when the line alone states two or more
        # value columns, one of them a role: "Item description Senior Equity Total".
        # Otherwise the line is read as before (a title, a label, prose).
        cols = _columns([_Line(start, end, _Kind.HEADER, tokens)])
        if cols is None or not any(not c.role.startswith(("year:", "percent")) for c in cols):
            tokens = None
    if tokens and any(g in ("role", "year", "pct") for g, _ in tokens):
        return _Line(start, end, _Kind.HEADER, tokens)
    if tokens and all(g in ("scale", "currency") for g, _ in tokens):
        return _Line(start, end, _Kind.HEADER, tokens)
    line_figs = [f for f in figs if f.start < end and f.end > start]
    if not line_figs:
        words = _label_words(text)
        return _Line(start, end, _Kind.WORDS if words <= MAX_LABEL_WORDS else _Kind.OTHER)
    if any(f.start < start or f.end > end for f in line_figs):
        return _Line(start, end, _Kind.OTHER)  # a figure across the line break
    if any(
        not _gap_is_space(page_text, a.end, b.start) for a, b in zip(line_figs, line_figs[1:], strict=False)
    ):
        return _Line(start, end, _Kind.OTHER)
    if page_text[line_figs[-1].end : end].strip(" \t\r.;,"):
        return _Line(start, end, _Kind.OTHER)
    head = page_text[start : line_figs[0].start]
    if not head.strip():
        return _Line(start, end, _Kind.FIGURES)
    if _label_words(head) <= MAX_LABEL_WORDS:
        return _Line(start, end, _Kind.ROW)
    return _Line(start, end, _Kind.OTHER)


@functools.lru_cache(maxsize=32)
def _lines(page_text: str) -> tuple[_Line, ...]:
    figs = _page_figures(page_text)
    out: list[_Line] = []
    pos = 0
    for raw in page_text.split("\n"):
        end = pos + len(raw)
        out.append(_classify(page_text, pos, end, figs))
        pos = end + 1
    return tuple(out)


def _line_index(lines: tuple[_Line, ...], pos: int) -> int:
    for i, line in enumerate(lines):
        if line.start <= pos <= line.end:
            return i
    return len(lines) - 1


def _next_below(lines: tuple[_Line, ...], i: int) -> _Line | None:
    for line in lines[i + 1 :]:
        if line.kind is not _Kind.BLANK:
            return line
    return None


# --- the header --------------------------------------------------------------
def _scale_of(token: str) -> tuple[int, str | None]:
    """(power of ten, currency or None) of a scale token."""
    t = token.strip(" ()").replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
    cur_m = re.match(r"(?i)" + _CURRENCY, t)
    cur = None
    if cur_m is not None:
        cur = _currency_code(cur_m.group())
        t = t[cur_m.end() :].strip()
    low = t.casefold().removeprefix("in").strip()
    if low.startswith(("'000", "000", "thousand")):
        return 3, cur
    if low.startswith("million"):
        return 6, cur
    if low.startswith("billion"):
        return 9, cur
    return _EXPONENTS[low], cur


def _currency_code(token: str) -> str:
    t = token.strip(" ()").upper()
    if t == "$":
        return "$"
    if t in ("A$", "AU$", "AUD", "AUD$"):
        return "AUD"
    if t in ("US$", "USD"):
        return "USD"
    if t in ("NZ$", "NZD"):
        return "NZD"
    return t


def _year(token: str) -> str:
    digits = re.sub(r"\D", "", token)
    return "year:" + (digits if len(digits) == 4 else "20" + digits)


@dataclass(frozen=True)
class _Header:
    columns: tuple[_Column, ...]
    start: int
    end: int


def _merge(
    col: tuple[int | None, str | None], scale: int | None, cur: str | None
) -> tuple[int | None, str | None] | None:
    """A column's (scale, currency) with a new token's added; None on a conflict."""
    old_scale, old_cur = col
    if scale is not None and old_scale is not None and scale != old_scale:
        return None
    if cur is not None and old_cur is not None and cur != old_cur:
        return None
    return (old_scale if scale is None else scale, old_cur if cur is None else cur)


def _unit(group: str, token: str) -> tuple[int | None, str | None]:
    """(scale, currency) a scale or currency token states."""
    if group == "scale":
        return _scale_of(token)
    return None, _currency_code(token)


def _columns(block: list[_Line]) -> tuple[_Column, ...] | None:
    """The header's columns, in order; None when the tokens do not make columns."""
    roles: list[str] = []
    units: list[tuple[int | None, str | None]] = []
    block_unit: tuple[int | None, str | None] | None = (None, None)
    for line in block:
        toks = list(line.tokens)
        if all(g in ("scale", "currency") for g, _ in toks):
            if len(toks) == 1:  # one unit for the whole header
                block_unit = _merge(block_unit, *_unit(*toks[0])) if block_unit is not None else None
                if block_unit is None:
                    return None
                continue
            if len(toks) != len(roles) or len(roles) < 2:  # a units line: one per column
                return None
            for k, tok in enumerate(toks):
                merged = _merge(units[k], *_unit(*tok))
                if merged is None:
                    return None
                units[k] = merged
            continue
        current: int | None = None  # the column this line's last column word opened
        for k, (g, t) in enumerate(toks):
            if g == "free":
                continue  # only ever before the first column (_header_tokens)
            if g == "stub":
                if roles:
                    return None  # a stub word after the columns: not a header we read
                continue
            if g == "neutral":
                continue
            if g == "role":
                role = _ROLE_WORDS[t]
                prev = toks[k - 1][1] if k else None
                if current is not None and t in _DEBT_WORDS and prev in _MEZZ_HEADS:
                    continue  # "Mezzanine Debt": one mezzanine column
                if current is not None and roles[current] == role:
                    continue  # "Senior Debt", "Total Development Cost": one column
                roles.append(role)
                units.append((None, None))
                current = len(roles) - 1
            elif g in ("year", "pct"):
                roles.append(_year(t) if g == "year" else "percent")
                units.append((None, None))
                current = len(roles) - 1
            else:  # a scale or currency: its column's, or the header's before any column
                if current is None:
                    block_unit = _merge(block_unit, *_unit(g, t)) if block_unit is not None else None
                    if block_unit is None:
                        return None
                else:
                    merged = _merge(units[current], *_unit(g, t))
                    if merged is None:
                        return None
                    units[current] = merged
    if len(roles) < 2 or block_unit is None:
        return None
    columns: list[_Column] = []
    for role, unit in zip(roles, units, strict=True):
        merged = _merge(unit, *block_unit)
        if merged is None:
            return None
        columns.append(_Column(role, scale=merged[0], currency=merged[1]))
    return tuple(columns)


def _runs(page_text: str, start: int, end: int) -> list[int]:
    """Lengths of the runs of whitespace-separated figures inside [start, end)."""
    figs = [f for f in _page_figures(page_text) if start <= f.start and f.end <= end]
    runs: list[int] = []
    for i, f in enumerate(figs):
        if i and _gap_is_space(page_text, figs[i - 1].end, f.start):
            runs[-1] += 1
        else:
            runs.append(1)
    return runs


def _header_above(page_text: str, row: _Row) -> _Header | None:
    """The nearest header block above the row, reached through rows only."""
    lines = _lines(page_text)
    r = _line_index(lines, row.label_start)
    i = r - 1
    while i >= 0:
        line = lines[i]
        below = _next_below(lines, i)
        figure_below = below is not None and below.kind is _Kind.FIGURES
        if line.kind is _Kind.BLANK or line.kind in (_Kind.FIGURES, _Kind.ROW):
            i -= 1
            continue
        if line.kind is _Kind.HEADER and not figure_below:
            break
        if line.kind in (_Kind.WORDS, _Kind.HEADER) and figure_below:
            i -= 1  # a row label on its own line, its figures below it
            continue
        return None  # a sub-heading, prose or an unknown header: no header
    else:
        return None
    top = i
    j = i - 1
    while j >= 0 and lines[j].kind in (_Kind.HEADER, _Kind.BLANK):
        if lines[j].kind is _Kind.HEADER:
            top = j
        j -= 1
    block = [line for line in lines[top : i + 1] if line.kind is _Kind.HEADER]
    cols = _columns(block)
    if cols is None:
        return None
    header = _Header(cols, lines[top].start, lines[i].end)
    if any(n > len(cols) for n in _runs(page_text, header.end, row.label_start)):
        return None  # a wider row between: another table
    return header


# --- the check ---------------------------------------------------------------
def _decimal(value: object) -> tuple[Decimal, Figure] | None:
    """The value as one plain number, with its figure; None for anything else."""
    text = value_text(value)
    if text is None or isinstance(value, bool):
        return None
    figs = figures(text)
    if len(figs) != 1 or figs[0].kind not in (FigureKind.MONEY, FigureKind.NUMBER):
        return None
    rest = (text[: figs[0].start] + text[figs[0].end :]).strip()
    if rest:
        return None
    try:
        return Decimal(figs[0].value), figs[0]
    except InvalidOperation:
        return None


def _bracketed(page_text: str, cell: Figure) -> bool:
    before = page_text[: cell.start].rstrip(" \t")
    after = page_text[cell.end :].lstrip(" \t")
    return before.endswith("(") or after.startswith(")")


def _cell_value(page_text: str, cell: Figure, col: _Column, scaled_page: bool) -> Decimal | None:
    """The cell's value under its header, or None when it is not a proven number."""
    if cell.kind not in (FigureKind.MONEY, FigureKind.NUMBER) or _bracketed(page_text, cell):
        return None
    if col.scale is None and scaled_page and cell.kind is FigureKind.NUMBER:
        return None  # D13: the page states a scale this column does not
    try:
        d = Decimal(cell.value)
    except InvalidOperation:
        return None
    return d.scaleb(col.scale or 0)


def check_column(
    value: object, page_text: str, start: int, end: int, *, field: str | None
) -> ColumnCheck | None:
    """Prove the value's column in the table row at [start, end), or None (fail-safe)."""
    target = field_column(field)
    wanted = _decimal(value)
    if target is None or wanted is None:
        return None
    key, role = target
    row = _row_at(page_text, start, end)
    if row is None or not _label_names_key(row.label, key):
        return None
    header = _header_above(page_text, row)
    if header is None or len(header.columns) != len(row.cells):
        return None
    mine = [k for k, c in enumerate(header.columns) if c.role == role]
    if len(mine) != 1:
        return None
    idx = mine[0]
    scaled_page = page_scaled(page_text)
    values = [
        _cell_value(page_text, cell, col, scaled_page)
        for cell, col in zip(row.cells, header.columns, strict=True)
    ]
    number = wanted[0]
    if values[idx] is not None and values[idx] == number:
        outcome = ColumnOutcome.MATCH
    elif any(v is not None and v == number for k, v in enumerate(values) if k != idx):
        outcome = ColumnOutcome.WRONG_COLUMN
    else:
        outcome = ColumnOutcome.NO_MATCH
    col = header.columns[idx]
    return ColumnCheck(
        outcome=outcome,
        role=role,
        cell=row.cells[idx],
        scale=col.scale or 0,
        header_currency=col.currency,
        label=row.label,
        header_span=(header.start, header.end),
        row_span=(row.label_start, row.cells[-1].end),
    )


# --- enumerating a table, rather than sampling it ------------------------------------
#
# Everything above proves something about ONE value the model pointed at. This
# reads the whole table instead, and it exists because of what that sampling
# costs.
#
# Measured 2026-09-24 across three deal folders: 372 of 404 distinct paths are
# collection rows, and they are where run-to-run instability lives. A window
# holding twelve comparable sales gets three or five of them reported, a
# different three or five on the next read, so the same documents produce a
# different fact sheet. Neither more passes, a stronger model, nor key
# canonicalisation moved it, because none of them changes what the model is
# being asked to do: choose which rows to mention.
#
# So the model keeps pointing at one cell and this layer reads the column. Two
# reads that mention different rows of one table then produce the SAME set, and
# it costs no extra model call.


@dataclass(frozen=True)
class TableRow:
    """One row of a table: its label as written, its cells, and its extent."""

    label: str
    cells: tuple[Figure, ...]
    start: int
    end: int


_WORD_CHAR = re.compile(r"[^\W\d_]", re.UNICODE)


def _enumerable_row(page_text: str, start: int, end: int) -> TableRow | None:
    """A table row for ENUMERATION, which is a looser question than ``_row_at``.

    ``_row_at`` requires the label to be words before the first figure, because
    it is proving a cited value's column and a label that could be a figure is
    not proof. Enumeration cannot use that rule: the single largest collection
    in this corpus is comparable sales, whose labels are street addresses, and
    "12 Riddell Street 5,250,000 120 43,750" begins with a figure. Those lines
    classify as OTHER and would be invisible to a stricter reader - which is
    the exact table this layer exists for.

    So the CELLS are the maximal trailing run of figures separated only by
    whitespace, and the label is everything before that run. The label may
    contain figures; it must contain at least one letter, so a line of bare
    numbers is not a row with a numeric label.

    This proves nothing about what the columns mean. ``column_check`` is still
    the only thing that does, and it still uses ``_row_at``.
    """
    figs = _page_figures(page_text)
    on_line = [f for f in figs if start <= f.start and f.end <= end]
    if len(on_line) < 2:
        return None
    if page_text[on_line[-1].end : end].strip(" \t\r\n.;,"):
        return None
    # Walk back from the last figure while only whitespace separates them: that
    # run is the row's cells, and anything before it is the label.
    first = len(on_line) - 1
    while first > 0 and _gap_is_space(page_text, on_line[first - 1].end, on_line[first].start):
        first -= 1
    cells = tuple(on_line[first:])
    if len(cells) < 2:
        return None
    label_text = page_text[start : cells[0].start]
    if not _WORD_CHAR.search(label_text):
        return None
    if _label_words(label_text) > MAX_LABEL_WORDS:
        return None
    return TableRow(label=" ".join(label_text.split()), cells=cells, start=start, end=end)


def rows_of_table_at(page_text: str, start: int, end: int) -> tuple[TableRow, ...]:
    """Every row of the table the span ``[start, end)`` sits in, in page order.

    Empty unless the span itself reads as a row: a span that is prose, a
    heading or a lone figure has no table to enumerate, and inventing one is
    the false pass this module refuses to make elsewhere.

    The table is the run of neighbouring lines that also read as rows AND state
    the same number of cells. A blank line does not end it, because tables are
    often spaced; a line that is not a row, or that is a row of a different
    width, does. The equal-width rule is what keeps a following paragraph or a
    total line with its own shape out of the block.

    A header is not required and is not read. This says only that these rows
    belong to one table, never what a column means.
    """
    here = _enumerable_row(page_text, start, end)
    if here is None:
        return ()
    lines = _lines(page_text)
    i = _line_index(lines, start)
    width = len(here.cells)

    def walk(step: int) -> list[TableRow]:
        out: list[TableRow] = []
        j = i + step
        while 0 <= j < len(lines):
            line = lines[j]
            if line.kind is _Kind.BLANK:
                j += step
                continue
            row = _enumerable_row(page_text, line.start, line.end)
            if row is None or len(row.cells) != width:
                break
            out.append(row)
            j += step
        return out

    above = walk(-1)
    above.reverse()
    return (*above, here, *walk(1))


def cell_index(row: TableRow, value_start: int) -> int | None:
    """Which cell of ``row`` starts at ``value_start``, or None.

    The column is taken POSITIONALLY, which is the same assumption
    ``column_check`` makes and states: with page text alone the n-th figure of
    a row sits under the n-th column of its header. Returning None rather than
    a nearest match keeps that assumption honest — a value that is not itself a
    cell cannot name a column.
    """
    for n, cell in enumerate(row.cells):
        if cell.start == value_start:
            return n
    return None
