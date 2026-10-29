"""Figures, GST basis, and the value-in-span check.

A verified quote does NOT prove the figure inside it. Mutating one or two digits
of a true quote still matched 100% of the time at every fuzzy threshold
(2026-09-18 measurement), median score 0.992 - one wrong digit barely moves a
similarity score. So every fact carries a second leg: the value's figures must
appear in the MATCHED SPAN. Scoped to the span this catches 96.5% of corrupted
figures with no false alarms; scoped to the whole page it catches 90.6%, because
a page usually contains some other number that happens to match.

GST basis is tracked because incl/excl GST on a contract sum or cost to complete
is one of the most damaging misreads in a construction paper.

The verifier's API
------------------
Task 4's verifier MUST call ``value_in_page_span(value, page_text, start, end,
kind=...)`` with the anchor's raw page offsets. It reads the figures of the
whole page and keeps those lying wholly inside the span. Re-reading the bare
span text changes what it says ("Lot 5 June 2027" cut to "5 June 2027" becomes
a date; "A-500,000" cut to "-500,000" gains a sign; "$3m-5m" cut to "5m" loses
its currency), so ``value_in_span(value, span_text)`` is only for text that IS
the whole context.

One definition of a figure
--------------------------
``figures(text)`` is the single reader of figures: ``numeric_tokens`` and
``figure_spans`` are views of it, and the anchor snaps span edges to
``figure_spans`` so a matched span never cuts a figure. Each ``Figure`` carries
a canonical ``value``, a ``kind`` (money, percent, date, number, ambiguous) and
the raw ``[start, end)`` extent of its whole written form - sign, currency,
number, magnitude and percent unit.

One left-to-right scanner reads them. At each candidate position it tries a
date, then a number; the first that matches is emitted and the scanner jumps
past it, so figures come back in document order and never overlap. The number
grammar is::

    sign? currency? number magnitude? percent?

- number: comma-grouped ``\\d{1,3}(,\\d{3})+`` or plain digits, then an optional
  decimal part; a leading ``.5`` reads as 0.5. A malformed grouping ("1,0000",
  "5,12") is never fused. Space-grouped thousands ("$12 400 000", also with a
  no-break, thin or narrow no-break space) fuse ONLY behind a currency; without
  one they stay separate ("Level 12 400 George St" is an address). A number
  starts at any digit not preceded by a digit, so letters glued to it
  ("FY2025", "AUD12,400,000", "ICR 3.5x") never split or truncate it.
- a grouping continues across ONE line break: after a line-end comma
  ("12,400,<br>000"), or - behind a currency - where the break stands in for a
  group space and a group space on either side shows it is a grouping
  ("$12 400<br>000", "$12<br>400 000"; not "$500<br>200", a table row). A
  grouping cut by more than one break, or by a second one, is not joined and
  both pieces read AMBIGUOUS, valued "12400," and ",0" - equal to no number.
  (<br> stands for one line break: LF, CR LF, CR, VT, FF, U+001C-U+001E,
  U+0085, U+2028 or U+2029.)
- sign: an UNSPACED "-" or U+2212 directly before the currency or digit, not
  preceded by a word character, "%", ")" or "]". It is a sign after whitespace
  or at a line start, including after a previous number ("Net 1,500,000
  -200,000" in a table row), so "65% -70%" reads -70 - a visible false flag,
  never a false pass. En and em dashes are never signs; a spaced dash ("5 - 3",
  a bullet "- $1,200") is never a sign. Accounting parentheses are NOT
  negative: "($12,400,000)" is 12400000.
- currency: AUD, USD, NZD, EUR, GBP, or any Unicode currency symbol (category
  Sc: $, pound, euro, yen, ...) optionally led by up to three capitals (A$, AU$,
  US$, NZ$, HK$); glued or followed by one space (a tab or any Zs space). A
  code is not recognised inside a longer word.
- magnitude: thousand(s)/million(s)/billion(s) always expand; so do mn, mil,
  mill, mln, bn and bil, with or without a currency. With a currency every
  short suffix expands (k, m, mm/MM = million, b). Without one, a glued k
  expands ("450k"); a bare m, mm or b, or a spaced k, is AMBIGUOUS: the
  figure's value keeps its suffix ("12.4m") and equals neither 12.4 nor
  12400000. A suffix followed by a letter, digit or superscript is not a
  suffix ("$12.4milestone", "$12.4m2"). In a range the second limb inherits
  the first limb's currency ("$3m-5m", "$3m to 5m").
- between the number and its magnitude or unit may stand: whitespace (tabs, Zs
  spaces, line breaks), at most one hyphen (-, U+2010, U+2011 or the soft hyphen
  U+00AD), whitespace. The extent covers it: "$12.4<br>million",
  "$12.4 - million", "$12.4-<br>million", "$1.2<U+202F>m", "12.5<br>per cent".
  A short k, m, b or mm after a line break or a hyphen is AMBIGUOUS, currency or
  not. Across MORE than one line break every magnitude is AMBIGUOUS
  ("12.4<br><br>million" reads "12.4m"); a "%" still attaches, "per cent" does
  not.
- sign after the currency: "$-12.4", "A$-500,000", "$ -500,000" are negative
  money. A sign on both sides ("-$-5") or a "±" is AMBIGUOUS, valued "±5"; a
  sign glued to another dash ("$--5", "5--3") is AMBIGUOUS.
- currency (Task 3c, D10): every money figure carries ``Figure.currency`` - AUD
  for $, A$, AU$, AUD$, $A and AUD; USD for US$, USD$ and USD; NZD for NZ$, NZD$
  and NZD; EUR; GBP; JPY; other symbols as written. A value that states a
  currency matches only the same currency.
- trailing currency (Task 4 fix round 3): a number with no currency before it,
  followed by a currency code (AUD, USD, NZD, EUR, GBP) or by "dollars" -
  optionally "Australian", "US", "United States", "American", "NZ" or "New
  Zealand" dollars - is money in that currency; "dollars" alone is AUD, like "$".
  The extent covers the words: "12,400,000 AUD", "5 million dollars". A figure
  that is already AMBIGUOUS ("12.4m dollars") stays AMBIGUOUS, and a code with
  another number after it is that number's prefix ("Level 12 AUD 71 956").
  Fix round 4: a code followed by a capitalised word is an address or label, not
  a suffix ("Level 12 AUD House" is NUMBER 12); "dollars" followed by a country
  or code qualifier - "(US)", "US", "U.S.", "NZ", any [A-Z]{2,3} other than AUD
  or A - is not read AUD: the number stays a bare NUMBER (fail-safe).
- canonical form uses ``Decimal`` only, never ``float``: magnitude expansion is
  an exact exponent shift, leading zeros and trailing fractional zeros are
  stripped, -0 is 0, and the result is positional (never exponent notation).

Generalised range rule (Task 3c), after the scanner: two figures joined by at
most 24 characters of whitespace, dashes, "~", "/", ",", "&", "+", brackets or
the words to, till, until, thru, through, and, or, plus, minus, vs, up to (at
most one line break on each side of the connector):

- the second carries a magnitude the first lacks: the first is AMBIGUOUS
  ("$1.2-1.5m" reads "1.2m"; "$3, 4 or 5 million"; "$1.2 1.5m"). Unless the
  first is independent: own currency AND own magnitude; or at least 1000x the
  second limb read at the second's magnitude ("$500,000 and $1.2m" plain,
  "$1,200-$1,500m" and "$8,250 and $950k" AMBIGUOUS); or an identifier's number
  (Lot, Year, Stage, Tranche, Schedule, ...) with a second limb carrying its own
  currency ("Year 1 - $5m" plain, "Stage 3 to 5 million" AMBIGUOUS); or a
  written percent, which never borrows a magnitude.
- a bare number before a percent across a real connector is a percent ("3-4.5%").
- only the first limb carries a magnitude: a bare second limb is AMBIGUOUS
  ("$3m-5" reads "5m"), unless it carries a unit word ("$5m, 12 months").
- a magnitude (or OCR look-alike: rn, rnillion, mi11ion, bi11ion; or with a
  footnote digit, "million1") that the grammar did not take, reachable across
  whitespace, dashes, brackets or punctuation, makes a bare figure AMBIGUOUS
  ("$12.4 -- million", "($12.4) million", "$12.4, million").

The allowlist (Task 3c): a figure is definite only when its immediate context
is clean - whitespace or a line boundary on each side; on the right, sentence
punctuation (. , ; :) followed by whitespace or the end, or a known unit ("3.5x",
"12.5pa", "100sqm", "12.4m²" area, "12-month", "$500/sqm"); brackets and quotes
around it are looked through; a dash between two figures is the range shape.
ANY other glued character - superscripts, a digit after a suffix, * † ‡ ^, a
footnote bracket, O/o/l/I confusables, &, "/", ":", unknown letters - makes the
figure AMBIGUOUS. Its value then carries a GLUE mark (U+2047) for each unclean
side: "FY2025" reads "⁇2025", "3/45" reads "3⁇" and "⁇45". A value written the
same way still matches ("3/45" in "3/45 Smith St"), a plain number never does.
OCR digit fragments ("$ 1 2 , 4 0 0") are AMBIGUOUS too. Unknown means flag,
never pass.

Explicit footnote markers (Task 3c tuning) are clean trailing context, consumed
into the extent after any magnitude or unit: superscript digits, * † ‡ §, [n]
and (n) with n of at most 2 digits, ^n. "$5m¹" is 5000000. Not markers: ² or ³
straight after "m"/"mm" (area, volume), an ASCII digit ("$5m1", "7%1" stay
AMBIGUOUS), and a power of ten ("10⁶", "10^6").

Page scale (D13; closing action: flag, never apply): on a page with any scale
marker ("($m)", "$m", "$'000", "$000s", "(millions)", "in thousands", "$
million") every bare NUMBER reads AMBIGUOUS for kind money or None - it may be
in thousands or millions. kind="number" still reads it as written. values.py
never applies a scale; column-aware scaling belongs to table extraction (Plan C).

A figure is never matched on its bare value after dropping context written next
to it (the stopping-rule fix and its closing action):

- A value carrying a glue mark ("<65%", "<US$5m") must also occur as written,
  and not followed - on its line or at the start of the next - by a unit it does
  not write ("<12.4" is not "<12.4<br>years"). Only int, float, Decimal and date
  values skip the written-word match.
- A currency before a number with a spaced count or time unit is that unit's
  number ("in AUD 12 months", "$ 2 tranches"); a spaced currency before a bare
  year, or any money figure followed by a lone currency ("2026 $ 2025 $"), is
  AMBIGUOUS.
- A single-space currency group before a COUNT unit (units, lots, dwellings,
  apartments, tenants, stages, tranches, buildings, floors, levels) is two
  columns ("Rent $850 120 units"); before any other unit word it is AMBIGUOUS
  ("Rent $450 000 pa").
- A tolerance ("±", "+/-", "(+/-)", "plus or minus", in any spacing or across a
  line break) makes the figure AMBIGUOUS, valued "±5"; it keeps its currency.
- Interjections before a magnitude - (), [], {}, dash pairs, repeated - are
  looked past ("$5 [five] million").
- A soft hyphen, a format character, a space next to a group comma or inside
  the digits, or a comma then whitespace then a digit after a grouped figure
  ("$33,589, 400") cuts the figure.
- A money figure's denominator - "per <word>", "/<word>", "a <word>", pw, pcm,
  pm, pq, psm, weekly, ... - is part of it and must be written by the value
  ("$850" is not "$850 per week"; per annum is the default period). It is
  checked on the matched occurrence itself.

Percent scale: a percentage is read as written - "12.5%" and "12.5 per cent"
are "12.5", kind percent. There is no percent-to-fraction conversion, so the
caller passes 12.5, not 0.125; converting would let 0.125 match unrelated text.
A value written with its unit keeps it: "8.95%" matches only a percent, and
"$8.95" never matches one, with or without ``kind``.

Dates follow the AUSTRALIAN convention and become ISO strings: "2026-07-01";
"1 July 2026" and "1st Jul. 2026" (full or abbreviated month, any case, day and
month separated by horizontal space only); "01/07/2026" and "01-07-2026" read
DAY/MONTH/YEAR. "July 2026" (month name capitalised) and ISO "2026-07" become
"2026-07" for years 1900-2199. A day number right after an identifier word
(Lot, Unit, Level, Suite, Stage, No., DP, ...) is not a day: "Lot 5 June 2027"
is Lot 5 then 2027-06. An impossible date (31 February, month 13) is not a
date: its digits fall through as ordinary numbers. A US-format date whose day
and month are both <= 12 is misread - an accepted risk, deal documents are
Australian; any other US date fails to parse, so its value check flags.
"""

from __future__ import annotations

import bisect
import functools
import math
import operator
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

_MONTHS: dict[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH_YEAR_RANGE = range(1900, 2200)
# A value whose decimal exponent is beyond this cannot be a document figure, and
# formatting it positionally would build an enormous string.
_MAX_ADJUSTED = 1000

# Regex fragments. Non-ASCII characters are written as regex escapes.
# Horizontal space: a tab or any Unicode space separator (category Zs).
_ZS = "".join(ch for ch in map(chr, range(0x10000)) if unicodedata.category(ch) == "Zs")
_HS = "[\\t" + "".join(re.escape(ch) for ch in _ZS) + "]"
# One line break; CR LF counts as one.
_BREAK = "(?:\\r\\n|[\\n\\r\\v\\f\\x1c-\\x1e\\x85\\u2028\\u2029])"
_BREAK_RE = re.compile(_BREAK)
# Horizontal space around exactly one line break.
_ONE_BREAK = _HS + "*+" + _BREAK + _HS + "*+"
# Any run of spaces and line breaks. Possessive: scanned once, never re-split.
_WS_RUN = "(?:" + _HS + "|" + _BREAK + ")*+"
_HYPHEN = "[-\\N{HYPHEN}\\N{NON-BREAKING HYPHEN}\\N{SOFT HYPHEN}]"
# What may stand between a number and its magnitude or unit: whitespace, at most
# one hyphen (soft hyphen included), whitespace. How many line breaks it holds is
# judged in _number_at.
_TAIL_GAP = "(?:" + _WS_RUN + "(?:" + _HYPHEN + _WS_RUN + ")?)"
# Every dash-like character: Unicode dashes (Pd), the minus sign, the soft hyphen.
_DASH_CHARS = "".join(
    ch for ch in map(chr, range(0x10000)) if unicodedata.category(ch) == "Pd" or ch in "\N{MINUS SIGN}\xad"
)
_DASHLIKE = "[" + "".join(re.escape(ch) for ch in _DASH_CHARS) + "]"
_GROUP_SPACE = "[ \\xa0\\N{THIN SPACE}\\N{NARROW NO-BREAK SPACE}]"
_MINUS = "\\N{MINUS SIGN}"
_RANGE_DASHES = "-\\N{MINUS SIGN}\\N{EN DASH}\\N{EM DASH}"
# Every currency symbol (Unicode category Sc). All of them sit below U+20000.
_CURRENCY_SYMBOLS = "".join(ch for ch in map(chr, range(0x20000)) if unicodedata.category(ch) == "Sc")
_SC = "[" + "".join(re.escape(ch) for ch in _CURRENCY_SYMBOLS) + "]"
# An explicit footnote marker glued after a figure (Task 3c tuning): superscript
# digits, * † ‡ §, [n] and (n) with n of at most 2 digits, ^n. It is clean
# trailing context, consumed into the figure's extent. (² and ³ directly after an
# area or volume unit are that unit, not a footnote.)
_FOOTNOTE = (
    "(?:[\\xb9\\xb2\\xb3\\u2070\\u2074-\\u2079]++|[*\\u2020\\u2021\\xa7]++"
    "|\\[[0-9]{1,2}\\]|\\([0-9]{1,2}\\)|\\^[0-9]{1,2})"
)
_FOOTNOTE_RE = re.compile(_FOOTNOTE)

# Where a figure can begin: a digit, a leading decimal point, a sign, a currency
# symbol, or the first letter of a word (a currency code, a symbol prefix such
# as "HK$", or a month name). Nothing else is tried.
_CANDIDATE = re.compile("[0-9.\\-\\xb1" + _MINUS + "]|" + _SC + "|(?<![A-Za-z])[A-Za-z]")

_ISO_DATE = re.compile(r"(?<![0-9])([0-9]{4})-([0-9]{2})-([0-9]{2})(?![0-9])")
_ISO_MONTH = re.compile(r"(?<![0-9])([0-9]{4})-([0-9]{2})(?![0-9]|-[0-9])")
_NUMERIC_DATE = re.compile(r"(?<![0-9])([0-9]{1,2})([/-])([0-9]{1,2})\2([0-9]{4})(?![0-9])")
_DAY_MONTH_YEAR = re.compile(
    "(?<![0-9])(?P<day>[0-9]{1,2})(?P<ord>st|nd|rd|th)?"
    + _HS
    + "+(?P<mon>[A-Za-z]+)\\.?,?"
    + _HS
    + "+(?P<year>[0-9]{4})(?![0-9])",
    re.IGNORECASE,
)
_MONTH_YEAR = re.compile(
    "(?<![A-Za-z])(?P<mon>[A-Za-z]+)\\.?,?" + _HS + "+(?P<year>[0-9]{4})(?![0-9])",
)
# "Lot 5 June 2027": the 5 belongs to the identifier, not to a date.
_IDENTIFIER_BEFORE = re.compile(
    "(?i)(?<![A-Za-z])(?:lots?|units?|levels?|suites?|stages?|sections?|clauses?|items?|parts?"
    "|blocks?|buildings?|nos?|numbers?|dp|sp|sec|cl|pt)\\.?" + _HS + "+$"
)
# A number right after an identifier word is a label, not a range limb: "Year 1 -
# $5m" states Year 1. The identifier words above, plus year/tranche/schedule/phase.
_INDEPENDENT_LABEL = re.compile(
    "(?i)(?<![A-Za-z])(?:lots?|units?|levels?|suites?|stages?|sections?|clauses?|items?|parts?"
    "|blocks?|buildings?|nos?|numbers?|dp|sp|sec|cl|pt|years?|tranches?|schedules?|phases?)\\.?" + _HS + "+$"
)

# Space-grouped thousands, only behind a currency. The grouping continues across
# ONE line break standing in for a group space, when a group space on either side
# shows it is a grouping: "$12 400<br>000", "$12<br>400 000". Not "$500<br>200",
# which is a table row.
_SPACE_GROUPED = (
    rf"[0-9]{{1,3}}(?:"
    rf"(?:{_GROUP_SPACE}[0-9]{{3}})++(?![0-9])"
    rf"(?:{_ONE_BREAK}[0-9]{{3}}(?:{_GROUP_SPACE}[0-9]{{3}})*+(?![0-9]))?"
    rf"|{_ONE_BREAK}[0-9]{{3}}(?:{_GROUP_SPACE}[0-9]{{3}})++(?![0-9])"
    rf")"
)
# Comma-grouped thousands. A comma at a line end carries the grouping across ONE
# line break: "12,400,<br>000".
_COMMA_GROUPED = (
    rf"(?<![0-9],)[0-9]{{1,3}}(?:"
    rf"(?:,[0-9]{{3}})++(?:,{_ONE_BREAK}[0-9]{{3}}(?:,[0-9]{{3}})*+)?"
    rf"|,{_ONE_BREAK}[0-9]{{3}}(?:,[0-9]{{3}})*+"
    rf")(?![0-9]|,[0-9])"
)

_NUMBER = re.compile(
    rf"""
    (?P<sign>(?<![\w%)\]])[-{_MINUS}\xb1])?
    (?P<cur>
        (?:
            (?<![A-Za-z])(?:AUD|USD|NZD|EUR|GBP)
          | (?:(?<![A-Za-z])[A-Z]{{1,3}})?{_SC}
            | \$(?:AU|A|US|NZ)(?![A-Za-z])
        )
        {_HS}?
    )?
    (?P<sign2>(?(cur)[-{_MINUS}]|(?!)))?
    (?P<num>
        (?<![0-9])
        (?:
            (?(cur){_SPACE_GROUPED}|(?!))
          | {_COMMA_GROUPED}
          | [0-9]+
        )
        (?:\.[0-9]+)?
      | (?<![0-9A-Za-z.])\.[0-9]+
    )
    (?![0-9])
    (?:
        (?P<wgap>{_TAIL_GAP})(?P<word>(?i:thousands?|millions?|billions?|trillions?))(?![A-Za-z0-9])
      | (?P<sgap>{_TAIL_GAP})
        (?P<short>
            (?i:mill|mln|mil|mio|mn|bil|bln|bn|trn|tn|thou|k|b)
            (?!(?![\xb9\xb2\xb3⁰⁴-⁹])\w)
          | (?i:mm|m)(?!(?![\xb9⁰⁴-⁹])\w|[\xb2\xb3])
        )
    )?
    (?P<pct>(?P<pgap>{_TAIL_GAP})(?:%|(?P<pword>(?i:per{_HS}?cent|percent))(?![A-Za-z])))?
    (?P<fn>{_FOOTNOTE})?
    """,
    re.VERBOSE,
)
# Everything in a written number that is not a digit, sign or decimal point.
_SEPARATORS = re.compile("[,\\s" + _GROUP_SPACE[1:])

# A grouping cut by more than one line break - or by a second one - is not
# joined, and neither piece is trusted: both read AMBIGUOUS ("12,400,<br><br>000").
_GROUP_SHAPED_COMMA = re.compile(rf"[0-9]{{1,3}}(?:,(?:{_ONE_BREAK})?[0-9]{{3}})*")
_GROUP_SHAPED_SPACE = re.compile(rf"[0-9]{{1,3}}(?:(?:{_GROUP_SPACE}|{_ONE_BREAK})[0-9]{{3}})*")
# The break is required before the next group is scanned, so a long comma chain
# on one line is never rescanned from each of its figures.
_CUT_COMMA_TAIL = re.compile(
    rf",(?P<ws>{_HS}*+{_BREAK}{_WS_RUN})(?P<next>[0-9]{{3}}(?:,[0-9]{{3}})*+)(?![0-9]|,[0-9])"
)
_CUT_SPACE_TAIL = re.compile(
    rf"(?P<ws>{_HS}*+{_BREAK}{_WS_RUN})(?P<next>[0-9]{{3}}(?:{_GROUP_SPACE}[0-9]{{3}})*+)(?![0-9])"
)
_HAS_GROUP_SPACE = re.compile(_GROUP_SPACE)
_CUT_PIECE_MARK = ","  # "12400," and ",0": the value of a piece of a cut grouping

_WORD_EXPONENT = {"thousand": 3, "million": 6, "billion": 9, "trillion": 12}
_SHORT_EXPONENT = {
    "k": 3,
    "m": 6,
    "mm": 6,
    "mn": 6,
    "mil": 6,
    "mill": 6,
    "mln": 6,
    "b": 9,
    "bn": 9,
    "bil": 9,
    "tn": 12,
    "mio": 6,
    "bln": 9,
    "trn": 12,
    "thou": 3,
}
# Unambiguous without a currency: glued, spaced, after one line break or after
# a hyphen ("12.4bn", "12.4 mn", "$12.4-mln").
_SHORT_WITHOUT_CURRENCY = frozenset({"mn", "mil", "mill", "mln", "mio", "bn", "bil", "bln", "trn", "thou"})
# How an AMBIGUOUS magnitude is kept in a value: a suffix whose written form
# reads back as the same ambiguous figure, and which equals no plain number.
_AMBIGUOUS_SUFFIX = {3: " k", 6: "m", 9: "b", 12: "tn"}

# A second range limb inherits the first limb's currency only across a dash or "to".
_INHERIT_GAP = re.compile(_HS + "*(?:[" + _RANGE_DASHES + "]|(?i:to))" + _HS + "*")
# What may join two range limbs (Task 3c, generalised range rule): whitespace,
# dashes, "~", "/", ",", "&", "+", brackets, and the connector words to, till,
# until, thru, through, and, or, up to. At most 24 characters, at most one line
# break on each side of the connector (counted in Python).
_JOINER = (
    "(?:" + _DASHLIKE + "|[~/,&+()\\[\\]]"
    "|(?<![A-Za-z])(?i:up" + _HS + "+to|to|till|until|thru|through|and|or|plus|minus|vs|versus)(?![A-Za-z]))"
)
_JOIN_GAP = re.compile("(?:" + _HS + "|" + _BREAK + "|" + _JOINER + ")*+")
_JOIN_GAP_MAX = 24
# A magnitude the grammar did not take, across whitespace, dashes and brackets.
# The token is compared after undoing OCR confusables (rn -> m; 1, l, I, |, ! -> l;
# 0 -> o), so "rnillion", "mi11ion", "bi11ion" and a lone "rn" count.
_LOOSE_TOKEN = re.compile(
    "(?P<gap>(?:" + _HS + "|" + _BREAK + "|" + _DASHLIKE + "|[()\\[\\],.;:/*])*+)(?P<tok>[A-Za-z0-9|!]++)"
    "(?![\\w\xb2\xb3\xb9⁰-⁹])"  # "12.4m²" is an area, not "12.4 m"
)
_OCR_TO_LETTER = str.maketrans({"1": "l", "|": "l", "!": "l", "0": "o"})
# Final fix (C): a bracketed interjection between a figure and its magnitude
# ("$5 (five) million", "$37 (in words) million") is looked past.
_LOOSE_INTERJECTION = re.compile(
    "(?:" + _HS + "*+(?:" + _BREAK + _HS + "*+)?"
    "(?:\\([^()\\n]{1,40}\\)|\\[[^\\[\\]\\n]{1,40}\\]|\\{[^{}\\n]{1,40}\\}"
    "|[\N{EM DASH}\N{EN DASH}][^\N{EM DASH}\N{EN DASH}\\n]{1,40}[\N{EM DASH}\N{EN DASH}]))++"
)
# Final fix (C): a space next to a group comma inside a figure ("$24 ,546,000",
# "$33, 089,000"), or inside its digits ("$9 7,722,000"), cuts the figure.
_GROUP_CUT_RIGHT = re.compile(_HS + "+,[0-9]|," + _HS + "+(?:[0-9]{1,3}(?:,[0-9]{3})+|0[0-9]*)(?![0-9])")
_DIGITS_CUT_RIGHT = re.compile(_HS + "[0-9]{1,3}(?:,[0-9]{3})+(?![0-9])")
_GROUP_CUT_LEFT = re.compile("[0-9]," + _HS + "+\\Z")
# Closing action (6): after a grouped figure, a comma then any whitespace then a
# digit ("$33,589, 400") is a cut group, not a sentence comma.
_GROUP_COMMA_SPACE = re.compile(",(?:" + _HS + "|" + _BREAK + ")+[0-9]")
# "plus or minus 10%", "+/- 10%": the spelled-out "\u00b1" - the sign is unknown.
_WS = "(?:" + _HS + "|" + _BREAK + ")"
_TOLERANCE_BEFORE = re.compile(
    "(?i)\\(?"
    + _WS
    + "*(?:[\xb1\u2213]|\\+"
    + _WS
    + "*/"
    + _WS
    + "*\\(?"
    + _WS
    + "*"
    + _DASHLIKE
    + _WS
    + "*\\)?"
    "|"
    + _DASHLIKE
    + _WS
    + "*/"
    + _WS
    + "*\\+|\\+"
    + _WS
    + "*"
    + _DASHLIKE
    + "|\\+"
    + _WS
    + "+or"
    + _WS
    + "+"
    + _DASHLIKE
    + "|plus"
    + _WS
    + "*(?:[-/]|"
    + _WS
    + "+or"
    + _WS
    + "*[-]?|-or-)"
    + _WS
    + "*minus)"
    + _WS
    + "*\\)?"
    + _WS
    + "*:?"
    + _WS
    + "*(?:of"
    + _WS
    + "+)?\\Z"
)
# Closing action (4): a money figure's denominator - "per <word>", "/<word>",
# "a <word>", pw/pcm/pm/pq/psm and weekly/monthly/... - across one line break.
# It is part of the figure: a value matches only when it writes the same one.
_DENOMINATOR = re.compile(
    "(?i)" + _HS + "*(?:" + _BREAK + _HS + "*)?"
    "(?:per(?:"
    + _HS
    + "|"
    + _BREAK
    + ")+(?!cent)(?:(?:calendar|square|sq\\.?)"
    + _HS
    + "+)?(?P<per>[A-Za-z][A-Za-z\xb2]*)"
    "|/" + _HS + "*(?P<slash>[A-Za-z][A-Za-z0-9\xb2]*)"
    "|a" + _HS + "+(?P<a>[A-Za-z]+)"
    "|(?P<abbr>pw|pcm|pm|pq|psm|weekly|fortnightly|monthly|quarterly|nightly|daily|annually|yearly))"
    "(?![A-Za-z])"
)
_DENOMINATOR_CLASS = {
    "week": "week",
    "wk": "week",
    "pw": "week",
    "weekly": "week",
    "month": "month",
    "mth": "month",
    "mo": "month",
    "pcm": "month",
    "pm": "month",
    "monthly": "month",
    "quarter": "quarter",
    "qtr": "quarter",
    "pq": "quarter",
    "quarterly": "quarter",
    "fortnight": "fortnight",
    "fortnightly": "fortnight",
    "night": "night",
    "nightly": "night",
    "day": "day",
    "daily": "day",
    "annum": "year",
    "year": "year",
    "yr": "year",
    "yearly": "year",
    "annually": "year",
    "sqm": "area",
    "sq": "area",
    "m2": "area",
    "m\xb2": "area",
    "psm": "area",
    "metre": "area",
    "meter": "area",
}
_WORD_THEN_DIGITS = re.compile("([A-Za-z]+)[0-9]+")
# A lone currency symbol after a figure, with no number of its own: a column header.
_LONE_CURRENCY_AFTER = re.compile(_HS + "+(?:[A-Z]{1,3})?" + _SC + "(?!" + _HS + "*[0-9.\\-])")
_DOUBLE_SIGN_MARK = "\N{PLUS-MINUS SIGN}"  # "-$-5" reads "±5": equal to no number

# The allowlist (Task 3c). A figure's value is definite only when the character
# touching each side of its extent is clean; otherwise it reads AMBIGUOUS, and a
# GLUE mark takes the place of that side's character in the value ("FY2025" ->
# "⁇2025", "3/45" -> "3⁇", "⁇45"). The mark is itself unclean glue, so the value
# reads back as the same AMBIGUOUS figure, and "3/45" as a value still matches
# "3/45" on a page but never "45/3" or "60:3/45".
_GLUE_MARK = "\N{DOUBLE QUESTION MARK}"
_OPENERS = "([{\"'\N{LEFT DOUBLE QUOTATION MARK}\N{LEFT SINGLE QUOTATION MARK}"
_CLOSERS = ")]}\"'\N{RIGHT DOUBLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}"
_SENTENCE_PUNCT = ".,;:"
# A soft hyphen is invisible: it never makes a range ("$3\xad5,370,000").
_RANGE_DASH_CHARS = (frozenset(_DASH_CHARS) - {"\xad"}) | {"~"}
# Units a figure may carry glued (or after a hyphen) and still be clean: "3.5x",
# "12.5pa", "100sqm", "12.4m²" (area), "12-month", "$500/sqm". Money takes only
# the per-period and per-unit forms.
_PER_UNIT = "/(?:sqm|m2|m\xb2|annum|pa|yr|year|month|mth|week|wk|day|unit|lot|room|bed|ha|hectare)"
# A unit ends at a non-alphanumeric; a footnote marker may follow it ("3.5x¹").
_UNIT_END = "(?![A-Za-z0-9])"
_UNIT_GLUED = re.compile(
    "(?i)(?:-?(?:x|times|pa|p\\.a\\.?|sqm|sq\\.?m|m[\xb2\xb323]|km|ha|yrs?|years?|mths?|months?|days?"
    "|wks?|weeks?|hrs?|storeys?|bps|bp|lots?|units?|beds?|bedrooms?)|" + _PER_UNIT + ")" + _UNIT_END
)
_MONEY_UNIT_GLUED = re.compile("(?i)(?:pa|p\\.a\\.?|" + _PER_UNIT + ")" + _UNIT_END)
# A unit word after a figure, glued or after one space: the figure is not bare.
_UNIT_AFTER_WORD = re.compile(
    "(?i)(?:"
    + _HS
    + "*"
    + _BREAK
    + _HS
    + "*|"
    + _HS
    + "?)(?:%|per"
    + _HS
    + "?cent|percent|x|times|pa|p\\.a\\.?|sqm|sq\\.?m|m[\xb2\xb323]|km|ha"
    "|yrs?|years?|mths?|months?|days?|wks?|weeks?|hrs?|storeys?|bps|bp|lots?|units?|beds?|bedrooms?"
    "|dwellings?|apartments?|tenants?|stages?|tranches?|buildings?|floors?|levels?|sites?|people|persons?"
    "|times?|business" + _HS + "+days?)" + _UNIT_END
)
# Final fix (C): a currency before a number that carries a SPACED count or time
# unit ("in AUD 12 months", "$ 2 lots", "USD 2 business days") is that unit's number.
_COUNT_UNIT_AFTER = re.compile(
    "(?i)(?:"
    + _HS
    + "*"
    + _BREAK
    + _HS
    + "*|"
    + _HS
    + "+)(?:business"
    + _HS
    + "+days?|months?|mths?|years?|yrs?|days?|weeks?|wks?|hours?|hrs?"
    "|lots?|units?|dwellings?|apartments?|tenants?|stages?|tranches?|buildings?|floors?|levels?|sites?|beds?"
    "|bedrooms?|storeys?|people|persons?|sqm|hectares?|ha)"
    "(?![A-Za-z0-9])"
)
# Closing action (1): a single-space currency group before a COUNT unit is two
# columns ("Rent $850 120 units"); before any other unit word it is AMBIGUOUS
# ("Rent $450 000 pa" is neither $450 nor, surely, $450,000).
_TWO_COLUMN_COUNT = re.compile(
    "(?i)" + _HS + "+(?:units?|lots?|dwellings?|apartments?|tenants?|stages?|tranches?|buildings?|floors?"
    "|levels?)(?![A-Za-z0-9])"
)
# OCR digit fragments: single digits one space apart ("$ 1 2 , 4 0 0"). A run of
# three or more, or two with a lone comma or point between them, is a fragment;
# two plain digits ("Year 1 7 percent") are not.
_FRAGMENT_LINKS = (" ", " , ", " . ")
# D13 (closing action: flag, never apply): a page-level scale marker ("($m)",
# "$m", "$'000", "$000s", "(millions)", "in thousands", "$ million" as a header
# token) makes every bare NUMBER on the page AMBIGUOUS for kind money or None - it
# may be in thousands or millions. values.py never applies a scale; column-aware
# scaling belongs to table extraction (Plan C).
_SCALE_MARKER = re.compile(
    "(?i)(?<![A-Za-z0-9$])(?:A|AU|US|NZ)?\\$"
    + _HS
    + "?(?:m|mn|bn|b|k|'000|\N{RIGHT SINGLE QUOTATION MARK}000"
    "|000s?|millions?|thousands?|billions?)(?![A-Za-z0-9])"
    "|\\(" + _HS + "*(?:in" + _HS + "+)?(?:millions?|thousands?|billions?)" + _HS + "*\\)"
    "|(?<![A-Za-z])in" + _HS + "+(?:millions|thousands|billions)(?![A-Za-z])"
)


class GstBasis(str, Enum):
    INCLUSIVE = "inclusive"
    EXCLUSIVE = "exclusive"
    UNSTATED = "unstated"
    # Both bases stated in one span. Never read as either basis: a human resolves it.
    MIXED = "mixed"


class FigureKind(str, Enum):
    MONEY = "money"
    PERCENT = "percent"
    DATE = "date"
    NUMBER = "number"
    # A bare "12.4m": metres or millions. Equals no plain number.
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Figure:
    """One figure: canonical value, kind, and the raw extent of its written form.

    ``currency`` (Task 3c, D10) is the ISO-ish code of a money figure's currency,
    stated or inherited from its range: AUD for $, A$, AU$ and AUD (Australian
    documents default "$" to AUD); USD for US$ and USD; NZD for NZ$ and NZD; EUR
    for EUR and the euro sign; GBP for GBP and the pound sign; JPY for the yen
    sign; any other symbol as written ("HK$"). It takes no part in equality.
    """

    value: str
    kind: FigureKind
    start: int
    end: int
    currency: str | None = field(default=None, compare=False)


def _format_decimal(d: Decimal) -> str:
    """Positional, no leading zeros, no trailing fractional zeros, -0 -> 0."""
    if d.is_zero():
        return "0"
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _canonical(num: str, exponent: int, negative: bool) -> str:
    d = Decimal(_SEPARATORS.sub("", num))
    t = d.as_tuple()
    if exponent and isinstance(t.exponent, int):
        # Rebuild with a shifted exponent: exact, whatever the context precision.
        d = Decimal((t.sign, t.digits, t.exponent + exponent))
    if negative:
        d = d.copy_negate()  # exact; unary minus would round to the context
    return _format_decimal(d)


def _valid_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


@dataclass
class _Info:
    """What the post-passes need to know about one figure."""

    # A magnitude written on the figure itself (expanded or AMBIGUOUS), or None.
    own_magnitude: int | None = None
    percent: bool = False
    own_currency: bool = False
    labelled: bool = False
    # The written number before any magnitude (sign included); None for dates.
    mantissa: Decimal | None = None
    num_start: int = -1
    num_end: int = -1
    single_digit: bool = False
    signed: bool = False
    # A money, number or percent figure a range rule may still turn AMBIGUOUS.
    candidate: bool = False


@dataclass
class _Scan:
    out: list[Figure] = field(default_factory=list)
    info: list[_Info] = field(default_factory=list)
    prev_end: int = -1
    prev_currency: str | None = None
    # Raw extent of the second piece of a grouping cut by line breaks, once seen.
    cut_next: tuple[int, int] = (-1, -1)

    def emit(self, figure: Figure, info: _Info | None = None) -> int:
        self.out.append(figure)
        self.info.append(info if info is not None else _Info())
        self.prev_end = figure.end
        self.prev_currency = figure.currency if figure.kind is FigureKind.MONEY else None
        return figure.end


# The letters written with "$", before it ("AUD$") or after it ("$A").
_DOLLAR_PREFIX = {
    "": "AUD",
    "A": "AUD",
    "AU": "AUD",
    "AUD": "AUD",
    "US": "USD",
    "USD": "USD",
    "NZ": "NZD",
    "NZD": "NZD",
}
# A currency written AFTER a number with none before it: a code, or "dollars"
# with an optional country (Task 4 fix round 3). "dollars" alone is AUD. Never
# when another number follows the code or word.
_TRAILING_CURRENCY = re.compile(
    _HS + "+(?:(?P<code>AUD|USD|NZD|EUR|GBP)(?![A-Za-z])"
    # Task 4 fix round 4, ruling (d): a code followed by a capitalised word is an
    # address or a label ("Level 12 AUD House", "100 AUD Street"), not a suffix.
    "(?!\\s+[A-Z][A-Za-z])"
    "|(?P<country>(?i:australian|aud|us|u\\.s\\.|united"
    + _HS
    + "+states|american|nz|new"
    + _HS
    + "+zealand)"
    + _HS
    # Concern-1 ruling: a singular "dollar" before a word is an adjective ("5,000
    # dollar notes"), not a currency suffix. "dollars" is unchanged.
    + "+)?(?i:dollars|dollar(?![\\s-]+[A-Za-z]))(?![A-Za-z])"
    # Ruling (c): "dollars" followed by a country or code qualifier - (US), US,
    # U.S., NZ, or any [A-Z]{2,3} other than AUD/A - is not an AUD suffix.
    "(?!\\s*\\(?\\s*(?:(?i:u\\.?s\\.?|n\\.?z\\.?)(?![A-Za-z])|(?!(?:AUD|A)(?![A-Za-z]))[A-Z]{2,3}(?![A-Za-z]))))"
    # Not when a number follows: then the code is that number's prefix
    # ("Level 12 AUD 71 956 818" is Level 12, then AUD 71 ...).
    "(?!" + _HS + "*+[-\\u2212\\xb1]?[$0-9.])"
)
# A digit, then only horizontal space, right before a leading currency code.
_NUMBER_BEFORE_CODE = re.compile("[0-9]" + _HS + "+\\Z")
_COUNTRY_CURRENCY = (("australian", "AUD"), ("aud", "AUD"), ("zealand", "NZD"), ("nz", "NZD"))


def _trailing_code(m: re.Match[str]) -> str:
    if m["code"] is not None:
        return m["code"]
    country = (m["country"] or "").lower()
    if not country:
        return "AUD"
    for word, code in _COUNTRY_CURRENCY:
        if word in country:
            return code
    return "USD"  # us, u.s., united states, american


def _currency_code(cur: str) -> str:
    c = "".join(ch for ch in cur if not ch.isspace())
    if c in ("AUD", "USD", "NZD", "EUR", "GBP"):
        return c
    if c.startswith("$") and len(c) > 1:  # "$A", "$AU", "$US", "$NZ"
        return _DOLLAR_PREFIX.get(c[1:], c)
    prefix, symbol = c[:-1], c[-1]
    if symbol == "$":
        return _DOLLAR_PREFIX.get(prefix, c)
    base = {"\N{EURO SIGN}": "EUR", "\N{POUND SIGN}": "GBP", "\N{YEN SIGN}": "JPY"}.get(symbol, symbol)
    return base if not prefix else prefix + symbol


def _after_identifier(text: str, i: int) -> bool:
    return _IDENTIFIER_BEFORE.search(text, max(0, i - 40), i) is not None


def _date_at(text: str, i: int, scan: _Scan) -> int | None:
    """Emit a date (or an impossible date's numbers) starting at i; return the new position."""
    m = _ISO_DATE.match(text, i)
    if m:
        iso = _valid_date(int(m[1]), int(m[2]), int(m[3]))
        if iso:
            return scan.emit(Figure(iso, FigureKind.DATE, m.start(), m.end()))
    m = _NUMERIC_DATE.match(text, i)
    if m:
        iso = _valid_date(int(m[4]), int(m[3]), int(m[1]))
        if iso:
            return scan.emit(Figure(iso, FigureKind.DATE, m.start(), m.end()))
    m = _DAY_MONTH_YEAR.match(text, i)
    if m and m["mon"].lower() in _MONTHS and not _after_identifier(text, i):
        iso = _valid_date(int(m["year"]), _MONTHS[m["mon"].lower()], int(m["day"]))
        if iso:
            return scan.emit(Figure(iso, FigureKind.DATE, m.start(), m.end()))
        # "31 February 2027" is not a date. Its day and year are ordinary
        # numbers, and the month name must not then pair with the year.
        day_end = m.end("ord") if m["ord"] is not None else m.end("day")
        scan.emit(Figure(_canonical(m["day"], 0, False), FigureKind.NUMBER, m.start(), day_end))
        year = Figure(_canonical(m["year"], 0, False), FigureKind.NUMBER, m.start("year"), m.end())
        return scan.emit(year)
    m = _MONTH_YEAR.match(text, i)
    if m and m["mon"][0].isupper() and m["mon"].lower() in _MONTHS and int(m["year"]) in _MONTH_YEAR_RANGE:
        value = f"{m['year']}-{_MONTHS[m['mon'].lower()]:02d}"
        return scan.emit(Figure(value, FigureKind.DATE, m.start(), m.end()))
    m = _ISO_MONTH.match(text, i)
    if m and int(m[1]) in _MONTH_YEAR_RANGE and 1 <= int(m[2]) <= 12:
        return scan.emit(Figure(f"{m[1]}-{m[2]}", FigureKind.DATE, m.start(), m.end()))
    return None


def _number_at(text: str, i: int, scan: _Scan) -> int | None:
    """Emit a number starting at i; return the new position."""
    if i == scan.cut_next[0]:
        # The second piece of a cut grouping: its whole written group run, AMBIGUOUS.
        piece_end = scan.cut_next[1]
        value = _CUT_PIECE_MARK + _canonical(text[i:piece_end], 0, False)
        return scan.emit(Figure(value, FigureKind.AMBIGUOUS, i, piece_end))
    m = _NUMBER.match(text, i)
    if not m:
        return None
    if (
        m["cur"] is not None
        and m["word"] is None
        and m["short"] is None
        and m["pct"] is None
        and len(_HAS_GROUP_SPACE.findall(m["num"])) == 1
        and not _breaks(m["num"])
        and _TWO_COLUMN_COUNT.match(text, m.end("num")) is not None
        and (cut := _HAS_GROUP_SPACE.search(m["num"])) is not None
    ):
        # Final fix: "Rent $850 120 units" is two columns, not $850,120 - the
        # second group belongs to its unit. Read only the first group here.
        split = m.start("num") + cut.start()
        m = _NUMBER.match(text, i, split)
        if not m:
            return None
    num_start = m.start("num")
    if num_start != i and _date_at(text, num_start, _Scan()) is not None:
        return None  # "-2026-07-01", "AUD 30 June 2027": the date wins, from its own start
    # "-$-5" and "±5": the sign is unknown, so the figure is AMBIGUOUS, valued "±5".
    # A written "±5" reads back as the same value.
    double_sign = m["sign"] == _DOUBLE_SIGN_MARK or (m["sign"] is not None and m["sign2"] is not None)
    negative = (m["sign"] is not None or m["sign2"] is not None) and not double_sign  # "-$5", "$-5", "$ -5"
    code = _currency_code(m["cur"]) if m["cur"] is not None else None
    body_start = m.start("cur") if code is not None else num_start
    if (
        code is None
        and m["sign"] is None
        and scan.prev_currency is not None
        and _INHERIT_GAP.fullmatch(text, scan.prev_end, body_start)
    ):
        code = scan.prev_currency  # "$3m-5m": the second limb inherits the first limb's currency
    end = m.end()
    exponent = 0
    ambiguous_suffix = ""
    magnitude: int | None = None
    percent = m["pct"] is not None
    if m["word"] is not None:
        word_exponent = magnitude = _WORD_EXPONENT[m["word"].lower().removesuffix("s")]
        if _breaks(m["wgap"]) > 1:
            ambiguous_suffix = _AMBIGUOUS_SUFFIX[word_exponent]  # a paragraph away
        else:
            exponent = word_exponent
    elif m["short"] is not None:
        short = m["short"].lower()
        gap = m["sgap"]
        short_exponent = magnitude = _SHORT_EXPONENT[short]
        if short in _SHORT_WITHOUT_CURRENCY:
            if _breaks(gap) > 1:
                ambiguous_suffix = _AMBIGUOUS_SUFFIX[short_exponent]
            else:
                exponent = short_exponent
        elif gap and (_breaks(gap) or not gap.isspace()):
            # After a line break or a hyphen a lone k/m/b/mm may be anything.
            ambiguous_suffix = " k" if short == "k" else short
        elif code is not None or (short == "k" and not gap):
            exponent = short_exponent
        else:
            # Metres or millions? Keep the suffix so the value equals no plain number.
            ambiguous_suffix = " k" if short == "k" else short
    if m["fn"] is not None and m["num"] == "10" and m["fn"][0] not in "*†‡\xa7[(":
        end = m.start("fn")  # "10⁶", "10^6": a power of ten, not a footnote - left glued
    if percent and m["pword"] is not None and _breaks(m["pgap"]) > 1:
        percent = False  # "per cent" a paragraph away is not this figure's unit
        end = m.start("pct")
    value = _canonical(m["num"], exponent, negative) + ambiguous_suffix
    if double_sign:
        value = _DOUBLE_SIGN_MARK + value
    if ambiguous_suffix or double_sign:
        kind = FigureKind.AMBIGUOUS
    elif percent:
        kind = FigureKind.PERCENT
    elif code is not None:
        kind = FigureKind.MONEY
    else:
        kind = FigureKind.NUMBER
    trailing = False
    if kind is FigureKind.NUMBER and (tc := _TRAILING_CURRENCY.match(text, end)) is not None:
        # "12,400,000 AUD", "5 million dollars": money, the currency written after it.
        kind, code, end, trailing = FigureKind.MONEY, _trailing_code(tc), tc.end(), True
    if (
        kind is FigureKind.MONEY
        and len(_HAS_GROUP_SPACE.findall(m["num"])) == 1
        and not _breaks(m["num"])
        and _UNIT_AFTER_WORD.match(text, m.end("num")) is not None
    ):
        value, kind, code = value + _GLUE_MARK, FigureKind.AMBIGUOUS, None
    elif kind is FigureKind.MONEY and magnitude is None and _COUNT_UNIT_AFTER.match(text, end) is not None:
        # Final fix (C): "in AUD 12 months", "$ 2 lots": the number carries a unit,
        # so it is that unit's number, not money.
        kind, code = FigureKind.NUMBER, None
    elif kind is FigureKind.MONEY and m["cur"] is not None:
        # A currency written apart from its number is only trusted for a clean
        # amount: not a bare year ("in AUD 2025 and prior"). And no money figure
        # followed by a lone currency of its own is trusted: that is a column
        # header ("2026 $ 2025 $", "Balance $12 $").
        spaced = m["cur"][-1].isspace()
        year_like = spaced and m["num"].isdigit() and len(m["num"]) == 4 and 1900 <= int(m["num"]) <= 2199
        header = _LONE_CURRENCY_AFTER.match(text, end) is not None
        # Concern-1 ruling: a code sitting between a number and a space-grouped
        # number ("ABN 12 AUD 71 956 818") may close the first or open the second;
        # neither is proven money.
        between = (
            _HAS_GROUP_SPACE.search(m["num"]) is not None
            and _NUMBER_BEFORE_CODE.search(text, max(0, m.start("cur") - 40), m.start("cur")) is not None
        )
        if (magnitude is None and (year_like or header)) or between:
            value, kind, code = _GLUE_MARK + value, FigureKind.AMBIGUOUS, None
    if end == m.end("num") and (next_piece := _cut_grouping(text, m)) is not None:
        value, kind = value + _CUT_PIECE_MARK, FigureKind.AMBIGUOUS
        scan.cut_next = next_piece
    digits = _SEPARATORS.sub("", m["num"])
    info = _Info(
        own_magnitude=magnitude,
        percent=percent,
        own_currency=m["cur"] is not None or trailing,
        # "Year 1 - $5m": a figure with no currency right after an identifier word
        # is that identifier's number.
        labelled=m["cur"] is None
        and _INDEPENDENT_LABEL.search(text, max(0, m.start() - 40), m.start()) is not None,
        mantissa=Decimal(digits).copy_negate() if negative else Decimal(digits),
        num_start=num_start,
        num_end=m.end("num"),
        single_digit=len(digits) == 1,
        signed=m["sign"] is not None or m["sign2"] is not None,
        candidate=kind in (FigureKind.MONEY, FigureKind.NUMBER, FigureKind.PERCENT) and magnitude is None,
    )
    keep = kind is FigureKind.MONEY or double_sign
    return scan.emit(Figure(value, kind, m.start(), end, code if keep else None), info)


def _breaks(gap: str | None) -> int:
    return len(_BREAK_RE.findall(gap)) if gap else 0


def _cut_grouping(text: str, m: re.Match[str]) -> tuple[int, int] | None:
    """The second piece's extent, when this number's grouping was cut by line breaks.

    Only a grouping the scanner did not join counts: more than one line break,
    or a second break in one number ("12,400,<br><br>000", "12,<br>400,<br>000",
    "$12 400<br><br>000"). A space grouping needs a currency and a group space on
    one side, as for joining.
    """
    num = m["num"]
    if "." in num:
        return None
    if _GROUP_SHAPED_COMMA.fullmatch(num):
        tail = _CUT_COMMA_TAIL.match(text, m.end("num"))
        if tail is not None and _breaks(tail["ws"]):
            return tail.span("next")
    if m["cur"] is not None and _GROUP_SHAPED_SPACE.fullmatch(num):
        tail = _CUT_SPACE_TAIL.match(text, m.end("num"))
        if (
            tail is not None
            and _breaks(tail["ws"])
            and (_HAS_GROUP_SPACE.search(num) or _HAS_GROUP_SPACE.search(tail["next"]) or _breaks(num))
        ):
            return tail.span("next")
    return None


def figures(text: str) -> list[Figure]:
    """Every figure in ``text``, in document order (see the module docstring)."""
    scan = _Scan()
    pos = 0
    while (c := _CANDIDATE.search(text, pos)) is not None:
        i = c.start()
        new_pos = _date_at(text, i, scan)
        if new_pos is None:
            new_pos = _number_at(text, i, scan)
        pos = new_pos if new_pos is not None else i + 1
    _ranges(text, scan)
    _allowlist(text, scan)
    return scan.out


def _loose_magnitude(text: str, pos: int) -> int | None:
    """A magnitude (or its OCR look-alike) the grammar did not take, after ``pos``."""
    exp = _loose_token_exponent(text, pos)
    if exp is None and (aside := _LOOSE_INTERJECTION.match(text, pos)) is not None:
        exp = _loose_token_exponent(text, aside.end())
    return exp


def _loose_token_exponent(text: str, pos: int) -> int | None:
    m = _LOOSE_TOKEN.match(text, pos)
    if m is None or _breaks(m["gap"]) > 1 or m["tok"][0].isdigit():
        return None
    raw = m["tok"]
    tok = raw.lower().replace("rn", "m").translate(_OCR_TO_LETTER)
    exp = _SHORT_EXPONENT.get(tok) or _WORD_EXPONENT.get(tok.removesuffix("s"))
    if exp is None and (glued := _WORD_THEN_DIGITS.fullmatch(raw)) is not None:
        # "$2.5 million1": a magnitude word with a footnote digit glued to it.
        word = glued[1].lower()
        exp = _WORD_EXPONENT.get(word.removesuffix("s"))
        if exp is None and word in _SHORT_WITHOUT_CURRENCY:
            exp = _SHORT_EXPONENT[word]
    return exp


def _join_ok(text: str, a: int, b: int) -> bool:
    """Do ``text[a:b]`` join two range limbs? (<= 24 chars, <= 1 break per side).

    A connector dangling at a line end ("$3 to<br><br>5 million") joins across any
    number of blank lines (Task 3c tuning, fail-safe); that trailing whitespace
    does not count toward the 24 characters.
    """
    lead_end = a
    while lead_end < b and text[lead_end].isspace():
        lead_end += 1
    if lead_end == b:  # whitespace only
        return b - a <= _JOIN_GAP_MAX and _breaks(text[a:b]) <= 1
    tail_start = b
    while tail_start > lead_end and text[tail_start - 1].isspace():
        tail_start -= 1
    dangling = _breaks(text[tail_start:b]) > 0
    if (tail_start if dangling else b) - a > _JOIN_GAP_MAX:
        return False
    if _JOIN_GAP.fullmatch(text, a, b) is None:
        return False
    return _breaks(text[a:lead_end]) <= 1 and _breaks(text[lead_end:tail_start]) <= 1


def _independent(first: _Info, second: _Info) -> bool:
    """Is ``first`` independent of the magnitude or unit ``second`` carries?

    Own currency AND own magnitude; or at least 1000x the second limb read at the
    second's magnitude ("$500,000 and $1.2m", but not "$1,200-$1,500m"); or an
    identifier label with the second limb carrying its own currency ("Year 1 - $5m",
    but not "Stage 3 to 5 million").
    """
    if first.own_currency and first.own_magnitude is not None:
        return True
    if first.percent:
        return True  # a written percent never borrows a magnitude ("6% - $5m")
    if first.mantissa is not None and second.mantissa is not None and second.mantissa != 0:
        if abs(first.mantissa) >= 1000 * abs(second.mantissa):
            return True
    return first.labelled and second.own_currency


def _ranges(text: str, scan: _Scan) -> None:
    """The generalised range rule (Task 3c), after the scanner's specific rules.

    Two figures joined by a short gap of joiners (``_join_ok``):

    - the second carries a magnitude the first lacks: the first is AMBIGUOUS
      ("$1.2-1.5m", "$3 and 5 million", "$1.2 ~ 1.5m"), unless independent. A
      magnitude it borrows counts for the figure before it ("$3, 4 or 5 million").
    - the second is a percent and the first a bare number: the first is a
      percent ("3-4.5%"); a first with a currency but no percent is AMBIGUOUS.
    - the first carries its own magnitude and the second has neither magnitude
      nor unit: the second is AMBIGUOUS ("$3m-5"), unless independent.

    A figure with no joiner to its neighbour can still reach a magnitude the
    grammar did not take ("$12.4 -- million", "$12.4 rnillion"): AMBIGUOUS.
    """
    out, info = scan.out, scan.info
    n = len(out)
    reach: list[int | None] = [inf.own_magnitude for inf in info]
    for i in range(n - 1, -1, -1):
        f, inf = out[i], info[i]
        if not inf.candidate:
            continue
        exp: int | None = None
        joined = i + 1 < n and _join_ok(text, f.end, out[i + 1].start)
        if joined and reach[i + 1] is not None and not _independent(inf, info[i + 1]):
            exp = reach[i + 1]
        if exp is None:
            exp = _loose_magnitude(text, f.end)
        if exp is not None:
            out[i] = replace(f, value=f.value + _AMBIGUOUS_SUFFIX[exp], kind=FigureKind.AMBIGUOUS)
            reach[i] = exp
            inf.candidate = False
            continue
        # D6 needs a real connector: "3-4.5%", "3 to 4.5 per cent". Two table columns
        # ("12.4  4.25%") are not a range.
        if (
            joined
            and out[i + 1].kind is FigureKind.PERCENT
            and not inf.percent
            and not text[f.end : out[i + 1].start].isspace()
        ):
            if f.kind is FigureKind.NUMBER and not inf.labelled:
                out[i] = replace(f, kind=FigureKind.PERCENT)  # "3-4.5%" is 3 per cent
                inf.percent = True
            elif f.kind is FigureKind.MONEY and not _independent(inf, info[i + 1]):
                out[i] = replace(f, value=f.value + _GLUE_MARK, kind=FigureKind.AMBIGUOUS, currency=None)
            inf.candidate = False
    for j in range(1, n):
        g, inf = out[j], info[j]
        first = info[j - 1]
        if (
            inf.candidate
            and not inf.percent
            and first.own_magnitude is not None
            and _UNIT_AFTER_WORD.match(text, g.end) is None
            and _join_ok(text, out[j - 1].end, g.start)
            and not _independent(inf, first)
        ):
            out[j] = replace(
                g, value=g.value + _AMBIGUOUS_SUFFIX[first.own_magnitude], kind=FigureKind.AMBIGUOUS
            )
            inf.candidate = False


def _clean_left(text: str, start: int, prev: Figure | None) -> bool:
    k = start
    while k > 0 and text[k - 1] in _OPENERS:
        k -= 1
    if k == 0 or text[k - 1].isspace():
        return True
    if text[k - 1] in _RANGE_DASH_CHARS and k == start and prev is not None:
        # A dash between two figures is the range shape the specific rules read.
        q = k - 1
        lead = q
        while q > 0 and text[q - 1].isspace():
            q -= 1
        while q > 0 and text[q - 1] in _CLOSERS:
            q -= 1
        return prev.end == q and _breaks(text[q:lead]) <= 1
    return False


def _clean_right(text: str, end: int, nxt: Figure | None, money: bool) -> bool:
    unit = (_MONEY_UNIT_GLUED if money else _UNIT_GLUED).match(text, end)
    j = unit.end() if unit is not None else end
    if unit is not None and (note := _FOOTNOTE_RE.match(text, j)) is not None:
        j = note.end()  # "ICR 3.5x¹": a footnote after a glued unit
    k = j
    while k < len(text) and text[k] in _CLOSERS:
        k += 1
    if k == len(text) or text[k].isspace():
        return True
    c = text[k]
    if c in _SENTENCE_PUNCT:
        return k + 1 == len(text) or text[k + 1].isspace()
    if c in _RANGE_DASH_CHARS and j == end and nxt is not None:
        q = k + 1
        while q < len(text) and text[q].isspace():
            q += 1
        while q < len(text) and text[q] in _OPENERS:
            q += 1
        return nxt.start in (k + 1, q) and _breaks(text[k + 1 : q]) <= 1
    return False


def _fragments(text: str, scan: _Scan) -> tuple[set[int], set[int]]:
    """Figures in an OCR digit-fragment run: (those unclean on the left, on the right)."""
    out, info = scan.out, scan.info
    left: set[int] = set()
    right: set[int] = set()
    i = 0
    while i < len(out):
        j = i
        punct = False
        while (
            j + 1 < len(out)
            and info[j].single_digit
            and info[j + 1].single_digit
            and info[j + 1].num_start == out[j + 1].start
            and (link := text[info[j].num_end : out[j + 1].start]) in _FRAGMENT_LINKS
        ):
            punct = punct or link != " "
            j += 1
        if j > i and (j - i + 1 >= 3 or punct):
            right.update(range(i, j))
            left.update(range(i + 1, j + 1))
        i = j + 1
    return left, right


def _allowlist(text: str, scan: _Scan) -> None:
    """Task 3c: a figure is definite only when its immediate context is clean.

    Clean on each side: whitespace, a line boundary, or (right) sentence
    punctuation followed by whitespace or the end; brackets and quotes around a
    figure are looked through; a known unit glued on the right ("3.5x", "12.5pa",
    "12.4m²", "12-month", "$500/sqm"); a dash between two figures (a range). Any
    other glued character - superscripts, a digit after a suffix, * † ‡ ^, a
    footnote bracket, O/o/l/I confusables, &, unknown letters - makes the figure
    AMBIGUOUS, with a GLUE mark for each unclean side in its value. So do a sign
    glued to another dash ("$--5") and OCR digit fragments ("$ 1 2 , 4 0 0").
    """
    out, info = scan.out, scan.info
    n = len(out)
    fragment_left, fragment_right = _fragments(text, scan)
    cut_next: set[int] = set()
    for i in range(n):
        f, inf = out[i], info[i]
        if f.value.startswith(_CUT_PIECE_MARK) or f.value.endswith(_CUT_PIECE_MARK):
            continue  # a cut grouping's piece is already AMBIGUOUS
        prev = out[i - 1] if i else None
        nxt = out[i + 1] if i + 1 < n else None
        left = _clean_left(text, f.start, prev) and i not in fragment_left
        right = _clean_right(text, f.end, nxt, f.kind is FigureKind.MONEY) and i not in fragment_right
        if inf.mantissa is not None:
            written = text[inf.num_start : inf.num_end]
            if _GROUP_CUT_RIGHT.match(text, inf.num_end) is not None:
                right = False
            if "," in written and _GROUP_COMMA_SPACE.match(text, inf.num_end) is not None:
                right = False
            if ("," in written or written.startswith("0")) and _GROUP_CUT_LEFT.search(
                text, max(0, inf.num_start - 8), inf.num_start
            ):
                left = False
            if inf.own_currency and "," not in written and len(written) <= 3:
                if _DIGITS_CUT_RIGHT.match(text, inf.num_end) is not None:
                    right = False
                    cut_next.add(i + 1)
            if i in cut_next:
                left = False
        if not f.value.startswith(_DOUBLE_SIGN_MARK) and (
            _TOLERANCE_BEFORE.search(text, max(0, f.start - 40), f.start) is not None
        ):
            # "+/- US$5m", "plus or minus<br>5%", "(+/-) 5%": a tolerance. AMBIGUOUS, but it
            # keeps its currency, so it never matches another currency's tolerance.
            f = replace(f, value=_DOUBLE_SIGN_MARK + f.value.lstrip("-"), kind=FigureKind.AMBIGUOUS)
            out[i] = f
        if inf.signed and f.start > 0 and text[f.start - 1] in _RANGE_DASH_CHARS:
            left = False  # D11: "$--5", "5--3"
        # One mark per side: a value the range rule already marked keeps a single one,
        # so it reads back as itself.
        left = left or f.value.startswith(_GLUE_MARK)
        right = right or f.value.endswith(_GLUE_MARK)
        if left and right:
            continue
        value = ("" if left else _GLUE_MARK) + f.value + ("" if right else _GLUE_MARK)
        out[i] = replace(f, value=value, kind=FigureKind.AMBIGUOUS, currency=None)


def figure_spans(text: str) -> list[tuple[int, int]]:
    """The raw ``[start, end)`` extent of every figure in ``text``."""
    return [(f.start, f.end) for f in figures(text)]


def numeric_tokens(text: str) -> list[str]:
    """The canonical value of every figure in ``text``, in document order."""
    return [f.value for f in figures(text)]


# --- GST basis ----------------------------------------------------------------

_DASHES = {ord(ch): "-" for ch in "\N{HYPHEN}\N{NON-BREAKING HYPHEN}\N{FIGURE DASH}\N{EN DASH}\N{EM DASH}"}
_DASHES[ord("\N{MINUS SIGN}")] = "-"
_SINGLE_QUOTES = {
    ord(ch): "'"
    for ch in "\N{LEFT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}"
    "\N{SINGLE LOW-9 QUOTATION MARK}\N{SINGLE HIGH-REVERSED-9 QUOTATION MARK}"
    "\N{MODIFIER LETTER APOSTROPHE}"
}
_QUOTES = _SINGLE_QUOTES | {
    ord(ch): '"'
    for ch in "\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}\N{DOUBLE LOW-9 QUOTATION MARK}"
}
_GST_DOTTED = re.compile(r"\bg\s?\.\s?s\s?\.\s?t\b\.?")
_GST_SPELLED = re.compile(r"\bgoods\s+(?:and|&)\s+services\s+tax\b")
_INCL = r"(?:incl(?:uding|usive|udes|uded|ude)?|inc)"
_EXCL = r"(?:excl(?:uding|usive|udes|uded|ude)?|exc|ex)"
_NEG = r"(?:\bnot\s+|n't\s+)"
# Every whitespace run below is separated from the next by a required token, so
# no two unbounded runs are adjacent: matching stays linear on long blank runs.
#
# "incl GST", "inc. GST", "ex-GST", "inclusive (of GST)", "does not include GST".
# Not when "GST" opens a registration detail: "Example Inc. GST No. 12".
_GST_FORWARD = re.compile(
    rf"(?P<neg>{_NEG})?\b(?:(?P<inc>{_INCL})|(?P<exc>{_EXCL}))\.?\s*(?:\(\s*)?(?:of\s+)?(?:-\s*)?"
    r"(?P<gst>gst)\b(?!\s*(?:regist|number\b|no\s*\.|no\s*[0-9#]))"
)
# "GST inclusive", "GST-ex", "GST: excl", "GST (inclusive)", "GST is not included",
# "not GST inclusive".
_GST_REVERSED = re.compile(
    rf"(?P<neg0>{_NEG})?\b(?P<gst>gst)\s*(?:[-:(]\s*)?(?:(?:is|are)\s+)?(?P<neg>not\s+)?"
    rf"(?:(?P<inc>{_INCL})|(?P<exc>{_EXCL}))\b"
)
# The figure excludes GST: "plus GST", "+ GST", "net of GST", "GST will be added",
# "GST payable in addition".
_GST_ON_TOP = re.compile(
    r"(?:\bplus|\+|\bnet\s+of)\s*gst\b"
    r"|\bgst\s+(?:(?:will|is|to)\s+be\s+|is\s+)?(?:added|payable\s+in\s+addition)\b"
)
# A form answer after the phrase: "GST inclusive: No", "inclusive of GST? Yes".
_ANSWER = re.compile(r"\s*[:?]\s*(?P<ans>yes|no)\b")


def _gst_text(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).casefold()
    t = t.translate(_DASHES).translate(_QUOTES)
    t = _GST_DOTTED.sub("gst", t)
    return _GST_SPELLED.sub("gst", t)


def _basis_of(m: re.Match[str], t: str, negations: tuple[str, ...]) -> GstBasis:
    flips = sum(1 for name in negations if m[name] is not None)
    answer = _ANSWER.match(t, m.end())
    if answer is not None and answer["ans"] == "no":
        flips += 1
    inclusive = (m["inc"] is not None) != (flips % 2 == 1)
    return GstBasis.INCLUSIVE if inclusive else GstBasis.EXCLUSIVE


def gst_basis(text: str) -> GstBasis:
    """The GST basis ``text`` states: INCLUSIVE, EXCLUSIVE, MIXED (both), or UNSTATED."""
    t = _gst_text(text)
    found: set[GstBasis] = set()
    used_gst: set[int] = set()
    for m in _GST_FORWARD.finditer(t):
        found.add(_basis_of(m, t, ("neg",)))
        used_gst.add(m.start("gst"))
    if _GST_ON_TOP.search(t):
        found.add(GstBasis.EXCLUSIVE)
    for m in _GST_REVERSED.finditer(t):
        # "incl GST ex stamp duty": that GST already belongs to "incl".
        if m.start("gst") not in used_gst:
            found.add(_basis_of(m, t, ("neg0", "neg")))
    if len(found) > 1:
        return GstBasis.MIXED
    return found.pop() if found else GstBasis.UNSTATED


# --- value in span ------------------------------------------------------------

_EXPONENT_LITERAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[eE][+-]?[0-9]+")
# Words that qualify a figure without identifying anything: they never force a
# whole-word match of a mixed value ("8.95% p.a.", "3.5x", "$4.12m excl GST").
_QUALIFIER_WORDS = re.compile(
    r"(?i)(?<![a-z])(?:p\.?\s?a\.?|per\s+annum|x|times|incl(?:uding|usive)?\.?|inc\.?"
    r"|excl(?:uding|usive)?\.?|exc\.?|ex\.?|of|gst|plus|net)(?![a-z])"
)


# Comparator symbols (Task 4 C3): identifying, never qualifiers.
COMPARATOR_SYMBOLS: frozenset[str] = frozenset("<>=~±≤≥≠⩽⩾")


def _bounded(d: Decimal) -> str | None:
    if not d.is_finite():
        return None
    if d.is_zero():
        return "0"
    if abs(d.adjusted()) > _MAX_ADJUSTED:
        return None
    return _format_decimal(d)


def _value_text(value: object) -> str | None:
    """The value as text to read figures from; None when it can never be in a span."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)  # a bool is not a number: never 1/0
    if isinstance(value, float):
        # repr is the shortest round-trip form; Decimal(float) would expose binary noise.
        return _bounded(Decimal(repr(value))) if math.isfinite(value) else None
    if isinstance(value, int):
        return _bounded(Decimal(value))
    if isinstance(value, Decimal):
        return _bounded(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    if _EXPONENT_LITERAL.fullmatch(text.strip()):
        return _bounded(Decimal(text.strip()))  # "1.24E+7" is 12400000, not 1.24 and 7
    return text


def _words(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).casefold()
    t = t.translate(_DASHES).translate(_QUOTES)
    return " ".join(t.split())


_MARKS = frozenset({"Mn", "Mc", "Me"})
_WORD_CHAR = re.compile(r"\w")
# How far past an occurrence edge to look through punctuation for a glued figure.
_GLUE_REACH = 16


def _is_word_char(ch: str) -> bool:
    # A combining mark continues the word it sits on ("Jose" + U+0301).
    return _WORD_CHAR.match(ch) is not None or unicodedata.category(ch) in ("Mn", "Mc")


def _normalised_with_map(text: str) -> tuple[str, list[int], list[int]]:
    """``_words`` with, per output character, the raw [start, end) it came from.

    Normalised per base character and its combining marks, so each output
    character maps to one raw segment.
    """
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    i, n = 0, len(text)
    while i < n:
        j = i + 1
        while j < n and unicodedata.category(text[j]) in _MARKS:
            j += 1
        piece = unicodedata.normalize("NFKC", text[i:j]).casefold().translate(_DASHES).translate(_QUOTES)
        for ch in piece:
            if ch.isspace():
                if not out or out[-1] == " ":
                    continue
                ch = " "
            out.append(ch)
            starts.append(i)
            ends.append(j)
        i = j
    if out and out[-1] == " ":
        del out[-1], starts[-1], ends[-1]
    return "".join(out), starts, ends


def _figure_at(figs: tuple[Figure, ...], starts: tuple[int, ...], k: int) -> Figure | None:
    """The page figure whose extent holds raw offset ``k``."""
    idx = bisect.bisect_right(starts, k) - 1
    if idx >= 0 and figs[idx].start <= k < figs[idx].end:
        return figs[idx]
    return None


def _glued_figure(
    page_text: str, figs: tuple[Figure, ...], starts: tuple[int, ...], k: int, step: int
) -> bool:
    """Is a figure reachable from raw offset ``k`` (moving by ``step``) through
    punctuation only - no space, no word character? ("20" in "60:20:40")."""
    for _ in range(_GLUE_REACH):
        if not 0 <= k < len(page_text):
            return False
        if _figure_at(figs, starts, k) is not None:
            return True
        ch = page_text[k]
        if ch.isspace() or _is_word_char(ch):
            return False
        k += step
    return False


def _occurrence_ok(
    wanted: list[Figure],
    wanted_kind: FigureKind | None,
    value_text: str,
    page_text: str,
    start: int,
    end: int,
    ra: int,
    rb: int,
) -> bool:
    """An occurrence ``page_text[ra:rb]`` of the value counts only if its edges cut
    no page word or figure, no figure is glued to it through punctuation, and
    the page figures inside it equal the value's figures, in order and kind."""
    if (
        ra == start
        and 0 < ra < len(page_text)
        and _is_word_char(page_text[ra - 1])
        and _is_word_char(page_text[ra])
    ):
        return False
    if (
        rb == end
        and 0 < rb < len(page_text)
        and _is_word_char(page_text[rb - 1])
        and _is_word_char(page_text[rb])
    ):
        return False
    figs, starts = _page_figures(page_text)
    for edge in (ra, rb):
        f = _figure_at(figs, starts, edge)
        if f is not None and f.start < edge:
            return False  # a figure crosses the edge
    if _glued_figure(page_text, figs, starts, ra - 1, -1) or _glued_figure(page_text, figs, starts, rb, 1):
        return False
    inside = [f for f in figs[bisect.bisect_left(starts, ra) : bisect.bisect_left(starts, rb)] if f.end <= rb]
    if any(_GLUE_MARK in w.value for w in wanted) and _UNIT_AFTER_WORD.match(page_text, rb) is not None:
        return False  # "<12.4" is not "<12.4 years": the unit written next to it counts
    scaled = _page_scaled(page_text)
    return len(inside) == len(wanted) and all(
        _same_figure(w, p, wanted_kind, scaled, written=True) and _denominator_ok(w, value_text, p, page_text)
        for w, p in zip(wanted, inside, strict=True)
    )


def _slice_word_match(
    value_text: str,
    page_text: str,
    start: int,
    end: int,
    wanted: list[Figure] | None = None,
    wanted_kind: FigureKind | None = None,
) -> bool:
    """The value occurs in ``page_text[start:end]`` as whole words, in order, and
    the occurrence agrees with the page's figures (see ``_occurrence_ok``)."""
    slice_text = page_text[start:end]
    whole = _words(value_text)
    if not whole or whole not in _words(slice_text):
        return False  # cheap prefilter: the mapped normalisation below is per character
    needle = _normalised_with_map(value_text)[0]
    norm, map_start, map_end = _normalised_with_map(slice_text)
    pattern = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)")
    pos = 0
    while (m := pattern.search(norm, pos)) is not None:
        ra, rb = start + map_start[m.start()], start + map_end[m.end() - 1]
        if _occurrence_ok(wanted or [], wanted_kind, value_text, page_text, start, end, ra, rb):
            return True
        pos = m.start() + 1
    return False


def _has_identifying_words(text: str, figs: list[Figure]) -> bool:
    pieces: list[str] = []
    last = 0
    for f in figs:
        pieces.append(text[last : f.start])
        last = f.end
    pieces.append(text[last:])
    rest = unicodedata.normalize("NFKC", _QUALIFIER_WORDS.sub(" ", " ".join(pieces)))
    # A comparator ("< 65%", "≤ 1.5x") states a direction: it identifies the
    # value like a word does, so the value must occur as written (Task 4 C3).
    # So does every other math symbol, after NFKC folding (fix round 3).
    return any(ch.isalpha() or ch in COMPARATOR_SYMBOLS or unicodedata.category(ch) == "Sm" for ch in rest)


def _needs_word_match(text: str, figs: list[Figure]) -> bool:
    # Two or more figures: their order and what stands between them ("60:40",
    # "3/45", "12 of 15", "8%/10%") identify the value, so "x" and "of" there
    # are not qualifiers - the whole value must occur as written. A glue mark
    # stands for a character the figure's value does not carry ("<65%" vs ">65%",
    # "<US$5m" vs "<A$5m"), so a glued figure must also occur as written.
    return len(figs) > 1 or any(_GLUE_MARK in f.value for f in figs) or _has_identifying_words(text, figs)


# A unit a value states right after a figure. The span must state a unit of the
# same class right after the matching figure: "5X" is not "5Y", and "8.95% p.a."
# is not "8.95% per month". "3.5x" still matches "3.5 times".
_UNIT_AFTER = (
    re.compile(rf"{_HS}*+(?i:x|times)(?![A-Za-z])"),
    re.compile(rf"{_HS}*+(?i:p\.?{_HS}?a(?![A-Za-z])\.?|per{_HS}+annum(?![A-Za-z]))"),
)


def _unit_after(text: str, pos: int, endpos: int) -> re.Pattern[str] | None:
    for unit in _UNIT_AFTER:
        if unit.match(text, pos, endpos):
            return unit
    return None


def _unit_follows(unit: re.Pattern[str] | None, page_text: str, pos: int, end: int) -> bool:
    # Matched against the page, so a unit word cut by the span edge ("5Xa") fails.
    if unit is None:
        return True
    m = unit.match(page_text, pos)
    return m is not None and m.end() <= end


def _denominator(text: str, pos: int) -> str | None:
    """The per-unit or per-period denominator written after a money figure, or None.

    Closing action (4): generic - "per <word>", "/<word>", "a <word>", and pw,
    pcm, pm, pq, psm (also weekly, monthly, ...), across one line break. Per
    annum is the default period of an amount, so it counts as no denominator.
    """
    m = _DENOMINATOR.match(text, pos)
    if m is None:
        return None
    word = (m["per"] or m["slash"] or m["a"] or m["abbr"]).lower()
    cls = _DENOMINATOR_CLASS.get(word, _DENOMINATOR_CLASS.get(word.removesuffix("s"), word.removesuffix("s")))
    return None if cls == "year" else cls


def _denominator_ok(w: Figure, value_text: str, p: Figure, page_text: str) -> bool:
    # "$850" is not "$850 per week": a money figure's denominator is part of it.
    if p.kind is not FigureKind.MONEY:
        return True
    return _denominator(page_text, p.end) == _denominator(value_text, w.end)


def _same_figure(
    w: Figure, p: Figure, wanted_kind: FigureKind | None, scaled: bool = False, written: bool = False
) -> bool:
    # Round-5 invariant: an AMBIGUOUS figure equals nothing, not even the same
    # canonical string ("20.1m" is not "20.1m": metres or millions). Only a value
    # also matched as written words, or the same written tolerance, may meet one.
    if FigureKind.AMBIGUOUS in (p.kind, w.kind) and not written and _DOUBLE_SIGN_MARK not in w.value:
        return False
    if p.value != w.value:
        return False
    if wanted_kind is not None and p.kind is not wanted_kind:
        return False
    # D13: on a page with a scale marker a bare NUMBER may be in thousands or
    # millions, so it is no money figure and no untyped one.
    if scaled and p.kind is FigureKind.NUMBER and wanted_kind in (None, FigureKind.MONEY):
        return False
    # A value that states a currency matches only a figure stating the same one
    # (D10) - also an AMBIGUOUS figure that keeps its currency ("+/- US$5m").
    if w.currency is not None and p.currency != w.currency:
        return False
    # The value's own written unit also applies: a written percent matches only
    # a percent; written money never matches a percent.
    if w.kind is FigureKind.PERCENT:
        return p.kind is FigureKind.PERCENT
    if w.kind is FigureKind.MONEY:
        return p.kind is FigureKind.MONEY
    return True


@functools.lru_cache(maxsize=32)
def _page_figures(page_text: str) -> tuple[tuple[Figure, ...], tuple[int, ...]]:
    figs = tuple(figures(page_text))
    return figs, tuple(f.start for f in figs)


@functools.lru_cache(maxsize=32)
def _page_scaled(page_text: str) -> bool:
    return _SCALE_MARKER.search(page_text) is not None


def value_in_page_span(
    value: object, page_text: str, start: int, end: int, *, kind: FigureKind | str | None = None
) -> bool:
    """True when ``value`` is stated inside ``page_text[start:end]``, read in page context.

    THIS IS THE CHECK TASK 4's VERIFIER USES, with the anchor's raw offsets into
    the page. The figures are read from the WHOLE page, and only those lying
    wholly inside ``[start, end)`` count. Re-reading the bare span instead
    changes what it says: "Lot 5 June 2027" cut to "5 June 2027" reads as a
    date that the page does not state; "A-500,000" cut to "-500,000" gains a
    sign; "65%-70%" cut to "-70%" gains one too; "$3m-5m" cut to "5m" loses its
    currency. A figure the span cuts is not in it.

    Every figure of ``value`` must equal one of those figures:

    - int, float and Decimal values (and a str in exponent form, "1.24E+7") are
      formatted positionally first; a bool is not a number. A None, blank or
      non-finite value is never in a span.
    - ``kind`` given: the matching figure must be of that kind.
    - The value's own written unit applies too, with or without ``kind``: "8.95%"
      matches only a percent; "$8.95" never matches a percent.
    - A unit written right after a value's figure ("5X", "3.5 times", "p.a.",
      "per annum") must follow the matching figure too, in the same class.
    - A str value with two or more figures ("60:40", "3/45", "12 of 15"), or
      with identifying words ("Lot 5A", "Lot 5 DP 123456"), must in addition
      occur in ``page_text[start:end]`` as whole words, in order, after
      normalising width, case, dashes, quotes and whitespace. Qualifier words
      alone ("p.a.", "x", "excl GST") do not force this. An occurrence counts
      only if, on the raw page, no word or figure crosses its edges, no figure
      is glued to it through punctuation ("20:40" in "60:20:40", "3/45" in
      "3/45/7"), and the figures inside it are the value's figures, in order
      and kind ("Stage 2" is not in "Stage 2,500"). A combining mark counts as
      part of a word.
    - A comparator symbol (< > = ~ ± ≤ ≥ ≠), or any other Unicode math symbol
      (category Sm) after NFKC folding ("＜" is "<"; "≦", "≪", "×", "+"), outside
      the value's figures counts as an identifying word: "< 65%" must occur as
      written.
    - A value with no figures is matched on whole words alone, under the same
      edge rules.

    Raises TypeError unless ``start`` and ``end`` are integers (``operator.index``;
    a bool is refused), and ValueError unless ``0 <= start <= end <= len(page_text)``.
    """
    if isinstance(start, bool) or isinstance(end, bool):
        raise TypeError("span offsets must be integers, not bool")
    start, end = operator.index(start), operator.index(end)
    if not 0 <= start <= end <= len(page_text):
        raise ValueError(f"span [{start}, {end}) is not inside a page of {len(page_text)} characters")
    wanted_kind = None if kind is None else FigureKind(kind)
    text = _value_text(value)
    if text is None or not text.strip():
        return False
    wanted = figures(text)
    if not wanted:
        return _slice_word_match(text, page_text, start, end)
    page_figs, starts = _page_figures(page_text)
    present = [
        p
        for p in page_figs[bisect.bisect_left(starts, start) : bisect.bisect_left(starts, end)]
        if p.end <= end
    ]
    scaled = _page_scaled(page_text)
    numeric = isinstance(value, (int, float, Decimal, date))
    written = not numeric and _needs_word_match(text, wanted)
    for w in wanted:
        unit = _unit_after(text, w.end, len(text))
        if not any(
            _same_figure(w, p, wanted_kind, scaled, written)
            and _unit_follows(unit, page_text, p.end, end)
            and _denominator_ok(w, text, p, page_text)
            for p in present
        ):
            return False
    # Only numbers and dates are read purely as figures; every other value type
    # (str, or any object with __str__) is also matched on its written words.
    if written:
        return _slice_word_match(text, page_text, start, end, wanted, wanted_kind)
    return True


def value_in_span(value: object, span_text: str, *, kind: FigureKind | str | None = None) -> bool:
    """``value_in_page_span`` with the span as the whole page.

    Reading a span out of its page can change what it says (see
    ``value_in_page_span``), so a verifier holding page offsets must call
    ``value_in_page_span`` instead.
    """
    return value_in_page_span(value, span_text, 0, len(span_text), kind=kind)


# Public names for what the verifier (Task 4) reuses; naming only.
value_text = _value_text
QUALIFIER_WORDS = _QUALIFIER_WORDS
# D13's page-level scale test, for the table layer (tables.py): a column with no
# scale of its own on a scaled page keeps the page-level ambiguity.
page_scaled = _page_scaled
