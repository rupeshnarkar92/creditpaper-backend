"""Normalise page text for quote matching, keeping a map back to raw offsets.

Naive substring matching of an extractor's quote against raw PDF page text
fails 93.2% of the time on the fixture corpus (2026-09-18 measurement): PDFs
carry curly quotes, ligatures, non-breaking spaces and words hyphenated across
line breaks, while an LLM emits a single clean line. Normalising both sides
lifts exact matches to 96.0%. The offset map exists so a match found in
normalised space can still cite a position in the original page text.

Each output character carries the half-open raw range ``[offsets[k], ends[k])``
it came from. Canonical composition (NFC) is applied per *cluster*: a starter
plus every following combining mark (category M or non-zero combining class)
and Hangul medial/final jamo. Those are the only characters NFC ever merges
across - every non-Hangul primary composite in Unicode has a combining mark as
its second element (checked over the full code space) - so composing cluster by
cluster equals composing the whole string, while each output character maps to
the whole cluster it came from. A span ending on a decomposed letter therefore
slices all of that letter's marks, and nothing after a multi-mark letter, a
jamo syllable or an NFC-expanding composition exclusion drifts.

Invisible format characters (Unicode category Cf: soft hyphen, zero-width
space and joiners, word joiner, byte-order mark, bidi controls) never reach the
text. A soft hyphen before a line break rejoins the word it splits, like a hard
hyphen does; anywhere else it is dropped.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_CHAR_MAP = {
    "\u2018": "'",  # U+2018 left single quotation mark
    "\u2019": "'",  # U+2019 right single quotation mark
    "\u201a": "'",  # U+201A single low-9 quotation mark
    "\u201b": "'",  # U+201B single high-reversed-9 quotation mark
    "\u201c": '"',  # U+201C left double quotation mark
    "\u201d": '"',  # U+201D right double quotation mark
    "\u201e": '"',  # U+201E double low-9 quotation mark
    "\u2013": "-",  # U+2013 en dash
    "\u2014": "-",  # U+2014 em dash
    "\u2212": "-",  # U+2212 minus sign
    "\u2010": "-",  # U+2010 hyphen
    "\u2011": "-",  # U+2011 non-breaking hyphen
    "\u2012": "-",  # U+2012 figure dash
    "\uff0d": "-",  # U+FF0D fullwidth hyphen-minus
    "\u00a0": " ",  # U+00A0 non-breaking space
    "\u2000": " ",  # U+2000 en quad
    "\u2001": " ",  # U+2001 em quad
    "\u2002": " ",  # U+2002 en space
    "\u2003": " ",  # U+2003 em space
    "\u2004": " ",  # U+2004 three-per-em space
    "\u2005": " ",  # U+2005 four-per-em space
    "\u2006": " ",  # U+2006 six-per-em space
    "\u2007": " ",  # U+2007 figure space
    "\u2008": " ",  # U+2008 punctuation space
    "\u2009": " ",  # U+2009 thin space
    "\u200a": " ",  # U+200A hair space
    "\u202f": " ",  # U+202F narrow no-break space
    "\u205f": " ",  # U+205F medium mathematical space
    "\u3000": " ",  # U+3000 ideographic space
    "\u2026": "...",  # U+2026 horizontal ellipsis
}
_LIGATURES = {
    "\ufb00": "ff",  # U+FB00 latin small ligature ff
    "\ufb01": "fi",  # U+FB01 latin small ligature fi
    "\ufb02": "fl",  # U+FB02 latin small ligature fl
    "\ufb03": "ffi",  # U+FB03 latin small ligature ffi
    "\ufb04": "ffl",  # U+FB04 latin small ligature ffl
}
_SOFT_HYPHEN = "\u00ad"
# Hyphens that rejoin a word split across a line break.
_JOIN_HYPHENS = frozenset("-\u2010\u2011" + _SOFT_HYPHEN)
# Every character Python's str.splitlines treats as a line or paragraph break.
_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
# Characters below U+0300 (no marks, no Cf but the soft hyphen, all NFC-stable).
_SIMPLE_RUN = re.compile("[\x00-\u02ff]+")


@dataclass(frozen=True)
class Normalised:
    """Normalised text plus, per character, the half-open raw range it came from.

    ``offsets[k]`` is the raw start and ``ends[k]`` the raw end of the source
    of ``text[k]``.
    """

    text: str
    offsets: list[int]
    ends: list[int]

    def raw_span(self, start: int, end: int) -> tuple[int, int]:
        """Map a half-open span in `text` to a half-open span in the raw input.

        Raises ValueError unless ``0 <= start < end <= len(text)``: clamping an
        invalid span would silently cite raw text nobody matched.
        """
        if not 0 <= start < end <= len(self.text):
            raise ValueError(f"span ({start}, {end}) is invalid for text of length {len(self.text)}")
        return (self.offsets[start], self.ends[end - 1])


def _joins_cluster(ch: str) -> bool:
    cp = ord(ch)
    return (
        unicodedata.category(ch).startswith("M")
        or unicodedata.combining(ch) != 0
        # Conjoining medial (V) and final (T) jamo: category Lo, but NFC folds
        # them into a preceding L jamo or LV syllable.
        or 0x1161 <= cp <= 0x1175
        or 0x11A8 <= cp <= 0x11C2
    )


def _is_dropped(ch: str) -> bool:
    return ch != _SOFT_HYPHEN and unicodedata.category(ch) == "Cf"


def _composed(raw: str) -> tuple[list[str], list[int], list[int]]:
    """NFC `raw` cluster by cluster, minus Cf characters, with a raw range per char.

    A Cf character inside a cluster is skipped without splitting it, so it
    cannot block composition. The soft hyphen is kept (it is Cf) so the
    line-break join can see it.
    """
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    n = len(raw)
    pos = 0
    while pos < n:
        # Fast path: a run of characters below U+0300 is NFC-stable and holds
        # no marks and no Cf other than the soft hyphen (checked over the
        # range), so each is its own cluster - unless the next character
        # joins the run's last one, which then goes through the slow path.
        run = _SIMPLE_RUN.match(raw, pos)
        if run is not None:
            stop = run.end()
            if stop < n and (_joins_cluster(raw[stop]) or _is_dropped(raw[stop])):
                stop -= 1
            chars.extend(raw[pos:stop])
            starts.extend(range(pos, stop))
            ends.extend(range(pos + 1, stop + 1))
            pos = stop
            if pos == run.end():
                continue
        if _is_dropped(raw[pos]):
            pos += 1
            continue
        members = [pos]
        nxt = pos + 1
        while nxt < n:
            if _is_dropped(raw[nxt]):
                nxt += 1
            elif _joins_cluster(raw[nxt]):
                members.append(nxt)
                nxt += 1
            else:
                break
        start, end = members[0], members[-1] + 1
        for ch in unicodedata.normalize("NFC", "".join(raw[i] for i in members)):
            chars.append(ch)
            starts.append(start)
            ends.append(end)
        pos = nxt
    return chars, starts, ends


def _is_horizontal_space(ch: str) -> bool:
    return ch.isspace() and ch not in _LINE_BREAKS


def _hyphen_join_target(chars: list[str], i: int) -> int | None:
    """Index of the letter or digit a line-break hyphen at `i` rejoins to, else None.

    Exactly one line break ("\\r\\n" counts as one) with optional horizontal
    space either side; a blank line between is a paragraph break, not a split.
    """
    n = len(chars)
    j = i + 1
    while j < n and _is_horizontal_space(chars[j]):
        j += 1
    if j >= n or chars[j] not in _LINE_BREAKS:
        return None
    j += 2 if chars[j] == "\r" and j + 1 < n and chars[j + 1] == "\n" else 1
    while j < n and _is_horizontal_space(chars[j]):
        j += 1
    if j < n and (chars[j].isalpha() or chars[j].isdigit()):
        return j
    return None


def normalise(raw: str) -> Normalised:
    chars, starts, ends = _composed(raw)
    out: list[str] = []
    out_starts: list[int] = []
    out_ends: list[int] = []
    pending_space: int | None = None  # raw index of the first char of a whitespace run
    i = 0
    n = len(chars)
    while i < n:
        ch = chars[i]
        # A hyphen at a line end, attached to the word before it (no space
        # between), splits that word: after a letter the word rejoins; after a
        # digit the hyphen is kept and the break dropped, before a letter or a
        # digit ("12-\nmonth" -> "12-month", "2025-\n2026" -> "2025-2026").
        # Letter, hyphen, break, digit ("pre-\n2026") is not joined. A spaced
        # dash ("Borrower -\nwhich") is punctuation, not a split, and stays.
        if ch in _JOIN_HYPHENS and pending_space is None and out:
            target = _hyphen_join_target(chars, i)
            if target is not None and out[-1].isalpha() and chars[target].isalpha():
                i = target
                continue
            if target is not None and out[-1].isdigit() and ch != _SOFT_HYPHEN:
                out.append("-")
                out_starts.append(starts[i])
                out_ends.append(ends[i])
                i = target
                continue
        if ch == _SOFT_HYPHEN:
            i += 1
            continue
        if ch.isspace():
            if out and pending_space is None:
                pending_space = starts[i]
            i += 1
            continue
        if pending_space is not None:
            # A whitespace character is a starter, so it heads its own cluster
            # and is exactly one raw character long.
            out.append(" ")
            out_starts.append(pending_space)
            out_ends.append(pending_space + 1)
            pending_space = None
        mapped = (_LIGATURES.get(ch) or _CHAR_MAP.get(ch) or ch).casefold()
        for piece in mapped:
            out.append(piece)
            out_starts.append(starts[i])
            out_ends.append(ends[i])
        i += 1
    return Normalised(text="".join(out), offsets=out_starts, ends=out_ends)
