"""Plan the read over a whole deal's corpus, with no hints and no document types.

`windows.py` plans windows for ONE field group using hint phrases from
`page_selector._PATTERNS` — it answers "which pages might hold the QS costs?".
This module answers a different question: given every page of every document,
what does a general extraction pass actually send to a model, and once?

Three rules, each with a measurement behind it (2026-09-22, real 144-document
folder, 3,168 pages):

  * PREFILTER. 31.8% of pages are dividers, plan sheets and signature pages
    carrying almost no text. Not one of them held a currency amount, and only
    2.0% held a date or a fact keyword, so they are dropped deterministically —
    with a RESCUE for that 2.0%, which costs 20 pages and makes the filter
    degrade safely on a corpus that is not this one.

  * DEDUPE THE READ, NOT THE FACT. A deal folder repeats itself: the same term
    sheet filed in three places, plan sets reprinted across submissions. Reading
    it three times is waste. But `model/apply.py` carries `ApplyMode.CONFIRM` —
    a fact from a SECOND document that agrees upgrades the field
    `extracted` -> `confirmed`, and one that diverges flags it, preserving both
    values — so suppressing the second document's facts destroys corroboration,
    which is worth more than the tokens.

    (That mechanism had NO CALLER in `src/` until 2026-09-22: `ingest()` applied
    every output with the default EXTRACT, so a second document silently
    overwrote the first. It is wired now; this module's fan-out is what feeds
    it.)

    So the READ is deduplicated and the FACTS are not: identical content is sent
    to the model once, and the facts that come back are emitted once per
    LOCATION it appeared at. `apply` still sees N facts from N documents, CONFIRM
    still fires, and the model call is still made once.

  * DEDUPE AT THE WINDOW, NOT THE PAGE. Page-level dedupe removes pages
    scattered through a document and leaves a sparse remainder, so a "10-page
    window" becomes pages 3, 7, 12, 28 — non-contiguous text that reads as
    nonsense and breaks any fact spanning a page boundary. Hashing the WINDOW
    keeps every window contiguous and still catches the case that actually
    matters, which is a whole document filed twice.

Pure: no I/O, no model calls, no database. Takes parsed pages, returns a plan.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence

from pydantic import BaseModel

from creditpaper.ingest.anchor import MIN_QUOTE_WORDS

# Measured thresholds. A page below BOTH is treated as structural unless it
# carries a rescue signal.
MIN_CHARS = 200
MIN_WORDS = 30
DEFAULT_WINDOW = 10

_WORD = re.compile(r"[A-Za-z]{2,}")
_MONEY = re.compile(r"\$\s*\d")
_DATE = re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b")
_KEYWORD = re.compile(
    r"\b(polic|insur|sum insured|expiry|certificate|licence|license|amount|total|"
    r"sum|rate|interest|valuation|market value|executed|dated|signed|abn|acn|"
    r"title|lot\b|folio|settlement)",
    re.I,
)


class Page(BaseModel):
    """One parsed page, as it came out of the document."""

    document_id: str
    page: int
    text: str
    # Whether this text came from OCR rather than the page's own text layer.
    # NOT part of `_content`, so it cannot change a window's hash and invalidate
    # the fact cache: it describes how the text was obtained, not what it says.
    ocr_used: bool = False


class Location(BaseModel, frozen=True):
    """Where a piece of content sits. A fact gets one of these per appearance."""

    document_id: str
    page: int


class ReadWindow(BaseModel):
    """Contiguous pages of ONE document, sent to a model exactly once.

    `locations` names every place this window's content appears, primary first.
    A window read once but present in three documents carries three locations,
    and its facts are emitted three times — see `fan_out`.
    """

    document_id: str
    pages: tuple[int, ...]
    page_texts: tuple[str, ...]
    text: str
    content_hash: str
    locations: tuple[Location, ...]
    # Which of `pages` were read by OCR. A fact anchored there is capped, the
    # way a vision transcription is (`verify.Reason.OCR_TEXT`).
    ocr_pages: frozenset[int] = frozenset()

    @property
    def is_echo(self) -> bool:
        """True when this content was already read elsewhere in the corpus."""
        return len(self.locations) > len(self.pages)

    def numbered(self) -> str:
        """The window with every line numbered, for LINE-ANCHORED extraction.

        Asking the model to quote a clause makes the ANCHOR the model's
        responsibility, and it fails in two ways we measured: a quote too short
        to locate (9.9-27.0% of facts) and a quote that does not locate at all
        (5.1-16.7%). Both are the model being asked to reproduce text.

        Pointing at a line instead makes the anchor OURS. The clause is then
        built from the page text we already hold, so it exists on the page by
        construction and cannot fail to anchor — and the model writes a number
        instead of a sentence, which is most of the output cost at small
        windows.
        """
        out: list[str] = []
        n = 0
        for page, text in zip(self.pages, self.page_texts, strict=True):
            out.append(f"--- page {page} ---")
            for line in text.splitlines():
                n += 1
                out.append(f"{n}| {line}")
        return "\n".join(out)

    def line_map(self) -> dict[int, tuple[int, str, int, int]]:
        """`{line: (page, text, start, end)}` for the numbering `numbered()` emits.

        `start` and `end` are character offsets into THAT PAGE's text. They are
        what makes a cited line a complete anchor: with them the clause's exact
        position is already known, so nothing has to be searched for and
        `find_quote` can be skipped entirely.

        Page headers are not numbered, so a line number always names real page
        content and never a separator we inserted.
        """
        out: dict[int, tuple[int, str, int, int]] = {}
        n = 0
        for page, text in zip(self.pages, self.page_texts, strict=True):
            offset = 0
            for line in text.splitlines(keepends=True):
                stripped = line.rstrip("\r\n")
                n += 1
                out[n] = (page, stripped, offset, offset + len(stripped))
                offset += len(line)
        return out

    def texts_by_page(self) -> dict[int, str]:
        """`{page: text}` as `ingest.verify.verify_fact` wants it.

        The verifier anchors against the page text, NOT the rendered body: the
        `--- page N ---` labels are ours and are not on the page, so a quote
        spanning one would never be found.
        """
        return dict(zip(self.pages, self.page_texts, strict=True))


class CorpusPlan(BaseModel):
    windows: tuple[ReadWindow, ...]
    pages_total: int
    pages_empty: int
    pages_dropped: int
    pages_rescued: int
    pages_read: int
    windows_deduped: int

    @property
    def calls(self) -> int:
        """Model calls this plan costs — the unit actually billed."""
        return len(self.windows)


def is_structural(text: str) -> bool:
    """A divider, plan sheet or signature page: no prose, and no fact signal.

    The rescue is the second half: a certificate of currency is plausibly one
    sparse page carrying insurer, policy number and expiry, and dropping it to
    save a fraction of a cent would be a bad trade on a corpus where that
    happens more than it does on this one.
    """
    stripped = text.strip()
    if not stripped:
        return True
    if len(stripped) >= MIN_CHARS and len(_WORD.findall(stripped)) >= MIN_WORDS:
        return False
    return not (_MONEY.search(stripped) or _DATE.search(stripped) or _KEYWORD.search(stripped))


def has_rescue_signal(text: str) -> bool:
    """Sparse, but carries a currency amount, a date or a fact keyword."""
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) >= MIN_CHARS and len(_WORD.findall(stripped)) >= MIN_WORDS:
        return False
    return bool(_MONEY.search(stripped) or _DATE.search(stripped) or _KEYWORD.search(stripped))


def _normalised(text: str) -> str:
    return " ".join(text.split()).casefold()


def _hash(text: str) -> str:
    return hashlib.sha256(_normalised(text).encode("utf-8")).hexdigest()


def _render(pages: Sequence[Page]) -> str:
    """The window body the model sees. Page numbers are labelled so a returned
    `page` can be resolved back to a real page of a real document."""
    return "\n\n".join(f"--- page {p.page} ---\n{p.text.strip()}" for p in pages)


def _content(pages: Sequence[Page]) -> str:
    """What the window SAYS, with no page labels — the thing to hash.

    Hashing the rendered body instead would put `--- page 45 ---` inside the
    digest, so a term sheet filed standalone at pages 1-8 and again inside a
    bundle at pages 45-52 would never match. That is precisely the duplication
    worth catching, so the label is rendering, not identity.
    """
    return "\n\n".join(p.text.strip() for p in pages)


def plan_corpus(pages: Iterable[Page], *, window: int = DEFAULT_WINDOW) -> CorpusPlan:
    """Every page in, the windows a general pass should actually send out.

    Pages are grouped by document in the order given, so a caller controls
    document order and page order; this does not sort them. Windows never cross
    a document boundary, because an anchor has to resolve to one page of one
    document.
    """
    by_document: dict[str, list[Page]] = {}
    total = empty = dropped = rescued = 0
    for page in pages:
        total += 1
        if not page.text.strip():
            empty += 1
            continue
        if is_structural(page.text):
            dropped += 1
            continue
        if has_rescue_signal(page.text):
            rescued += 1
        by_document.setdefault(page.document_id, []).append(page)

    seen: dict[str, ReadWindow] = {}
    order: list[str] = []
    read_pages = 0
    deduped = 0

    for document_id, kept in by_document.items():
        for start in range(0, len(kept), window):
            chunk = kept[start : start + window]
            body = _render(chunk)
            digest = _hash(_content(chunk))
            here = tuple(Location(document_id=document_id, page=p.page) for p in chunk)
            existing = seen.get(digest)
            if existing is not None:
                # Same content, already sent. Record where it also lives so the
                # facts fan out to it, and do NOT pay for the call again.
                seen[digest] = existing.model_copy(update={"locations": existing.locations + here})
                deduped += 1
                continue
            read_pages += len(chunk)
            seen[digest] = ReadWindow(
                document_id=document_id,
                pages=tuple(p.page for p in chunk),
                page_texts=tuple(p.text for p in chunk),
                ocr_pages=frozenset(p.page for p in chunk if p.ocr_used),
                text=body,
                content_hash=digest,
                locations=here,
            )
            order.append(digest)

    return CorpusPlan(
        windows=tuple(seen[d] for d in order),
        pages_total=total,
        pages_empty=empty,
        pages_dropped=dropped,
        pages_rescued=rescued,
        pages_read=read_pages,
        windows_deduped=deduped,
    )


class Located(BaseModel, frozen=True):
    """A clause whose position is already known — nothing to search for.

    `text` is cut verbatim out of `page`'s text at `span`, so it is not a quote
    to be matched but the page itself. `find_quote` would be asked to locate a
    string it was handed from the very text it searches.
    """

    text: str
    page: int
    span: tuple[int, int]
    # The CITED LINE's own extent, which is usually narrower than `span`:
    # `clause_at` grows the clause over neighbouring lines to reach the word
    # minimum, and may grow BACKWARDS, so `span` neither starts on the cited
    # line nor ends with it.
    #
    # Carried because a table row is a LINE. Reading a table from `span` finds
    # nothing — the clause runs past the row's last figure into the next line,
    # which is exactly what `tables._enumerable_row` refuses. Measured
    # 2026-09-24: column enumeration fired 0 times on 879 windows of a real
    # corpus while passing its unit test, because the test handed it a line and
    # production handed it a clause.
    line_span: tuple[int, int]


def clause_at(window: ReadWindow, line: int, *, min_words: int = MIN_QUOTE_WORDS) -> Located | None:
    """The located clause for a cited line, or None if the line is not in the window.

    The clause is grown from the cited line until it carries at least
    `min_words`, taking following lines first and preceding ones only if the
    page runs out. It never crosses a page boundary, because an anchor has to
    resolve to one page.

    This is the half that makes line anchoring worth doing. The text comes from
    the page we already hold, so:

      * it ALWAYS anchors — `find_quote` sees a string copied out of the very
        text it searches, so TOO_SHORT and UNALIGNED become unreachable;
      * the minimum is guaranteed rather than requested, so
        REFUSED_SHORT_QUOTE stops being a category.

    What remains is `value_in_span` — whether the value the model reported is
    actually on the line it pointed at. That is the check worth keeping, and it
    becomes the ONLY way a line-anchored fact can fail.
    """
    mapping = window.line_map()
    if line not in mapping:
        return None
    page = mapping[line][0]
    same_page = [n for n, row in sorted(mapping.items()) if row[0] == page]

    taken = [line]
    words = len(mapping[line][1].split())
    after = [n for n in same_page if n > line]
    before = [n for n in reversed(same_page) if n < line]
    while words < min_words and (after or before):
        # Forward first, then backward. Growth stays CONTIGUOUS either way, so
        # the result is a single character range rather than a stitched-together
        # string that appears nowhere on the page.
        nxt = after.pop(0) if after else before.pop(0)
        taken.append(nxt)
        words += len(mapping[nxt][1].split())

    start = mapping[min(taken)][2]
    end = mapping[max(taken)][3]
    page_text = dict(zip(window.pages, window.page_texts, strict=True))[page]
    return Located(
        text=page_text[start:end],
        page=page,
        span=(start, end),
        line_span=(mapping[line][2], mapping[line][3]),
    )


def fan_out(window: ReadWindow, page: int) -> tuple[Location, ...]:
    """Every location a fact anchored to `page` of `window` should be filed at.

    This is the half of dedupe that must NOT be skipped. The window was read
    once; the fact it produced belongs to every document that contains that
    content, because `apply`'s CONFIRM mode is the system's only free source of
    corroboration and it fires on a SECOND document agreeing.

    Returns one Location per appearance, in corpus order. A window that is not
    an echo returns exactly one.
    """
    if not window.pages:
        return ()
    try:
        offset = window.pages.index(page)
    except ValueError:
        # The model cited a page outside this window. Attribute to the window's
        # first page rather than guessing; the verifier will judge the anchor.
        offset = 0
    stride = len(window.pages)
    return tuple(
        window.locations[start + offset]
        for start in range(0, len(window.locations), stride)
        if start + offset < len(window.locations)
    )
