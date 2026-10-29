"""Locate an extractor's quote on a page: exact first, then fuzzy.

Defaults are measured, not chosen (2026-09-18, fixture corpus): threshold 0.90,
minimum 6 words and an ambiguity margin of 0.05 give 98-100% correct-page
accuracy with 0.0% wrong-page at every quote length. Three-word quotes
false-match 10% of the time, so short quotes are refused rather than guessed.
The margin exists for repeated schedules inside ONE document (3-20% wrong page),
which is the dominant failure mode; cross-document confusion is rare.

How a page is scored (both sides normalised by `text_norm.normalise`):

1. Locate. An exact substring occurrence of the quote if there is one;
   otherwise the region `rapidfuzz.fuzz.partial_ratio_alignment` picks. That
   call only LOCATES: when the page is shorter than the quote it aligns the
   page inside the quote and reports 100, so its own score is never used.
2. Snap. A span edge may not fall inside a word (a run of alphanumerics,
   combining marks included) or inside a FIGURE. What a figure is has ONE
   definition, the verifier's: `values.figure_spans(raw page)`, the raw extent
   of every figure the verifier reads - sign, currency (glued or spaced),
   number, magnitude (glued, spaced or across a line break), percent unit and
   footnote marker ("-$ 500,000", "-AUD500,000", "-.5%", "$12.4 million",
   "US$500,000", "8.95%*"). The region is trimmed of edge spaces, then each
   edge that falls inside a word or a figure moves outward to include it whole
   or inward to exclude it whole, whichever of the up to four combinations
   scores higher for the quote; ties keep the outward edge. A span therefore
   never partly covers a figure: `values.value_in_page_span`, and a re-read of
   the matched text, see the page's whole figure ("-0.50%") or none of it,
   never "0.50%". Inward snapping matters because the aligner's region often
   starts or ends on a fragment of a neighbouring word the quote does not
   contain.
3. Score. Every candidate's score is
   `Indel.normalized_similarity(normalised quote, final page span)`, so the
   score measures how much of the WHOLE quote the page region covers: a
   heading page shorter than the quote, or a quote carrying clauses the page
   lacks, cannot reach the threshold.

Snapping happens in normalised coordinates, where the aligner and the scorer
work, so a moved edge is scored on exactly the text it produces. Each raw figure
extent maps to the normalised characters whose raw source range overlaps it
(`Normalised.offsets` and `ends` are non-decreasing: two bisections per
figure). A whitespace run collapses to one space mapped to its first raw
character, and a composed or expanded cluster maps to its whole raw range, so
the mapped extent covers the figure and a span that includes or excludes it in
normalised text includes or excludes it in the raw text. The final raw span is
checked against the raw extents once more; a span that would still cut a
figure (which the mapping rules out) is dropped, never cited.

EXACT means the page span is identical to the quote after normalisation with
no edge moved (score 1.0). If either edge moved the result is FUZZY with the
score recomputed on the final span, and the threshold then applies.

Repetitive pages: every exact occurrence is checked for being clean (a regex
prefilter on word edges, then the figure check), so a clean copy is found
wherever it sits and an ambiguity it creates is reported. Unclean occurrences
are snapped and rescored for their first MAX_EXACT_OCCURRENCES distinct edge
signatures; a repeated signature would score identically and does not count.
Degenerate periodic text is bounded at _MAX_EXACT_SCAN occurrences; a page left
with unscored occurrences is treated as a possible tie, so it can make the
result AMBIGUOUS but never lets another page win by the margin.

`candidates` lists the pages at or above the threshold and within the margin
of the best. AMBIGUOUS may carry a single candidate when the runner-up is
within the margin but below the threshold. The 0.90/6/0.05
calibration predates this scoring and is due to be re-run against the fixture
corpus with the shipped verifier before any accuracy claim.
"""

from __future__ import annotations

import bisect
import functools
import re
import unicodedata
from enum import Enum

from pydantic import BaseModel
from rapidfuzz import fuzz
from rapidfuzz.distance import Indel

from creditpaper.ingest.text_norm import Normalised, normalise
from creditpaper.ingest.values import figure_spans

MIN_QUOTE_WORDS = 6
FUZZY_THRESHOLD = 0.90
AMBIGUITY_MARGIN = 0.05
# Distinct edge signatures of unclean exact occurrences snapped and rescored per
# page (bounds repetitive text). Repeated signatures do not count, and every
# occurrence is still checked for being clean.
MAX_EXACT_OCCURRENCES = 50
# Unclean exact occurrences visited per page for rescoring: a time bound for
# degenerate periodic text (the clean check is unbounded). A page left with
# unvisited occurrences counts as a possible tie in the ambiguity decision.
_MAX_EXACT_SCAN = 2000
# Scores are compared after rounding so 0.95 - 0.90 (0.04999999999999993 in
# binary floating point) counts as exactly the margin.
_SCORE_DECIMALS = 9
# Pages whose figure extents are remembered. A document's pages are anchored
# once per extracted fact, and reading a page's figures costs about as much as
# the rest of anchoring it; the cache holds offsets only, keyed by page text.
_FIGURE_CACHE_PAGES = 1024


class AnchorStatus(str, Enum):
    EXACT = "exact"
    FUZZY = "fuzzy"
    TOO_SHORT = "too_short"
    AMBIGUOUS = "ambiguous"
    UNALIGNED = "unaligned"


class AnchorResult(BaseModel):
    status: AnchorStatus
    page: int | None = None
    span: tuple[int, int] | None = None
    score: float = 0.0
    matched_text: str | None = None
    # Pages scoring at or above the threshold and within AMBIGUITY_MARGIN of
    # the best (the best included), ascending. EXACT/FUZZY: (page,).
    # AMBIGUOUS: the tied pages. UNALIGNED/TOO_SHORT: ().
    candidates: tuple[int, ...] = ()


@functools.lru_cache(maxsize=_FIGURE_CACHE_PAGES)
def _raw_figures(raw: str) -> tuple[tuple[int, int], ...]:
    """Raw extents of the page's figures, exactly as the verifier reads them."""
    return tuple(figure_spans(raw))


class _Page(str):
    """Normalised page text carrying its figure extents in normalised offsets.

    `fig_starts` and `fig_ends` are sorted, non-overlapping half-open extents.
    """

    fig_starts: list[int]
    fig_ends: list[int]

    def __new__(cls, text: str, fig_starts: list[int], fig_ends: list[int]) -> _Page:
        page = super().__new__(cls, text)
        page.fig_starts = fig_starts
        page.fig_ends = fig_ends
        return page


def _page(np_: Normalised, raw_figures: tuple[tuple[int, int], ...]) -> _Page:
    """Map raw figure extents to the normalised characters that came from them."""
    starts: list[int] = []
    ends: list[int] = []
    for raw_start, raw_end in raw_figures:
        first = bisect.bisect_right(np_.ends, raw_start)  # first char ending after raw_start
        stop = bisect.bisect_left(np_.offsets, raw_end)  # first char starting at or after raw_end
        if first >= stop:
            continue
        if ends and first < ends[-1]:  # two figures share a character: one block
            ends[-1] = max(ends[-1], stop)
            continue
        starts.append(first)
        ends.append(stop)
    return _Page(np_.text, starts, ends)


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or unicodedata.category(ch).startswith("M")


def _in_word(text: str, p: int) -> bool:
    """True when position `p` (between text[p - 1] and text[p]) is inside a word."""
    return 0 < p < len(text) and _is_word_char(text[p - 1]) and _is_word_char(text[p])


def _figure_around(page: _Page, p: int) -> int | None:
    """Index of the figure strictly containing position `p`, else None."""
    i = bisect.bisect_left(page.fig_starts, p) - 1  # the last figure starting before p
    if i >= 0 and page.fig_ends[i] > p:
        return i
    return None


def _cut_ok(page: _Page, p: int) -> bool:
    return not _in_word(page, p) and _figure_around(page, p) is None


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start] == " ":
        start += 1
    while end > start and text[end - 1] == " ":
        end -= 1
    return (start, end)


def _outward(page: _Page, start: int, end: int) -> tuple[int, int]:
    """Extend [start, end) to the enclosing word and figure boundaries."""
    while True:
        while _in_word(page, start):
            start -= 1
        i = _figure_around(page, start)
        if i is None:
            break
        start = page.fig_starts[i]
    while True:
        while _in_word(page, end):
            end += 1
        i = _figure_around(page, end)
        if i is None:
            break
        end = page.fig_ends[i]
    return (start, end)


def _inward(page: _Page, start: int, end: int) -> tuple[int, int]:
    """Drop any partial word or figure at either edge of [start, end), then edge spaces."""
    while start < end:
        if _in_word(page, start):
            start += 1
            continue
        i = _figure_around(page, start)
        if i is not None:
            start = page.fig_ends[i]
        elif page[start] == " ":
            start += 1
        else:
            break
    while end > start:
        if _in_word(page, end):
            end -= 1
            continue
        i = _figure_around(page, end)
        if i is not None:
            end = page.fig_starts[i]
        elif page[end - 1] == " ":
            end -= 1
        else:
            break
    return (start, end)


def _in_figure(page: _Page, i: int) -> bool:
    """True when the character page[i] belongs to a figure."""
    k = bisect.bisect_right(page.fig_starts, i) - 1
    return k >= 0 and page.fig_ends[k] > i


def _over_punctuation_left(page: _Page, start: int) -> int:
    """Move `start` left over spaces and punctuation to the previous word or figure."""
    while start > 0 and not _is_word_char(page[start - 1]) and not _in_figure(page, start - 1):
        start -= 1
    while page[start : start + 1] == " ":
        start += 1
    return start


def _over_punctuation_right(page: _Page, end: int) -> int:
    """Move `end` right over spaces and punctuation to the next word or figure."""
    while end < len(page) and not _is_word_char(page[end]) and not _in_figure(page, end):
        end += 1
    while end > 0 and page[end - 1] == " ":
        end -= 1
    return end


def _snap_edges(page: _Page, start: int, end: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Candidate start and end positions for [start, end) (already trimmed).

    Each edge: outward to the enclosing word/figure boundary, inward past a
    partial word/figure, or outward and then on over adjacent spaces and
    punctuation to the next word or figure (round 5, N1(b): identical text
    scores the same whether or not the aligner's window took the punctuation).
    """
    out_start, out_end = _outward(page, start, end)
    in_start, in_end = _inward(page, start, end)
    starts = (out_start, in_start, _over_punctuation_left(page, out_start))
    ends = (out_end, in_end, _over_punctuation_right(page, out_end))
    return tuple(dict.fromkeys(starts)), tuple(dict.fromkeys(ends))


def _snap(needle: str, page: _Page, start: int, end: int) -> tuple[float, tuple[int, int]] | None:
    """Best-scoring span around [start, end) cutting no word or figure: (score, span).

    Each edge independently takes one of its candidate positions (`_snap_edges`);
    the combination with the highest Indel similarity to `needle` wins, earlier
    candidates (outward first) on ties. None if no non-empty span remains.
    """
    start, end = _trim(page, start, end)
    if start >= end:
        return None
    starts, ends = _snap_edges(page, start, end)
    best: tuple[float, tuple[int, int]] | None = None
    for s in starts:
        for e in ends:
            if s >= e or not (_cut_ok(page, s) and _cut_ok(page, e)):
                continue
            score = Indel.normalized_similarity(needle, page[s:e])
            if best is None or score > best[0]:
                best = (score, (s, e))
    return best


def _word_clean_occurrences(needle: str) -> re.Pattern[str]:
    """Every (overlapping) occurrence of `needle` whose edges are not mid-word.

    A C-speed prefilter for the clean check: it rejects an occurrence only when
    an alphanumeric edge character of the quote touches an alphanumeric page
    character - which `_cut_ok` rejects too - so no clean occurrence is lost.
    Survivors are verified by `_cut_ok` (combining marks, figures).
    """
    before = "(?<![^\\W_])" if needle[:1].isalnum() else ""
    after = "(?![^\\W_])" if needle[-1:].isalnum() else ""
    return re.compile("(?=" + before + re.escape(needle) + after + ")")


def _exact_on_page(
    needle: str, hay: _Page, clean_scan: re.Pattern[str]
) -> tuple[float, tuple[int, int], bool, bool] | None:
    """Best exact occurrence of `needle`: (score, span, clean, complete).

    A clean occurrence (no edge inside a word or a figure) wins outright with
    score 1.0, wherever it sits on the page: every occurrence is checked. With
    none, occurrences are snapped and rescored and the best is returned as not
    clean. An occurrence whose edge context repeats an earlier one would score
    identically and is not rescored. `complete` is False when an occurrence was
    left unscored: after MAX_EXACT_OCCURRENCES distinct edge contexts, or after
    _MAX_EXACT_SCAN occurrences (a time bound for degenerate, periodic text).
    """
    for match in clean_scan.finditer(hay):
        pos = match.start()
        if _cut_ok(hay, pos) and _cut_ok(hay, pos + len(needle)):
            return (1.0, (pos, pos + len(needle)), True, True)
    best: tuple[float, tuple[int, int]] | None = None
    seen: set[tuple[tuple[str, bool], ...]] = set()
    pos = hay.find(needle)
    scanned = 0
    while pos != -1 and len(seen) < MAX_EXACT_OCCURRENCES and scanned < _MAX_EXACT_SCAN:
        scanned += 1
        end = pos + len(needle)
        starts, ends = _snap_edges(hay, pos, end)
        # The text between each candidate edge and the occurrence, and whether
        # the edge is a legal cut, fix the text of every snap candidate.
        edges = tuple(
            [(f"+{hay[s:pos]}" if s <= pos else f"-{hay[pos:s]}", _cut_ok(hay, s)) for s in starts]
            + [("|", False)]  # separates the start and the end candidates
            + [(f"+{hay[end:e]}" if e >= end else f"-{hay[e:end]}", _cut_ok(hay, e)) for e in ends]
        )
        if edges not in seen:
            seen.add(edges)
            snapped = _snap(needle, hay, pos, end)
            if snapped is not None and (best is None or snapped[0] > best[0]):
                best = snapped
        pos = hay.find(needle, pos + 1)
    if best is None:
        return None
    return (best[0], best[1], False, pos == -1)


def _best_on_page(needle: str, hay: _Page) -> tuple[float, tuple[int, int] | None]:
    """Fuzzy candidate: the aligned region snapped, scored on the whole quote."""
    if not needle or not hay:
        return (0.0, None)
    alignment = fuzz.partial_ratio_alignment(needle, hay)
    if alignment is None:
        return (0.0, None)
    snapped = _snap(needle, hay, alignment.dest_start, alignment.dest_end)
    if snapped is None:
        return (0.0, None)
    return snapped


def _cuts_a_figure(raw_figures: tuple[tuple[int, int], ...], start: int, end: int) -> bool:
    """True when the raw span [start, end) partly covers a raw figure extent."""
    for p in (start, end):
        i = bisect.bisect_left(raw_figures, (p, p)) - 1  # the last figure starting before p
        if i >= 0 and raw_figures[i][1] > p:
            return True
    return False


def _word_cuts(needle: str, text: str) -> int | None:
    """Fewest word-cut edges (0, 1 or 2) over the occurrences of `needle` in `text`.

    None when `needle` does not occur. A figure may be cut: only words count.
    """
    if not needle or needle not in text:
        return None
    before = "(?<![^\\W_])" if needle[:1].isalnum() else ""
    after = "(?![^\\W_])" if needle[-1:].isalnum() else ""
    body = re.escape(needle)
    if re.search(before + body + after, text):
        return 0
    if re.search(before + body, text) or re.search(body + after, text):
        return 1
    return 2


def _pages_holding(quote: str, matched: str, best_page: int, pages: dict[int, str]) -> set[int]:
    """Other pages as likely a source as `best_page` (round 5, N1(a)).

    Another page ties when it holds the matched text with no word cut, or the
    quote itself with no more word cuts than the best page's own occurrence of
    it has (a quote cut inside "01" of "2028-01-06" on its source page, and
    inside "41" of "2028-41-06" on another, identifies neither) - or with any
    number of word cuts when the best page does not hold the quote at all
    ("... $60 m" is on neither "$60 million" nor "$63 m"). A figure may be
    cut: the same characters standing clean on one page and splitting a figure
    on another leave the source unknown.
    """
    best_cuts = _word_cuts(quote, pages[best_page])
    ties: set[int] = set()
    for page, text in pages.items():
        if page == best_page:
            continue
        cuts = _word_cuts(quote, text)
        if (
            _word_cuts(matched, text) == 0
            or cuts == 0
            or (cuts is not None and (best_cuts is None or cuts <= best_cuts))
        ):
            ties.add(page)
    return ties


def _word_count(normalised_quote: str) -> int:
    return sum(1 for token in normalised_quote.split() if any(ch.isalnum() for ch in token))


def _within_margin(best: float, other: float) -> bool:
    return round(best - other, _SCORE_DECIMALS) < AMBIGUITY_MARGIN


def find_quote(
    quote: str,
    pages: dict[int, str],
    *,
    threshold: float = FUZZY_THRESHOLD,
    min_words: int = MIN_QUOTE_WORDS,
) -> AnchorResult:
    """Locate ``quote`` on ``pages`` (module docstring).

    ``min_words`` lowers the word minimum for ONE caller only: the table layer
    (``tables.py``), which accepts a shorter table-row quote solely when the
    result is EXACT and the row's column is proven against its header. Every
    other caller keeps MIN_QUOTE_WORDS.
    """
    nq = normalise(quote)
    if _word_count(nq.text) < min_words:
        return AnchorResult(status=AnchorStatus.TOO_SHORT)

    clean_scan = _word_clean_occurrences(nq.text)
    # (score, page, raw span, clean exact, raw page, every occurrence scored,
    #  normalised span text)
    scored: list[tuple[float, int, tuple[int, int], bool, str, bool, str]] = []
    normalised: dict[int, str] = {}
    for page, raw in sorted(pages.items()):
        np_ = normalise(raw)
        normalised[page] = np_.text
        raw_figures = _raw_figures(raw)
        hay = _page(np_, raw_figures)
        exact = _exact_on_page(nq.text, hay, clean_scan)
        if exact is not None:
            score, nspan, clean, complete = exact
        else:
            score, fspan = _best_on_page(nq.text, hay)
            if fspan is None:
                continue
            nspan, clean, complete = fspan, False, True
        raw_span = np_.raw_span(*nspan)
        if _cuts_a_figure(raw_figures, *raw_span):
            continue  # ruled out by the mapping; a cut figure is never cited
        scored.append((score, page, raw_span, clean, raw, complete, hay[nspan[0] : nspan[1]]))

    if not scored:
        return AnchorResult(status=AnchorStatus.UNALIGNED)

    # Stable sort over ascending page keys: a true tie favours the lower page.
    scored.sort(key=lambda row: row[0], reverse=True)
    best_score, best_page, (raw_start, raw_end), clean, raw, _, best_text = scored[0]
    if best_score < threshold:
        return AnchorResult(status=AnchorStatus.UNALIGNED, score=best_score)
    # A page with occurrences left unscored might hold a better one: it counts
    # as a possible tie (fail-safe), never as a lesser page.
    unscored = {row[1] for row in scored[1:] if not row[5]}
    # Hidden ambiguity (round 5, N1(a)): a page holding the same text is as
    # likely a source as the best page, whatever its own snapped score came to.
    ties = unscored | _pages_holding(nq.text, best_text, best_page, normalised)
    candidates = tuple(
        sorted(
            row[1]
            for row in scored
            if row[1] == best_page
            or row[1] in ties
            or (row[0] >= threshold and _within_margin(best_score, row[0]))
        )
    )
    if ties or (len(scored) > 1 and _within_margin(best_score, scored[1][0])):
        return AnchorResult(status=AnchorStatus.AMBIGUOUS, score=best_score, candidates=candidates)
    return AnchorResult(
        status=AnchorStatus.EXACT if clean else AnchorStatus.FUZZY,
        page=best_page,
        span=(raw_start, raw_end),
        score=best_score,
        matched_text=raw[raw_start:raw_end],
        candidates=candidates,
    )
