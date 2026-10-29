"""One verdict per extracted fact, with a confidence derived from what was checked.

D4 says no anchor, no field. The 2026-09-18 mutation experiment added a second
leg: a matching quote does not prove the figure in it. So a fact is VERIFIED
only when both legs hold (spec §3 step 7, as amended):

1. Anchor. ``anchor.find_quote`` locates the quote on a page: EXACT or FUZZY.
   An AMBIGUOUS anchor whose ``candidates`` include ``cited_page`` resolves to
   the cited page: the quote is re-anchored on that page alone, which yields its
   span, at a small cost (CITED_TIE_PENALTY). Otherwise it stays AMBIGUOUS.
   TOO_SHORT is refused, UNALIGNED flagged.
2. Value. ``values.value_in_page_span(value, page, start, end, kind=kind)`` with
   the anchor's raw offsets - the ONLY value check used here. The span is never
   re-read as bare text (``value_in_span(value, matched_text)`` would lose page
   context: "Lot 5 June 2027" cut to "5 June 2027" reads as a date). ``kind``
   comes from the caller, taken from the field's declared unit.
   - Money is proven only by a MONEY figure (C1, allowlist). A bare NUMBER - no
     currency written - is NEVER verified as money, whatever follows or precedes
     it ("12,400,000 shares", "Account 12400000", "GFA (sqm) 12,400"): the result
     is FLAGGED_VALUE with reason KIND_UNPROVEN - except a table cell under a "$"
     header, read by the table layer (below, "Table rows").
   - ``currency`` (C2) is required when kind is MONEY and allowed for an untyped
     value. Every money figure of the value must be matched by a span figure of
     that currency (``Figure.currency``: "$", "A$", "AU$" and "AUD" read AUD), and
     a currency the value writes must be that currency. Otherwise the result is
     FLAGGED_VALUE with reason CURRENCY_MISMATCH. The result carries ``currency``.
   - The deal currency (fix round 5, DECISIONS.md 2026-09-19). ``deal_currency``
     is the analyst-confirmed currency of the deal, required (it may be None)
     when kind is MONEY. A bare "$" - or "dollars" - in the value or on the page
     reads as it; explicit A$/AU$/AUD/$A/"Australian" figures are never re-read.
     While it is None (unconfirmed), a bare-"$" money fact is CURRENCY_UNPROVEN.
     A currency indicator outside the AUD allowlist {A$, AU$, AUD, AUD$, $A,
     "Australian dollar(s)"} in the span or on the figure's line (the line before
     counts when this one runs on from it: the figure opens the line, or the line
     starts in lower case) is CURRENCY_MISMATCH. The same indicator anywhere else
     in the document only adds the warning FOREIGN_CURRENCY_MENTIONED to a money
     fact. The text is normalised before the scan: whitespace runs (thin, narrow
     and no-break spaces and line breaks too) become one space, a hyphenated line
     break is rejoined, brackets around a code are stripped ("(US) $"), dotted or
     hyphened capitals are joined ("U.S.D.", "U-S-D"). Indicators: letters glued
     to "$" ("US$", "R$"); letters after "$", glued or one space on ("$US",
     "$Can", "$ US"; not the scales "$m", "$mn", "$bn"); 1-2 capitals then "$"
     ("US $"); any ISO 4217 code but AUD, letter-bounded, a capitalised plural
     too ("USDs"), and glued pairs ("AUDUSD") - codes that are everyday words
     (ALL, TOP, TRY, PEN, ...) only in capitals, XXX never; "<word> dollar(s)"
     unless the word is Australian, AUD, a magnitude or a function word ("in",
     "every", "today's"); "dollars" + a qualifier ("(US)", "NZ"); "currency /
     lawful money / legal tender of X" and "dollars of <Proper noun>" where X is
     not Australia; "<word> currency" with no AUD indicator after it; a
     "Currency:"/"Ccy |" label with a non-AUD value; "X-denominated",
     "denominated in X"; pound, euro, yen, yuan, rupee, peso, franc, sterling,
     greenback; any Unicode currency symbol but "$"; any figure values reads in
     another currency. Demonyms ("Singaporean", "Fijian", ...) are weak: a warning,
     never a flag. The scan knows the AUD allowlist only, so for any other deal
     currency every currency indicator on the line counts (conservative). The scan
     is cached per text: a document is scanned once, not once per fact.
   - A comparator (<, >, <=, >=, =, ~, +/-) in a value is identifying: values
     matches it only as written (C3), so "< 65%" never verifies on "> 65%".
   - A bool is not checked against the span (leg 2 does not apply). Its verdict
     is ANCHORED, never VERIFIED - "verified" is reserved for checked values -
     with ``value_checked=False`` and confidence capped at ANCHOR_ONLY_CAP.
   - Blank values (None, "", whitespace) are FLAGGED_VALUE at confidence 0.
   - Role (fix round 2). When the span states two or more figures of the
     value's kind (and, for money, of its currency) and the value equals only
     some of them - "Tranche A is $5,000,000 and Tranche B is $6,000,000" read
     as 6000000 - the span does not show which figure the field is. The verdict
     is FLAGGED_ROLE (ROLE_UNPROVEN), confidence at most ROLE_UNPROVEN_CAP, which
     sits below ``config.EXTRACTION_CONFIDENCE_THRESHOLD`` so the fact goes to
     review. A value equal to ALL of them, in order, is not a role question.
     Plan C's third leg (does the span support THIS field) clears these later.
     An AMBIGUOUS span figure - a range limb such as the "5" of "$5-6m" - is a
     peer when money is asked for and its currency is None or the value's (fix
     round 3). So are money figures whose currency is written after the number
     ("5,000,000 AUD", "5,000,000 dollars") - values reads those as money.
   - Negation (fix round 2). A negation cue - not, no, never, neither, nor,
     other than, except, excluding, without, or a "-n't" contraction - before
     the value in the same clause is FLAGGED_VALUE (NEGATED), for every value
     type. The clause runs back from the value (into the page before the span,
     at most _CLAUSE_REACH characters) to the last ";", ":", "!", "?", a blank
     line, or a "." that ends a sentence: after a digit, "%", a bracket, or a
     word of four or more letters, and followed by whitespace. So decimals
     ("8.95"), ratios ("60:40") and short abbreviations ("Mr.", "No.", "Ltd.",
     "p.a.") never end a clause; a longer clause can only add flags. Where the
     value has no position in the span (a bool, or text not found as written)
     the whole span and its leading clause are searched.
     - Also cues (fix round 2 addition): instead of, save for, rather than, in
       lieu of; and (fix round 3) cannot, none, formerly, previously, ceased to
       be, apart from, excluded. "unless" is NOT a cue (it would flag most
       term-sheet boilerplate).
     - Not cues (fix round 3, fewer false flags): "No." / "no." / "No #" (a
       number), "without limitation".
     - A cue left dangling at a clause break - at most two words between the
       last cue and the break: "will not be: $X", "not approved; $X", "not be
       approx. $X", "is not" then a blank line - carries over the break when
       the value follows the break directly (only spaces, an opening bracket or
       quote between). "is not relevant; the amount is $X" still ends the
       clause. Fail-safe; an addition beyond the round-3 rulings.
     - A GST basis phrase between a dangling cue's break and the value ("not:
       plus GST $X") does not stop the carry-over (fix round 4, fail-safe).
     - After the value (fix round 5, precise patterns only; polarity in
       general is the semantic support check's, Plan C Task 12): a negation or
       rejection phrase ADJACENT to the value - not, never, "no longer",
       cannot, "-n't" (and isnt, wasnt, hasnt, doesnt, dont, wont, cant ...),
       or a rejection verb form (rejected, declined, withdrawn, superseded,
       refused, cancel(l)ed, rescinded, revoked, terminated, voided, lapsed,
       denied, vetoed, annulled, disapproved, ...; never the nouns
       "termination", "cancellation", "withdrawal", "declining", "lapse date")
       - reached from the value's END across at most four filler words
       (auxiliaries, a few adverbs, which/that/who/and: "$X is, however, not
       approved", "$X, which was declined"), straight after ":", ";" or a dash
       ("$X: NOT APPROVED", "$X; declined"), or inside a bracket opened after
       the value, where ":" and ";" start afresh ("$X (status: not approved)").
       Any other word ends the look ("$X and the lender will not charge a fee"
       verifies). GST basis predicates ("not subject to GST", "not including
       GST") and bound phrases ("must not be exceeded", "not to exceed", "shall
       not exceed", "cannot be exceeded") are masked first. For a value with no
       position (a bool) such a phrase anywhere in the span counts.
     - Apostrophes (fix round 5): U+2019, U+02BC, U+2018, U+2032, "`" and
       U+00B4 read as "'" before any negation is matched.
     - Arithmetic: an operator word - less, minus, plus, net of - directly
       before the value (only spaces, ":" or "(" between) is FLAGGED_VALUE
       (ARITHMETIC): the page states a calculation, not the value. "plus GST" and
       "net of GST" stay basis phrases: GST, not the value, follows the operator.
       "plus" after a base-rate token (BBSY, BBSW, BSW, cash rate, RBA cash rate,
       base rate, reference rate) is a margin, not arithmetic (fix round 3):
       "BBSY plus 4.50% per annum" verifies 4.5. Only before a percent value
       (fix round 4, ruling (f)): "the cash rate plus $X" is ARITHMETIC.
       "no less than 1.5x" is a bound ("than" follows "less"), not arithmetic.
     - GST wording ("excluding GST", "not including GST", "GST exclusive", "ex
       GST", "plus GST", "net of GST") is the basis, not a negation: it is
       masked before the cues are searched.
     - A bound phrase - not/no/never/cannot/"-n't", then up to two of "be"/"to", then a
       comparative (less, more, greater, fewer, lower, higher, exceed(s/ed/ing),
       fall/drop/go below or under,
       below, above, later, earlier, sooner, in excess of): "no less than
       1.5x", "not exceeding 65%", "no later than 30 June 2027" - is NOT a
       negation. Choice, over the default flag: the phrase states the figure
       as a bound, so the figure is exactly what the page states; flagging
       would send nearly every covenant to review. It is masked, the value
       verifies, and reason BOUND_PHRASE records the direction for review. A
       value that claims a direction must write it ("<= 65%"), which C3
       matches only as written. "under", "over", "before" and "after" are left
       out of the list because they have non-comparative senses ("not over
       Lot 5"): after them the cue still flags.

Three further checks belong to the value leg:

- Figure order (N2). The ordered figures of the quote, read by ``values.figures``
  on the quote, must equal the ordered figures of the page lying wholly inside
  the anchor's raw span, read by ``values.figures`` in page context - compared
  on value, kind and currency. A fuzzy quote with its figures swapped, one digit
  changed, another currency or another kind is FLAGGED_VALUE (FIGURES_DIFFER).
- GST basis, for kind MONEY or an untyped value only. The span's basis
  (``values.gst_basis`` of the span text) is carried on the result as ``gst``;
  for other kinds ``gst`` is UNSTATED and nothing is compared. A value stating a
  basis (a str such as "$4.12m excl GST") that differs from the span's -
  including a span that states none - is FLAGGED_VALUE (GST_MISMATCH). MIXED, in
  the span or in the value, is a verdict of its own, FLAGGED_GST (GST_MIXED),
  chosen explicitly over FLAGGED_VALUE: both legs held and the value may well be
  right; what is unknown is the qualifier, and a human resolves it.
- Second read. ``second_read`` defaults to the ``_NOT_READ`` sentinel: no second
  read, agreement None. A second read of None or blank disagrees. Agreement
  compares the multiset (``Counter``) of figure values; a figure's kind and
  currency must not conflict where both reads state one (a bare 12400000 agrees
  with "$12,400,000"; "$5m" and "US$5m" do not); the multiset of tokens outside
  the figures - words and every symbol (comparators, ":" and "/" included),
  after values' qualifier words ("p.a.", "x", "incl GST", ...) and sentence
  punctuation are dropped - must be equal; and the two GST bases must be equal.
  A disagreement forces FLAGGED_VALUE.

Table rows (``tables.py``, 2026-09-19). When the caller passes ``field`` - the
fact's citation path - and it names a table column (``...[<key>].senior``,
``equity``, ``total`` or ``mezzanine``) with kind MONEY, the table layer reads
the anchored span as a row, finds its header above it on the page and maps the
field to its column by order (the module docstring there has every rule). When
it proves the mapping:

- The value leg is the column check alone: the value must equal the cell of its
  column times that column's header scale ("$'000", "$m"); equal to another
  cell of the row it is FLAGGED_VALUE (WRONG_COLUMN), equal to none
  VALUE_NOT_IN_SPAN. The cell's money kind and currency come from the cell or,
  for a bare cell, from its header's "$"/"A$"/"US$" (bare "$" reads as the deal
  currency; none is KIND_UNPROVEN). ROLE_UNPROVEN no longer applies: the header
  answers it, recorded as the informational reason COLUMN_PROVEN.
- A row quote under the 6-word minimum (a label and two or more figures, at
  least tables.MIN_ROW_WORDS words) is anchored with that lower minimum and kept
  only when its anchor (or its citation-resolved re-anchor) is EXACT, its
  figures equal the span's (N2) and its column is proven. Otherwise it is
  REFUSED_SHORT_QUOTE exactly as before, with the original TOO_SHORT anchor.
- When the header cannot be found or mapped nothing changes: FLAGGED_ROLE, or
  the refusal, as before. That is the fail-safe.

Vision text (``transcribe.py``, 2026-09-19). A page with no text layer is read
by a vision model and its transcription stands in for the page's text; the
caller names those pages in ``vision_pages``. The value check then runs against
the transcription, and a transcription can misread a digit: a misread would make
a wrong value look stated. So every fact whose anchor lands on a vision page gets
the reason VISION_TEXT and its confidence is capped at VISION_TEXT_CAP, below
``config.EXTRACTION_CONFIDENCE_THRESHOLD``: it goes to review whatever the
verdict, until an independent read (an analyst, a second read) agrees. The
verdict itself is unchanged: VISION_TEXT says what the check stood on, not that
it failed.

Semantic support (``support.py``, Plan C Task 12, DECISIONS.md 2026-09-19). The
third leg asks a model "does this span state that <field> = <value>?". The caller
passes ``support_checker`` (a ``support.SupportChecker`` bound to the field; tests
inject fakes) and ``material``. The check runs only when all hold:

- a checker was passed;
- the deterministic legs left the fact VERIFIED, ANCHORED or FLAGGED_ROLE. Every
  other verdict is already flagged, and nothing a model says can lift it: a model
  never verifies a fact that failed the anchor or the value leg, so it is not
  asked (and not paid for);
- the fact is material, or is FLAGGED_ROLE (ROLE_UNPROVEN), or a negation cue
  sits near the value (``_negation_nearby``: a wider, looser list than the
  deterministic NEGATED patterns - "lacks", "invalid", "pending", "requested",
  "refused" ... - within _CUE_REACH characters of the span, GST basis and bound
  phrases masked). That keeps cost bounded.

It is given the span and _CONTEXT_REACH characters of page on each side. Its
outcome is recorded on ``FactVerification.support`` and applied:

- "no" flags the fact: FLAGGED_VALUE at FLAGGED_VALUE_CONFIDENCE, with the
  model's reason code as a Reason (UNSUPPORTED_NEGATED, UNSUPPORTED_WRONG_ROLE,
  UNSUPPORTED_DIFFERENT_BASIS, UNSUPPORTED_OTHER), whatever its confidence.
- "unclear", or "yes" below SUPPORT_CONFIDENCE_FLOOR, adds SUPPORT_UNCLEAR and
  caps the confidence at SUPPORT_UNCLEAR_CAP, below the review threshold. The
  verdict is unchanged.
- "yes" at or above the floor clears FLAGGED_ROLE, and only that, and only when
  the model names the role: the verdict becomes VERIFIED at the confidence the
  arithmetic gave before the role cap, with the informational reason
  ROLE_SUPPORTED (ROLE_UNPROVEN stays listed: it is what was asked). On any other
  verdict a "yes" changes nothing - no bonus.

The vision cap still applies afterwards.

Confidence table (named constants below; no other numbers take part). It is
checked when the module is imported (``_check_confidence_table``):

====================================  ==========  =================================
Outcome                               Confidence  Constant
====================================  ==========  =================================
blank value                           0.00        BLANK_VALUE_CONFIDENCE
quote under 6 words (refused)         0.00        REFUSED_CONFIDENCE
quote not found (FLAGGED_ANCHOR)      0.20        FLAGGED_ANCHOR_CONFIDENCE
unresolved AMBIGUOUS anchor           0.40        FLAGGED_AMBIGUOUS_CONFIDENCE
value leg failed (FLAGGED_VALUE)      0.30        FLAGGED_VALUE_CONFIDENCE
second read disagrees, value leg ok   0.40        SECOND_READ_DISAGREEMENT_CONFIDENCE
GST basis MIXED (FLAGGED_GST)         0.50        FLAGGED_GST_CONFIDENCE
every flagged outcome above is <=    0.50        FAILED_LEG_CAP
FLAGGED_ROLE is capped at             0.60        ROLE_UNPROVEN_CAP (< config threshold)
VERIFIED, EXACT anchor                0.95        EXACT_CONFIDENCE
VERIFIED, FUZZY anchor                0.85        FUZZY_CONFIDENCE
  cited page differs from the anchor  -0.10       CITED_PAGE_MISMATCH_PENALTY
  AMBIGUOUS resolved by the citation  -0.05       CITED_TIE_PENALTY
  second read agrees                  +0.05       SECOND_READ_AGREEMENT_BONUS
  never above                         0.99        CONFIDENCE_CAP
ANCHORED (bool), never above          0.80        ANCHOR_ONLY_CAP
anchored on a vision page, never abv  0.65        VISION_TEXT_CAP (< config threshold)
support "unclear" or low, never abv   0.60        SUPPORT_UNCLEAR_CAP (< config threshold)
support "yes" counts from             0.80        SUPPORT_CONFIDENCE_FLOOR (model's own)
====================================  ==========  =================================

Arithmetic runs in that order (base, one penalty, bonus, 0.99 cap, bool cap) and
is rounded to 2 places. A resolved tie takes the base of its single-page
re-anchor and the tie penalty, never the mismatch penalty. When several flags
apply, the verdict is the first of: blank, refused, anchor, ambiguous, value
leg (FLAGGED_VALUE at 0.30, NEGATED included), second read (FLAGGED_VALUE at
0.40), GST MIXED, role (FLAGGED_ROLE); every reason found is still listed in
``reasons``. FLAGGED_ROLE takes the arithmetic above, then the role cap.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from enum import Enum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from creditpaper import config
from creditpaper.ingest.anchor import MIN_QUOTE_WORDS, AnchorResult, AnchorStatus, find_quote
from creditpaper.ingest.support import SupportChecker, SupportOutcome, SupportReason
from creditpaper.ingest.tables import (
    MIN_ROW_WORDS,
    ColumnCheck,
    ColumnOutcome,
    check_column,
    field_column,
    is_row_quote,
)
from creditpaper.ingest.values import (
    QUALIFIER_WORDS,
    Figure,
    FigureKind,
    GstBasis,
    figures,
    gst_basis,
    value_in_page_span,
    value_text,
)
from creditpaper.model.core import FactBasis, VerificationSummary

# --- the confidence table (see the module docstring) --------------------------
EXACT_CONFIDENCE: Final = 0.95
FUZZY_CONFIDENCE: Final = 0.85
CITED_PAGE_MISMATCH_PENALTY: Final = 0.10
CITED_TIE_PENALTY: Final = 0.05
SECOND_READ_AGREEMENT_BONUS: Final = 0.05
CONFIDENCE_CAP: Final = 0.99
ANCHOR_ONLY_CAP: Final = 0.80
FAILED_LEG_CAP: Final = 0.50
BLANK_VALUE_CONFIDENCE: Final = 0.0
REFUSED_CONFIDENCE: Final = 0.0
FLAGGED_ANCHOR_CONFIDENCE: Final = 0.20
FLAGGED_VALUE_CONFIDENCE: Final = 0.30
FLAGGED_AMBIGUOUS_CONFIDENCE: Final = 0.40
SECOND_READ_DISAGREEMENT_CONFIDENCE: Final = 0.40
FLAGGED_GST_CONFIDENCE: Final = 0.50
# Below config.EXTRACTION_CONFIDENCE_THRESHOLD (0.7): a role question goes to review.
ROLE_UNPROVEN_CAP: Final = 0.60
# Below config.EXTRACTION_CONFIDENCE_THRESHOLD (0.7): a fact checked against a
# vision transcription goes to review until an independent read agrees.
VISION_TEXT_CAP: Final = 0.65
# Semantic support (support.py). Below config.EXTRACTION_CONFIDENCE_THRESHOLD: an
# "unclear" (or a low-confidence) answer goes to review.
SUPPORT_UNCLEAR_CAP: Final = 0.60
# The model's own confidence under which its answer counts as unclear.
SUPPORT_CONFIDENCE_FLOOR: Final = 0.80
_DECIMALS: Final = 2

# ``second_read`` default: no second read was made. Distinct from a second read
# that returned None, which is a disagreement.
_NOT_READ: Final = object()
# ``deal_currency`` default: not passed (an error for kind MONEY). Distinct from
# None, which says the analyst has not confirmed the deal currency yet.
_DEAL_NOT_GIVEN: Final = object()

# A token outside the figures: a word, or any single symbol.
_TOKEN = re.compile(r"\w+|[^\w\s]")
# Sentence punctuation - followed by whitespace or the end - is not a token.
_SENTENCE_PUNCT = re.compile(r"[.,;:](?=\s|$)")

# --- negation (fix round 2; precise patterns only since fix round 5) -----------
# Every apostrophe-like character reads as "'" before any negation is matched
# (fix round 5): one-for-one, so offsets are kept.
_APOSTROPHES: Final = str.maketrans(dict.fromkeys("’ʼ‘′`´", "'"))
# Contractions: "-n't", and the forms written without the apostrophe.
_NT = r"\w+n't|isnt|wasnt|arent|werent|hasnt|havent|hadnt|doesnt|didnt|dont|wont|cant"
_NEGATION_CUE = re.compile(
    r"(?i)(?<!\w)(?:not|no(?!\.|\s*\#)|never|neither|nor|other\s+than|except|excluding"
    r"|without(?!\s+limitation)|" + _NT + r"|cannot|none|formerly|previously|ceased\s+to\s+be"
    r"|apart\s+from|excluded|instead\s+of|save\s+for|rather\s+than|in\s+lieu\s+of)(?!\w)"
)
# "plus" after a base-rate token is a margin, not arithmetic (fix round 3).
_MARGIN_PLUS = re.compile(
    r"(?i)(?<!\w)(?:bbsy|bbsw|bsw|(?:rba\s+)?cash\s+rate|base\s+rate|reference\s+rate)\s+plus[\s:(]*\Z"
)
_MARGIN_REACH: Final = 60
# At most this many words between a clause's last cue and the break: the cue dangles.
_DANGLING_WORDS: Final = 2
# After the value (fix round 5): a negation or rejection phrase ADJACENT to it.
# Rejection words match their verb forms only - "termination", "cancellation",
# "withdrawal", "declining" and "lapse date" are nouns and never match.
_REJECTED = (
    r"rejected|declined|withdrawn|withdrew|superseded|refused|cancell?ed|rescinded|revoked"
    r"|terminated|voided|void|lapsed|lapses|denied|vetoed|annulled|disapproved|unapproved"
    r"|abandoned|overturned"
)
_AFTER_PHRASE = re.compile(r"(?i)(?:not|never|no\s+longer|cannot|" + _NT + r"|" + _REJECTED + r")(?!\w)")
# Words that may stand between the value and that phrase: auxiliaries, a few
# adverbs, and relative or joining words ("$X, which is not approved", "$X is,
# however, not approved"). Any other word (a subject, a noun) ends the look.
_AFTER_FILLERS: Final = frozenset(
    "is was are were be been being has have had will shall would should may might must can could"
    " does did do also still however currently yet now since then therefore subsequently ultimately"
    " formally accordingly thus so which that who and".split()
)
_AFTER_MAX_FILLERS: Final = 4
_AFTER_REACH: Final = 120
# Tokens of the look after the value: a word, or a punctuation mark that matters.
_AFTER_TOKEN = re.compile(r"[:;(\[)\]—–]|\w+(?:'\w+)?")
# Where the look after the value stops: the end of the sentence.
_SENTENCE_END = re.compile(r"\n[ \t]*\n|[!?](?=\s|$)|(?<=[A-Za-z]{4})\.(?=\s|$)|(?<=[0-9%)\]])\.(?=\s|$)")
_WORD_RUN = re.compile(r"\w+")
# An arithmetic operator word directly before the value (only spaces, ":" or "("
# between): the page states a calculation, not the value. "plus GST" and "net of
# GST" never match here - "GST", not the value, follows the operator.
_ARITHMETIC_BEFORE = re.compile(r"(?i)(?<!\w)(?:less|minus|plus|net\s+of)[\s:(]*\Z")
_ARITHMETIC_REACH: Final = 20
# A cue followed by a comparative states a bound, not a negation - before or
# after the value ("must not be exceeded", "not to exceed", "shall not exceed").
_BOUND_PHRASE = re.compile(
    r"(?i)(?<!\w)(?:not|no|never|cannot|" + _NT + r")\s+(?:(?:be|to)\s+){0,2}"
    r"(?:less|more|greater|fewer|lower|higher|exceed(?:s|ed|ing)?|below|above|later|earlier|sooner"
    r"|in\s+excess\s+of|(?:fall|drop|go)\s+(?:below|under|short))(?!\w)"
)
# GST wording is the basis, never a negation of the value.
_GST_WORDS = r"(?:gst|g\.s\.t\.?|goods\s+(?:and|&)\s+services\s+tax)"
_GST_PHRASE = re.compile(
    r"(?i)(?<!\w)(?:(?:not|" + _NT + r")\s+)?(?:incl\w*|inc|excl\w*|exc|ex|plus|net\s+of)\.?\s*"
    r"(?:\(\s*)?(?:of\s+)?(?:-\s*)?" + _GST_WORDS + r"(?!\w)"
    r"|(?<!\w)(?:(?:not|" + _NT + r")\s+)?" + _GST_WORDS + r"\s*(?:[-:(]\s*)?(?:(?:is|are)\s+)?(?:not\s+)?"
    r"(?:incl\w*|excl\w*|inc|exc|ex)(?!\w)"
)
# A GST basis predicate after the value is not a negation: "not subject to GST".
_GST_NOT_SUBJECT = re.compile(
    r"(?i)(?<!\w)(?:not|never|"
    + _NT
    + r"|cannot)\s+(?:(?:be|been)\s+)?subject\s+to\s+"
    + _GST_WORDS
    + r"(?!\w)"
)
# Where a clause ends: ; : ! ? or a blank line anywhere; "." only after a digit,
# "%", a bracket or a word of four or more letters, and before whitespace.
_CLAUSE_END = re.compile(r"[;:!?](?=\s|$)|\n[ \t]*\n|(?<=[A-Za-z]{4})\.(?=\s)|(?<=[0-9%)\]])\.(?=\s)")

# --- currency (fix round 5: the analyst-confirmed deal currency) -------------
# A bare "$" (or "dollars") reads as the deal currency. A foreign indicator in the
# span or on the figure's line is CURRENCY_MISMATCH; one elsewhere in the document
# is a warning. The scan below finds indicators of a currency outside the AUD
# allowlist {A$, AU$, AUD, AUD$, $A, "Australian dollar(s)"} (fix round 4 list,
# widened in round 5), on text normalised first (_scan_text).
_AUD_LETTERS: Final = frozenset({"A", "AU", "AUD"})
# ISO 4217 alphabetic codes (current, fund and metal codes), AUD removed. XXX
# ("no currency") is left out: it is the draft placeholder ("$XXX").
_ISO_4217: Final = frozenset(
    (
        "AED AFN ALL AMD ANG AOA ARS AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP "
        "BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN "
        "ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HRK HTG HUF IDR ILS INR IQD IRR ISK "
        "JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK "
        "MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN "
        "PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB "
        "TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG "
        "XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU XTS XUA YER ZAR ZMW ZWG ZWL"
    ).split()
)
# Codes that are also everyday English words or names ("all", "top", "try"): a
# case-insensitive match would flag every document, so these match in capitals only.
_ISO_WORDS: Final = frozenset(
    {"ALL", "BAM", "BOB", "COP", "CUP", "ERN", "GEL", "MAD", "MOP", "PEN", "RON", "RUB", "SOS", "TOP", "TRY"}
)
# A code, alone or plural ("USDs"), letter-bounded.
_CODE_WORD = re.compile(r"(?<![A-Za-z])(?P<code>[A-Za-z]{3})(?P<plural>s?)(?![A-Za-z])")
# Two codes glued as a currency pair ("AUDUSD", "USDAUD", "NZDAUD").
_CODE_PAIR = re.compile(r"(?<![A-Za-z])(?P<a>[A-Z]{3})(?P<b>[A-Z]{3})(?![A-Za-z])")
# Letters glued before "$" ("US$", "S$", "R$", "usd$"); letters after "$", glued
# or (capitals) after one space ("$US", "$Can", "$Sing", "$us", "$ US"; not the
# scales "$m", "$mn", "$bn", "$k"); 1-2 capitals then at most one space or hyphen
# then "$" ("US $", "US-$"). Dotted and bracketed letters are joined first.
_LETTERS_DOLLAR = re.compile(
    r"(?<![A-Za-z])(?P<before>[A-Za-z]+)\$"
    r"|\$(?P<after>[A-Za-z]{2,4})(?![A-Za-z])|\$ (?P<after_spaced>[A-Z]{2,3})(?![A-Za-z])"
    r"|(?<![A-Za-z])(?P<spaced>[A-Z]{1,2})[ -]?\$"
)
_SCALE_AFTER_DOLLAR: Final = frozenset({"MN", "BN", "MIL", "MM", "BIL", "THOU"})
# "<word> dollar(s)", the word glued, hyphened or spaced ("US dollars",
# "US-dollars", "USdollars", "in dollars"), where the word is not Australian.
_WORD_DOLLAR = re.compile(r"(?i)(?<![A-Za-z0-9])(?P<word>[A-Za-z]+)[\s-]*dollars?(?![A-Za-z])")
# Not a qualifier: the allowlist, and the magnitude of a figure ("5 million dollars").
_DOLLAR_WORD_OK: Final = frozenset(
    {"australian", "aud", "million", "millions", "billion", "billions", "thousand", "thousands"}
    | {"trillion", "mn", "bn", "mil", "m", "k", "b"}
)
# "dollars" followed by a country or code qualifier (fix round 4, ruling (c)).
_DOLLAR_QUALIFIER = re.compile(
    r"(?i:dollars?)(?![A-Za-z]) ?(?:(?i:us|nz)(?![A-Za-z])|(?!(?:AUD|A)(?![A-Za-z]))[A-Z]{2,3}(?![A-Za-z]))"
)
# "currency of X", "lawful money of X", "legal tender of X", where X is not Australia.
_MONEY_OF = re.compile(
    r"(?i)(?<![A-Za-z])(?:currency|lawful\s+money|legal\s+tender|lawful\s+currency)\s+of\s+(?:the\s+)?"
    r"(?!(?:commonwealth\s+of\s+)?australia(?![A-Za-z]))[A-Za-z]"
    # "dollars of the United States": a proper noun after "of" ("dollars of debt" is not).
    r"|(?<![A-Za-z])(?i:dollars?)\s+of\s+(?:the\s+)?(?!(?:Commonwealth\s+of\s+)?Australia(?![A-Za-z]))[A-Z]"
)
# "<word> currency", unless the word is Australian/AUD or an AUD indicator follows.
_WORD_CURRENCY = re.compile(r"(?i)(?<![A-Za-z0-9])(?P<word>[A-Za-z]+)[ -]currency(?![A-Za-z])")
# A currency label and its value: "Currency: US", "Ccy | SG".
_CURRENCY_LABEL = re.compile(r"(?i)(?<![A-Za-z])(?:currency|ccy)(?![A-Za-z]) ?[:|] ?(?P<value>[A-Za-z$]+)")
_AUD_LABEL_VALUES: Final = frozenset({"AUD", "A$", "AU$", "AUD$", "$A", "AUSTRALIAN", "$"})
# "<X>-denominated", "denominated in <X>", where X is not Australian.
_DENOMINATED = re.compile(r"(?i)(?<![A-Za-z])(?P<a>[A-Za-z]+)-denominated|denominated in (?P<b>[A-Za-z$]+)")
_CURRENCY_WORDS = re.compile(
    r"(?i)(?<![A-Za-z])(?:pounds?|euros?|yen|yuan|rupees?|pesos?|francs?|sterling|greenbacks?)(?![A-Za-z])"
)
# Weak indicators - demonyms of a currency's country - are a warning only, never
# a flag: "the Singaporean sponsor" on the figure's line says nothing about "$".
_DEMONYMS = re.compile(
    r"(?<![A-Za-z])(?:American|Canadian|Singaporean|Fijian|Kiwi|Japanese|British|European|Swiss"
    r"|Chinese|Indian|Malaysian|Indonesian|Hong\s+Kong|New\s+Zealand)(?![A-Za-z])"
)
# A money figure written explicitly as Australian, not by a bare "$".
_EXPLICIT_AUD = re.compile(r"AUD|AU\$|A\$|\$A|(?i:australian)")
_AUD_NEAR = re.compile(r"AUD|AU\$|A\$|\$A|(?i:australian)")
# How far before the span a clause may start.
_CLAUSE_REACH: Final = 300


def _check_confidence_table() -> None:
    """Every flagged outcome sits at or under FAILED_LEG_CAP; every pass above it."""
    flagged = (
        BLANK_VALUE_CONFIDENCE,
        REFUSED_CONFIDENCE,
        FLAGGED_ANCHOR_CONFIDENCE,
        FLAGGED_AMBIGUOUS_CONFIDENCE,
        FLAGGED_VALUE_CONFIDENCE,
        SECOND_READ_DISAGREEMENT_CONFIDENCE,
        FLAGGED_GST_CONFIDENCE,
    )
    lowest_pass = min(
        min(EXACT_CONFIDENCE, FUZZY_CONFIDENCE) - max(CITED_PAGE_MISMATCH_PENALTY, CITED_TIE_PENALTY),
        ANCHOR_ONLY_CAP,
    )
    if not (
        all(0.0 <= c <= FAILED_LEG_CAP for c in flagged)
        and FAILED_LEG_CAP < lowest_pass
        and max(EXACT_CONFIDENCE, FUZZY_CONFIDENCE) <= CONFIDENCE_CAP <= 1.0
        and ANCHOR_ONLY_CAP <= CONFIDENCE_CAP
        and ROLE_UNPROVEN_CAP < config.EXTRACTION_CONFIDENCE_THRESHOLD
        and FAILED_LEG_CAP < VISION_TEXT_CAP < config.EXTRACTION_CONFIDENCE_THRESHOLD
        and SUPPORT_UNCLEAR_CAP < config.EXTRACTION_CONFIDENCE_THRESHOLD
        and 0.0 < SUPPORT_CONFIDENCE_FLOOR <= 1.0
    ):
        raise AssertionError("verify.py confidence table is inconsistent")


_check_confidence_table()


class Verdict(str, Enum):
    VERIFIED = "verified"
    # A bool fact: the quote is anchored, the value was not (cannot be) checked.
    ANCHORED = "anchored"
    FLAGGED_VALUE = "flagged_value"
    FLAGGED_ANCHOR = "flagged_anchor"
    FLAGGED_AMBIGUOUS = "flagged_ambiguous"
    # Both legs held, but the span (or the value) states both GST bases.
    FLAGGED_GST = "flagged_gst"
    # The value is stated, but so are other figures of its kind: which one is it?
    FLAGGED_ROLE = "flagged_role"
    REFUSED_SHORT_QUOTE = "refused_short_quote"


class Reason(str, Enum):
    """Why a verdict is what it is. Every reason found is listed, for review."""

    BLANK_VALUE = "blank_value"
    QUOTE_TOO_SHORT = "quote_too_short"
    QUOTE_NOT_FOUND = "quote_not_found"
    AMBIGUOUS_ANCHOR = "ambiguous_anchor"
    RESOLVED_BY_CITATION = "resolved_by_citation"
    VALUE_NOT_IN_SPAN = "value_not_in_span"
    # Money asked for, but the span states the figure without a currency.
    KIND_UNPROVEN = "kind_unproven"
    CURRENCY_MISMATCH = "currency_mismatch"
    # AUD read from a bare "$" on a page that states another currency.
    CURRENCY_UNPROVEN = "currency_unproven"
    FIGURES_DIFFER = "figures_differ"
    GST_MISMATCH = "gst_mismatch"
    GST_MIXED = "gst_mixed"
    SECOND_READ_DISAGREES = "second_read_disagrees"
    CITED_PAGE_MISMATCH = "cited_page_mismatch"
    VALUE_NOT_CHECKED = "value_not_checked"
    ROLE_UNPROVEN = "role_unproven"
    NEGATED = "negated"
    # An operator word ("less", "minus", "plus", "net of") directly before the value.
    ARITHMETIC = "arithmetic"
    # Informational: the value is stated as a bound ("no less than 1.5x").
    BOUND_PHRASE = "bound_phrase"
    # Table rows (tables.py): the value is in the row, but under another column.
    WRONG_COLUMN = "wrong_column"
    # Informational: the value's column was proven against the row's header.
    COLUMN_PROVEN = "column_proven"
    # The anchor's page text is a vision transcription (transcribe.py), not the
    # page's own text layer: confidence capped at VISION_TEXT_CAP, for review.
    VISION_TEXT = "vision_text"
    # The same, for a page read by OCR because it had no text layer. A separate
    # reason rather than reusing VISION_TEXT, because an analyst reviewing needs
    # to know WHICH reading they are looking at - Tesseract at 200 DPI and a
    # vision model fail differently - but the cap is the same, because the
    # objection is the same: the text is a reading of the page, not the page.
    OCR_TEXT = "ocr_text"
    # Semantic support (support.py): the model answered "no", with this reason.
    UNSUPPORTED_NEGATED = "unsupported_negated"
    UNSUPPORTED_WRONG_ROLE = "unsupported_wrong_role"
    UNSUPPORTED_DIFFERENT_BASIS = "unsupported_different_basis"
    UNSUPPORTED_OTHER = "unsupported_other"
    # The model answered "unclear", or "yes" under SUPPORT_CONFIDENCE_FLOOR.
    SUPPORT_UNCLEAR = "support_unclear"
    # Informational: a confident "yes" naming the role cleared FLAGGED_ROLE.
    ROLE_SUPPORTED = "role_supported"
    # The fact was WORTH a semantic check and could not have one: no concept
    # names its path, so `checker_for` has no definition to ask about. Recorded
    # rather than skipped, because a fact nothing could check must not be
    # indistinguishable from one that passed.
    SUPPORT_UNAVAILABLE = "support_unavailable"


_UNSUPPORTED: Final = {
    SupportReason.NEGATED: Reason.UNSUPPORTED_NEGATED,
    SupportReason.WRONG_ROLE: Reason.UNSUPPORTED_WRONG_ROLE,
    SupportReason.DIFFERENT_BASIS: Reason.UNSUPPORTED_DIFFERENT_BASIS,
    SupportReason.OTHER: Reason.UNSUPPORTED_OTHER,
}


class FactWarning(str, Enum):
    """Something review should see that does not change the verdict (fix round 5)."""

    # A currency other than AUD is mentioned somewhere in the document, away from
    # the fact's span and line (there it would be CURRENCY_MISMATCH).
    FOREIGN_CURRENCY_MENTIONED = "foreign_currency_mentioned"


# Reasons that fail the value leg outright.
_VALUE_LEG_FAILURES: Final = frozenset(
    {
        Reason.VALUE_NOT_IN_SPAN,
        Reason.KIND_UNPROVEN,
        Reason.CURRENCY_MISMATCH,
        Reason.CURRENCY_UNPROVEN,
        Reason.FIGURES_DIFFER,
        Reason.GST_MISMATCH,
        Reason.NEGATED,
        Reason.ARITHMETIC,
        Reason.WRONG_COLUMN,
    }
)


class FactVerification(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    confidence: float = Field(ge=0, le=1)
    page: int | None = None
    span: tuple[int, int] | None = None
    # What find_quote returned over the whole document.
    anchor: AnchorResult
    # The single-page re-anchor on the cited page, when an AMBIGUOUS anchor was
    # resolved by the citation; None otherwise.
    resolved_anchor: AnchorResult | None = None
    # The span's GST basis (kind MONEY or untyped only; UNSTATED otherwise).
    gst: GstBasis = GstBasis.UNSTATED
    # The currency the caller required (kind MONEY), or None.
    currency: str | None = None
    # The analyst-confirmed deal currency a bare "$" was read as, or None.
    deal_currency: str | None = None
    # None: no second read. True/False: the second read agreed or not.
    agreement: bool | None = None
    # False when the value was not checked against the span (a bool, or a
    # verdict reached before the value leg ran).
    value_checked: bool
    reasons: tuple[Reason, ...] = ()
    warnings: tuple[FactWarning, ...] = ()
    # The semantic support check's answer, when it ran (support.py).
    support: SupportOutcome | None = None

    def summary(self) -> VerificationSummary:
        """What the fact carries (``Fact.verification``): verdict, reasons, confidence."""
        return VerificationSummary(
            verdict=self.verdict.value,
            reasons=tuple(r.value for r in self.reasons),
            confidence=self.confidence,
        )

    def basis(self) -> FactBasis:
        """The qualifiers verification read from the span, for ``Fact.basis``.

        The GST basis is always read (UNSTATED when the span states none). The
        currency is recorded only when the fact verified: then it is the currency the
        span proved, the one required or the confirmed deal currency a bare "$" was
        read as. A flagged fact's currency is not proven, so it stays None.
        """
        proven = self.verdict is Verdict.VERIFIED
        return FactBasis(gst=self.gst, currency=(self.currency or self.deal_currency) if proven else None)


def _blank(value: object) -> bool:
    return value is None or not str(value).strip()


@dataclass(frozen=True)
class _Reading:
    """What a value states, for comparing two reads of one field."""

    values: Counter[str]
    # Per figure value: the kinds (NUMBER excluded) and currencies it is written with.
    kinds: dict[str, frozenset[FigureKind]]
    currencies: dict[str, frozenset[str]]
    tokens: Counter[str]
    gst: GstBasis


def _reading(value: object) -> _Reading | None:
    text = value_text(value)
    if text is None or not text.strip():
        return None
    figs = figures(text)
    kinds: dict[str, set[FigureKind]] = {}
    currencies: dict[str, set[str]] = {}
    for f in figs:
        kinds.setdefault(f.value, set())
        currencies.setdefault(f.value, set())
        if f.kind is not FigureKind.NUMBER:
            kinds[f.value].add(f.kind)
        if f.currency is not None:
            currencies[f.value].add(f.currency)
    pieces: list[str] = []
    last = 0
    for f in figs:
        pieces.append(text[last : f.start])
        last = f.end
    pieces.append(text[last:])
    rest = QUALIFIER_WORDS.sub(" ", " ".join(pieces))
    rest = _SENTENCE_PUNCT.sub(" ", rest)
    return _Reading(
        values=Counter(f.value for f in figs),
        kinds={v: frozenset(k) for v, k in kinds.items()},
        currencies={v: frozenset(c) for v, c in currencies.items()},
        tokens=Counter(_TOKEN.findall(unicodedata.normalize("NFKC", rest).casefold())),
        gst=gst_basis(text),
    )


def _agrees(value: object, other: object) -> bool:
    """Does a second read state the same value? Order-free; None or blank never agrees."""
    a, b = _reading(value), _reading(other)
    if a is None or b is None:
        return False
    if a.values != b.values or a.tokens != b.tokens or a.gst is not b.gst:
        return False
    return all(
        len(a.kinds[v] | b.kinds[v]) <= 1 and len(a.currencies[v] | b.currencies[v]) <= 1 for v in a.values
    )


def _key(f: Figure) -> tuple[str, FigureKind, str | None]:
    return (f.value, f.kind, f.currency)


def _bare(text: str, f: Figure) -> bool:
    """AUD read only from a bare "$" or "dollars": it means the deal currency."""
    return f.currency == "AUD" and _EXPLICIT_AUD.search(text, f.start, f.end) is None


def _effective_currency(text: str, f: Figure, deal: str | None) -> str | None:
    return deal if _bare(text, f) else f.currency


def _figure_region(page_text: str, start: int, end: int, figure: Figure) -> str:
    """The span and the whole lines it touches - plus the line before, when that
    line runs on into this one: the figure opens its line ("US" then a line break
    then "$12,400,000"), or the line starts in lower case (a wrapped sentence:
    "... in US" then "dollars. The limit is $12,400,000")."""
    line_start = page_text.rfind("\n", 0, min(start, figure.start)) + 1
    head = page_text[line_start:].lstrip(" \t")
    opens_line = not page_text[line_start : figure.start].strip(" \t([{\"'")
    if line_start > 0 and (opens_line or head[:1].islower()):
        line_start = page_text.rfind("\n", 0, line_start - 1) + 1
    line_end = page_text.find("\n", end)
    return page_text[line_start : len(page_text) if line_end < 0 else line_end]


def _money_reason(
    value: object,
    page_text: str,
    start: int,
    end: int,
    span_figures: list[Figure],
    currency: str | None,
    deal: str | None,
) -> Reason | None:
    """The currency question for each money (or, when a currency is asked, bare) figure.

    A bare "$" - in the value or on the page - reads as the deal currency; with
    no confirmed deal currency it is CURRENCY_UNPROVEN. A figure in another
    currency, or a bare-"$" figure with a foreign indicator in the span or on its
    line, is CURRENCY_MISMATCH. Explicit A$/AUD figures are never re-read.
    """
    text = value_text(value)
    if text is None:  # a non-finite number is in no span
        return Reason.VALUE_NOT_IN_SPAN
    for w in figures(text):
        if w.kind is FigureKind.NUMBER and currency is None:
            continue  # an untyped bare number asks no currency question
        if w.kind not in (FigureKind.MONEY, FigureKind.NUMBER):
            continue
        wanted = currency
        if w.kind is FigureKind.MONEY:
            own = _effective_currency(text, w, deal)
            if own is None:
                return Reason.CURRENCY_UNPROVEN  # the value's own bare "$", no deal currency
            if wanted is not None and own != wanted:
                return Reason.CURRENCY_MISMATCH
            wanted = own
        money = [p for p in span_figures if p.value == w.value and p.kind is FigureKind.MONEY]
        if not money:
            return Reason.KIND_UNPROVEN
        matched = [p for p in money if _effective_currency(page_text, p, deal) == wanted]
        if not matched:
            if deal is None and any(_bare(page_text, p) for p in money):
                return Reason.CURRENCY_UNPROVEN
            return Reason.CURRENCY_MISMATCH
        if all(_bare(page_text, p) for p in matched) and any(
            _foreign_on_line(_figure_region(page_text, start, end, p), deal) for p in matched
        ):
            return Reason.CURRENCY_MISMATCH
    return None


def _value_reason(
    value: object,
    page_text: str,
    start: int,
    end: int,
    span_figures: list[Figure],
    kind: FigureKind | None,
    currency: str | None,
    deal: str | None,
) -> Reason | None:
    """Why the value leg fails, or None when the span states the value."""
    if not value_in_page_span(value, page_text, start, end, kind=kind):
        if kind is FigureKind.MONEY and value_in_page_span(
            value, page_text, start, end, kind=FigureKind.NUMBER
        ):
            return Reason.KIND_UNPROVEN  # stated only as a bare number
        return Reason.VALUE_NOT_IN_SPAN
    if currency is not None or kind is None:
        return _money_reason(value, page_text, start, end, span_figures, currency, deal)
    return None


# --- the foreign-currency scan (fix rounds 4-5) --------------------------------
_SOFT_BREAK = re.compile(r"-[ \t]*\r?\n\s*")
_WHITESPACE = re.compile(r"\s+")
_BRACKETED_CODE = re.compile(r"[(\[] ?([A-Za-z.]{1,5}) ?[)\]]")
_DOTTED_LETTERS = re.compile(r"(?<![A-Za-z])((?:[A-Za-z]\.){2,3})(?![A-Za-z])")
_HYPHENED_LETTERS = re.compile(r"(?<![A-Za-z])[A-Z](?:-[A-Z]){1,2}(?![A-Za-z])")
# Two capitals before " $" that are words or units, not currencies ("PA $850").
_SPACED_OK: Final = frozenset(
    {"A", "AU", "PA", "PM", "EA", "TO", "OF", "IS", "IN", "AT", "OR", "ON", "BY", "UP", "NO", "AS", "IF"}
)
# Words before "dollar(s)" that are not a country qualifier ("in dollars",
# "today's dollars", "dollar-for-dollar", "every dollar").
_DOLLAR_FUNCTION_WORDS: Final = frozenset(
    "in of the a an every each per for s any many more fewer few those these such same all total"
    " nominal real current constant".split()
)
_CURRENCY_FUNCTION_WORDS: Final = frozenset("the a an this that same such its their whose which".split())
_DENOMINATED_OK: Final = frozenset(
    {"AUD", "AUSTRALIAN", "A$", "AU$", "AUD$", "$A", "THE", "A", "AN", "DOLLARS"}
)


def _scan_text(text: str) -> str:
    """Normalise before scanning: rejoin a word split by a hyphenated line break,
    collapse every whitespace run (thin, narrow and no-break spaces and line
    breaks included) to one space, strip brackets around a code ("(US) $"), and
    join dotted or hyphened capitals ("U.S.D.", "U-S-D")."""
    text = _WHITESPACE.sub(" ", _SOFT_BREAK.sub("", text))
    text = _BRACKETED_CODE.sub(r"\1", text)
    text = _DOTTED_LETTERS.sub(lambda m: m.group(1).replace(".", ""), text)
    return _HYPHENED_LETTERS.sub(lambda m: m.group().replace("-", ""), text)


def _strong_indicator(t: str) -> bool:
    """A currency outside the AUD allowlist, stated in normalised text ``t``."""
    for m in _LETTERS_DOLLAR.finditer(t):
        if m["before"] is not None and m["before"].upper() not in _AUD_LETTERS:
            return True
        if m["after"] is not None and m["after"].upper() not in _AUD_LETTERS | _SCALE_AFTER_DOLLAR:
            return True
        if m["after_spaced"] is not None and m["after_spaced"] not in _AUD_LETTERS | _SCALE_AFTER_DOLLAR:
            return True
        if m["spaced"] is not None and m["spaced"] not in _SPACED_OK:
            return True
    for m in _CODE_WORD.finditer(t):
        code = m["code"].upper()
        if code not in _ISO_4217:
            continue
        written_in_capitals = m["code"] == code
        if (code not in _ISO_WORDS or written_in_capitals) and (not m["plural"] or written_in_capitals):
            return True
    for m in _CODE_PAIR.finditer(t):
        a, b = m["a"], m["b"]
        if {a, b} <= _ISO_4217 | {"AUD"} and (a in _ISO_4217 or b in _ISO_4217):
            return True
    for m in _WORD_DOLLAR.finditer(t):
        word = m["word"].lower()
        if word not in _DOLLAR_WORD_OK and word not in _DOLLAR_FUNCTION_WORDS:
            return True
    for m in _WORD_CURRENCY.finditer(t):
        word = m["word"].lower()
        if word in ("australian", "aud") or word in _CURRENCY_FUNCTION_WORDS:
            continue
        if _AUD_NEAR.search(t, m.end(), m.end() + 25) is None:
            return True
    for m in _CURRENCY_LABEL.finditer(t):
        if m["value"].upper() not in _AUD_LABEL_VALUES:
            return True
    for m in _DENOMINATED.finditer(t):
        if (m["a"] or m["b"] or "").upper() not in _DENOMINATED_OK:
            return True
    if (
        _DOLLAR_QUALIFIER.search(t) is not None
        or _MONEY_OF.search(t) is not None
        or _CURRENCY_WORDS.search(t) is not None
    ):
        return True
    return any(ch != "$" and unicodedata.category(ch) == "Sc" for ch in set(t))


@functools.lru_cache(maxsize=4096)
def _indicators(text: str) -> tuple[bool, bool]:
    """(a strong foreign-currency indicator, a weak one - a demonym) in ``text``.

    Cached per text, so a document's pages are scanned once, not once per fact.
    """
    strong = _strong_indicator(_scan_text(text)) or any(
        f.kind is FigureKind.MONEY and f.currency not in (None, "AUD") for f in figures(text)
    )
    return strong, _DEMONYMS.search(_scan_text(text)) is not None


def _foreign_on_line(region: str, deal: str | None) -> bool:
    """A currency other than the deal's, stated in the span or on the figure's line.

    The scan knows the AUD allowlist. For another deal currency it is
    conservative: any currency indicator at all on the line counts.
    """
    strong, _ = _indicators(region)
    if deal in (None, "AUD"):
        return strong
    return strong or _AUD_NEAR.search(_scan_text(region)) is not None


def _foreign_mentioned(pages: dict[int, str]) -> bool:
    """Any foreign-currency indicator, strong or weak, anywhere in the document."""
    return any(any(_indicators(text)) for text in pages.values())


_NUMERIC_KINDS: Final = frozenset({FigureKind.MONEY, FigureKind.PERCENT, FigureKind.NUMBER})


def _role_unproven(
    value: object, span_figures: list[Figure], kind: FigureKind | None, currency: str | None
) -> bool:
    """Two or more span figures of the value's kind, and the value is only some of them."""
    text = value_text(value)
    if text is None:  # a non-finite number: the value leg has already failed
        return False
    wanted = figures(text)
    if not wanted:
        return False
    allowed: set[FigureKind] = set()
    for w in wanted:
        if kind is not None:
            allowed.add(kind)
        elif w.kind in (FigureKind.NUMBER, FigureKind.AMBIGUOUS):
            allowed |= _NUMERIC_KINDS  # an untyped number may be any of them
        else:
            allowed.add(w.kind)

    def peer(p: Figure) -> bool:
        if p.kind is FigureKind.AMBIGUOUS:
            # A range limb ("$5-6m"): a peer of money when its currency may match.
            return FigureKind.MONEY in allowed and p.currency in (None, currency)
        if p.kind not in allowed:
            return False
        return currency is None or p.kind is not FigureKind.MONEY or p.currency == currency

    peers = [p for p in span_figures if peer(p)]
    return len(peers) >= 2 and [w.value for w in wanted] != [p.value for p in peers]


def _clause_start(page_text: str, pos: int, floor: int) -> int:
    start = floor
    for m in _CLAUSE_END.finditer(page_text, floor, pos):
        # A dangling cue carries over only a break the value follows directly
        # ("not be: $X"), not one that opens a new clause ("not relevant; the amount is $X").
        # A GST basis phrase between the break and the value ("not:\n\nplus GST $X")
        # is the value's qualifier, so the value still follows the break (fix round 4).
        between = _GST_PHRASE.sub(" ", page_text[m.end() : pos])
        value_follows = not between.strip(" \t\r\n([\"'‘“")
        if not (value_follows and _dangling(page_text[start : m.start()])):
            start = m.end()
    return start


def _dangling(clause: str) -> bool:
    """Does ``clause`` end on a cue, with at most _DANGLING_WORDS words after it?"""
    unbound = _BOUND_PHRASE.sub(" ", _GST_PHRASE.sub(" ", clause))
    cues = list(_NEGATION_CUE.finditer(unbound))
    return bool(cues) and len(_WORD_RUN.findall(unbound, cues[-1].end())) <= _DANGLING_WORDS


def _clause_negation(clause: str) -> tuple[bool, bool]:
    """(a negation cue is in ``clause``, a bound phrase was masked out of it)."""
    masked = _GST_PHRASE.sub(" ", clause)
    unbound = _BOUND_PHRASE.sub(" ", masked)
    return _NEGATION_CUE.search(unbound) is not None, unbound != masked


@dataclass(frozen=True)
class _Position:
    """Where the value is written inside the span."""

    start: int
    end: int
    # The value there is a percent figure: only then may "BBSY plus" be a margin (ruling (f)).
    percent: bool


def _value_positions(
    value: object, page_text: str, start: int, end: int, span_figures: list[Figure]
) -> list[_Position]:
    """Where the value is inside the span: as written text, else by its figures."""
    if isinstance(value, bool):
        return []
    text = value_text(value)
    if text is None:  # a non-finite number has no position: the whole span is searched
        return []
    wanted = figures(text)
    if isinstance(value, str):
        words = text.split()
        pattern = re.compile(r"(?i)(?<!\w)" + r"\s+".join(map(re.escape, words)) + r"(?!\w)")
        percent = bool(wanted) and all(w.kind is FigureKind.PERCENT for w in wanted)
        found = [_Position(m.start(), m.end(), percent) for m in pattern.finditer(page_text, start, end)]
        if found:
            return found
    values = {w.value for w in wanted}
    return [
        _Position(p.start, p.end, p.kind is FigureKind.PERCENT) for p in span_figures if p.value in values
    ]


def _mask_after(text: str) -> str:
    """Blank out what is not a negation of the value: GST basis predicates ("not
    subject to GST", "not including GST") and bound phrases ("must not be
    exceeded", "not to exceed"). Same length, so offsets are kept."""

    def blank(m: re.Match[str]) -> str:
        return " " * len(m.group())

    return _BOUND_PHRASE.sub(blank, _GST_PHRASE.sub(blank, _GST_NOT_SUBJECT.sub(blank, text)))


def _negated_adjacent(page_text: str, end: int) -> bool:
    """A negation or rejection phrase ADJACENT to the value that ends at ``end``.

    Adjacent: after at most _AFTER_MAX_FILLERS filler words (auxiliaries, a few
    adverbs, "which"/"that"/"and") - "$X is, however, not approved", "$X, which
    was declined" - or straight after ":", ";" or a dash ("$X: NOT APPROVED",
    "$X; declined"), or inside a bracket opened after the value, where a ":" or
    ";" starts afresh ("$X (status: not approved)", "$X (see note 3; not
    approved)"). Any other word - a subject, a noun - ends the look outside a
    bracket; it is the semantic leg's question (Plan C Task 12), not this one's.
    The look stops at the end of the sentence.
    """
    limit = min(len(page_text), end + _AFTER_REACH)
    stop = _SENTENCE_END.search(page_text, end, limit)
    window = _mask_after(page_text[end : stop.start() if stop is not None else limit])
    depth = fillers = inner_fillers = 0
    inner_blocked = False
    for tok in _AFTER_TOKEN.finditer(window):
        t = tok.group()
        if t in "([":
            depth, inner_fillers, inner_blocked = depth + 1, 0, False
            continue
        if t in ")]":
            depth = max(0, depth - 1)
            continue
        if t in ":;\u2014\u2013":
            if depth:
                inner_fillers, inner_blocked = 0, False
            else:
                fillers = 0
            continue
        if depth and inner_blocked:
            continue
        if _AFTER_PHRASE.match(window, tok.start()) is not None:
            return True
        filler = t.lower() in _AFTER_FILLERS
        if depth:
            inner_fillers += 1
            inner_blocked = not filler or inner_fillers > _AFTER_MAX_FILLERS
        else:
            fillers += 1
            if not filler or fillers > _AFTER_MAX_FILLERS:
                return False
    return False


def _negation(
    value: object, page_text: str, start: int, end: int, span_figures: list[Figure]
) -> tuple[bool, bool, bool]:
    """(negated, stated as a bound, an arithmetic operator precedes it)."""
    # Fix round 5: every apostrophe-like character reads as "'" (one for one).
    page_text = page_text.translate(_APOSTROPHES)
    floor = max(0, start - _CLAUSE_REACH)
    positions = _value_positions(value, page_text, start, end, span_figures)
    if not positions:
        # No position (a bool, or text not found as written): the clause the span
        # starts in, and a negation or rejection phrase anywhere in the span.
        negated, _ = _clause_negation(page_text[_clause_start(page_text, start, floor) : end])
        anywhere = _AFTER_PHRASE.search(_mask_after(page_text[start:end])) is not None
        return negated or anywhere, False, False
    negated = bound = arithmetic = False
    for position in positions:
        pos = position.start
        n, b = _clause_negation(page_text[_clause_start(page_text, pos, floor) : pos])
        # The look after the value starts at the value's END (fix round 4, ruling (e)).
        negated, bound = negated or n or _negated_adjacent(page_text, position.end), bound or b
        window = page_text[max(0, pos - _ARITHMETIC_REACH) : pos]
        margin = page_text[max(0, pos - _MARGIN_REACH) : pos]
        # "BBSY plus" is a margin only before a percent value (fix round 4, ruling (f)).
        is_margin = position.percent and _MARGIN_PLUS.search(margin) is not None
        arithmetic = arithmetic or (_ARITHMETIC_BEFORE.search(window) is not None and not is_margin)
    return negated, bound, arithmetic


# The support check's gate (not a verdict): a wider, looser cue list than NEGATED.
# A false hit only costs one model call.
_LOOSE_CUE = re.compile(
    r"(?i)(?<!\w)(?:lack\w*|invalid\w*|unenforceable|pending|refus\w*|den(?:y|ied|ial)|reject\w*|declin\w*"
    r"|withdr[ae]w\w*|cancel\w*|terminat\w*|laps\w*|void\w*|revok\w*|rescind\w*|supersed\w*|replac\w*"
    r"|vari(?:ed|ation)|amend\w*|reduc\w*|instead|formerly|previously|original\w*|request\w*|propos\w*"
    r"|sought|unapproved|disapprov\w*|subject\s+to\s+approval)(?!\w)"
)
_CUE_REACH: Final = 200
_CONTEXT_REACH: Final = 400


def _negation_nearby(page_text: str, start: int, end: int) -> bool:
    """A negation cue near the span: the support check's gate, not a flag."""
    text = page_text.translate(_APOSTROPHES)
    window = text[max(0, start - _CUE_REACH) : end + _CUE_REACH]
    unbound = _BOUND_PHRASE.sub(" ", _GST_PHRASE.sub(" ", window))
    return any(p.search(unbound) is not None for p in (_NEGATION_CUE, _AFTER_PHRASE, _LOOSE_CUE))


def _is_money_fact(value: object, kind: FigureKind | None) -> bool:
    if kind is FigureKind.MONEY:
        return True
    if kind is not None or isinstance(value, bool):
        return False
    text = value_text(value)
    return text is not None and any(f.kind is FigureKind.MONEY for f in figures(text))


def _resolve_by_citation(
    quote: str,
    pages: dict[int, str],
    anchor: AnchorResult,
    cited_page: int | None,
    min_words: int = MIN_QUOTE_WORDS,
) -> AnchorResult | None:
    """An AMBIGUOUS anchor re-anchored on the cited page alone, when that page tied."""
    if cited_page is None or cited_page not in anchor.candidates or cited_page not in pages:
        return None
    single = find_quote(quote, {cited_page: pages[cited_page]}, min_words=min_words)
    if single.status in (AnchorStatus.EXACT, AnchorStatus.FUZZY):
        return single
    return None


def _table_value_reason(
    table: ColumnCheck,
    value: object,
    page_text: str,
    start: int,
    end: int,
    currency: str | None,
    deal: str | None,
) -> Reason | None:
    """The value leg for a table cell whose column is known (kind MONEY).

    The column check replaces the span-wide value check: WRONG_COLUMN or
    VALUE_NOT_IN_SPAN unless the value is the cell of its column. A cell's money
    kind and currency come from the cell ("$2,000,000") or, for a bare cell, from
    its header ("$", "$'000", "A$m"); neither is KIND_UNPROVEN (C1). A bare "$"
    reads as the deal currency, as elsewhere; a foreign currency indicator on the
    header or on the cell's line is CURRENCY_MISMATCH.
    """
    if table.outcome is ColumnOutcome.WRONG_COLUMN:
        return Reason.WRONG_COLUMN
    if table.outcome is not ColumnOutcome.MATCH:
        return Reason.VALUE_NOT_IN_SPAN
    cell = table.cell
    header = table.header_currency
    header_ccy = deal if header == "$" else header
    if cell.kind is FigureKind.MONEY:
        bare = _bare(page_text, cell)
        cell_ccy = _effective_currency(page_text, cell, deal)
        if header is not None and header_ccy != cell_ccy:
            return Reason.CURRENCY_MISMATCH if header_ccy is not None else Reason.CURRENCY_UNPROVEN
    elif header is not None:
        bare, cell_ccy = header == "$", header_ccy
    else:
        return Reason.KIND_UNPROVEN
    if cell_ccy is None:
        return Reason.CURRENCY_UNPROVEN
    text = value_text(value)
    if text is None:
        return Reason.VALUE_NOT_IN_SPAN
    for w in figures(text):
        if w.kind is FigureKind.MONEY:
            own = _effective_currency(text, w, deal)
            if own is None:
                return Reason.CURRENCY_UNPROVEN
            if own != cell_ccy:
                return Reason.CURRENCY_MISMATCH
    if currency is not None and cell_ccy != currency:
        return Reason.CURRENCY_MISMATCH
    if bare:
        hs, he = table.header_span
        region = page_text[hs:he] + "\n" + _figure_region(page_text, start, end, cell)
        if _foreign_on_line(region, deal):
            return Reason.CURRENCY_MISMATCH
    return None


def verify_fact(
    *,
    value: object,
    quote: str,
    pages: dict[int, str],
    kind: FigureKind | str | Collection[FigureKind | str] | None = None,
    # The devel_jason line's explicit flag. Their rule is automatic — a bool
    # cannot appear in its own quote — but a SUMMARY cannot either, and only
    # the caller knows it asked for one. The two AND together below.
    check_value: bool = True,
    currency: str | None = None,
    deal_currency: object = _DEAL_NOT_GIVEN,
    cited_page: int | None = None,
    second_read: object = _NOT_READ,
    field: str | None = None,
    vision_pages: Collection[int] = frozenset(),
    material: bool = False,
    support_checker: SupportChecker | None = None,
) -> FactVerification:
    """Verify one extracted fact against the document's pages (module docstring).

    ``kind`` is required: the caller passes the field's declared unit (money,
    percent, date, number), or None for an untyped value such as a name.
    ``currency`` (a code as ``values.Figure.currency`` spells it: AUD, USD, NZD,
    EUR, GBP, JPY, ...) is required for kind MONEY and refused for other kinds.
    ``deal_currency`` (fix round 5) is the analyst-confirmed deal currency, or
    None while it is unconfirmed; it must be passed when kind is MONEY. A bare "$"
    reads as it. Not passed for an untyped value, it counts as unconfirmed.
    ``field`` is the fact's citation path; for a table cell
    (``...[<key>].senior|equity|total|mezzanine``, kind MONEY) it lets the table
    layer prove the value's column (module docstring, "Table rows").
    ``vision_pages`` are the pages whose text is a vision transcription; a fact
    anchored on one is capped below the review threshold (module docstring,
    "Vision text").
    ``support_checker`` and ``material`` drive the semantic support check
    (module docstring, "Semantic support"). A checker's transport error
    propagates.
    ``kind`` may be a set of accepted kinds (a concept whose written unit varies, e.g.
    a covenant threshold: percent or number). The value is verified as each kind in
    turn and the first VERIFIED one is kept; the value's own digits must still match,
    so "65%" is 65, never 0.65. The support check runs once, on the kind chosen.
    """
    if kind is not None and not isinstance(kind, str):
        kinds = tuple(dict.fromkeys(FigureKind(k) for k in kind))
        if not kinds:
            raise ValueError("kind: an empty set of accepted kinds")
        if len(kinds) > 1 and FigureKind.MONEY in kinds:
            raise ValueError("money carries currency rules and cannot share an accepted-kinds set")
        args = {
            "value": value,
            "quote": quote,
            "pages": pages,
            "check_value": check_value,
            "currency": currency,
            "deal_currency": deal_currency,
            "cited_page": cited_page,
            "second_read": second_read,
            "field": field,
            "vision_pages": vision_pages,
            "material": material,
        }
        if len(kinds) == 1:
            return verify_fact(kind=kinds[0], support_checker=support_checker, **args)  # type: ignore[arg-type]
        runs = [verify_fact(kind=k, **args) for k in kinds]  # type: ignore[arg-type]
        chosen = next((i for i, r in enumerate(runs) if r.verdict is Verdict.VERIFIED), 0)
        if support_checker is None:
            return runs[chosen]
        return verify_fact(kind=kinds[chosen], support_checker=support_checker, **args)  # type: ignore[arg-type]
    wanted_kind = None if kind is None else FigureKind(kind)
    if wanted_kind is FigureKind.MONEY and currency is None:
        raise ValueError("currency is required when kind is MONEY")
    if wanted_kind is FigureKind.MONEY and deal_currency is _DEAL_NOT_GIVEN:
        raise ValueError(
            "deal_currency is required when kind is MONEY: the analyst-confirmed deal currency, "
            "or None while it is unconfirmed"
        )
    if deal_currency is _DEAL_NOT_GIVEN or deal_currency is None:
        deal: str | None = None
    elif isinstance(deal_currency, str):
        deal = deal_currency
    else:
        raise TypeError(f"deal_currency must be a currency code or None, not {deal_currency!r}")
    if currency is not None and wanted_kind not in (None, FigureKind.MONEY):
        raise ValueError(f"currency applies to kind MONEY or an untyped value, not {wanted_kind}")
    anchor = find_quote(quote, pages)
    # A table row shorter than the anchor's minimum (tables.py): anchored with a
    # lower minimum, and kept only if EXACT and its column is proven below.
    row_anchor: AnchorResult | None = None
    if (
        anchor.status is AnchorStatus.TOO_SHORT
        and wanted_kind is FigureKind.MONEY
        and field_column(field) is not None
        and is_row_quote(quote)
    ):
        row_anchor = find_quote(quote, pages, min_words=MIN_ROW_WORDS)
    agreement = None if second_read is _NOT_READ else _agrees(value, second_read)
    reasons: list[Reason] = []
    if agreement is False:
        reasons.append(Reason.SECOND_READ_DISAGREES)

    def early(verdict: Verdict, confidence: float, reason: Reason) -> FactVerification:
        return FactVerification(
            verdict=verdict,
            confidence=confidence,
            anchor=anchor,
            currency=currency,
            deal_currency=deal,
            agreement=agreement,
            value_checked=False,
            reasons=(reason, *reasons),
        )

    if _blank(value):
        return early(Verdict.FLAGGED_VALUE, BLANK_VALUE_CONFIDENCE, Reason.BLANK_VALUE)

    def refused() -> FactVerification:
        return early(Verdict.REFUSED_SHORT_QUOTE, REFUSED_CONFIDENCE, Reason.QUOTE_TOO_SHORT)

    if anchor.status is AnchorStatus.TOO_SHORT and row_anchor is None:
        return refused()

    # A short row that is not proven below is refused exactly as before.
    searched = anchor if row_anchor is None else row_anchor
    located = searched
    resolved: AnchorResult | None = None
    if searched.status is AnchorStatus.AMBIGUOUS:
        min_words = MIN_QUOTE_WORDS if row_anchor is None else MIN_ROW_WORDS
        resolved = _resolve_by_citation(quote, pages, searched, cited_page, min_words)
        if resolved is None:
            if row_anchor is not None:
                return refused()
            return early(Verdict.FLAGGED_AMBIGUOUS, FLAGGED_AMBIGUOUS_CONFIDENCE, Reason.AMBIGUOUS_ANCHOR)
        located = resolved
        reasons.append(Reason.RESOLVED_BY_CITATION)

    if located.status not in (AnchorStatus.EXACT, AnchorStatus.FUZZY):
        if row_anchor is not None:
            return refused()
        return early(Verdict.FLAGGED_ANCHOR, FLAGGED_ANCHOR_CONFIDENCE, Reason.QUOTE_NOT_FOUND)
    if row_anchor is not None and located.status is not AnchorStatus.EXACT:
        return refused()
    # find_quote gives every EXACT/FUZZY result a page and a span.
    assert located.page is not None and located.span is not None, located

    page = located.page
    start, end = located.span
    page_text = pages[page]
    span_figures = [f for f in figures(page_text) if start <= f.start and f.end <= end]

    # N2: the quote's figures, in order, are the span's figures in page context.
    if [_key(f) for f in figures(quote)] != [_key(f) for f in span_figures]:
        reasons.append(Reason.FIGURES_DIFFER)

    # Table rows: the value's column, proven against the row's header (tables.py).
    table: ColumnCheck | None = None
    if wanted_kind is FigureKind.MONEY and field is not None:
        table = check_column(value, page_text, start, end, field=field)
    if row_anchor is not None and (table is None or Reason.FIGURES_DIFFER in reasons):
        return refused()  # a short row needs its column and its exact figures

    # A bool never appears in its own quote ("true" is the extractor's reading
    # of "the licence is current"); nor does a SUMMARY, which is a paraphrase
    # of the sentence it came from and which only the caller can flag.
    value_checked = check_value and not isinstance(value, bool)
    if value_checked:
        if table is not None:
            reason = _table_value_reason(table, value, page_text, start, end, currency, deal)
        else:
            reason = _value_reason(value, page_text, start, end, span_figures, wanted_kind, currency, deal)
        if reason is not None:
            reasons.append(reason)
    else:
        reasons.append(Reason.VALUE_NOT_CHECKED)

    negated, bound, arithmetic = _negation(value, page_text, start, end, span_figures)
    if negated:
        reasons.append(Reason.NEGATED)
    elif bound:
        reasons.append(Reason.BOUND_PHRASE)
    if arithmetic:
        reasons.append(Reason.ARITHMETIC)
    if table is not None:
        if table.outcome is ColumnOutcome.MATCH:
            reasons.append(Reason.COLUMN_PROVEN)  # the header answers the role question
    elif value_checked and _role_unproven(value, span_figures, wanted_kind, currency):
        reasons.append(Reason.ROLE_UNPROVEN)

    span_gst = GstBasis.UNSTATED
    if wanted_kind in (None, FigureKind.MONEY):
        span_gst = gst_basis(page_text[start:end])
        value_gst = gst_basis(value) if isinstance(value, str) else GstBasis.UNSTATED
        if GstBasis.MIXED in (span_gst, value_gst):
            reasons.append(Reason.GST_MIXED)
        elif value_gst is not GstBasis.UNSTATED and value_gst is not span_gst:
            reasons.append(Reason.GST_MISMATCH)

    # A foreign currency mentioned elsewhere in the document is a warning on a
    # money fact, never a flag (fix round 5).
    warnings: tuple[FactWarning, ...] = ()
    if _is_money_fact(value, wanted_kind) and _foreign_mentioned(pages):
        warnings = (FactWarning.FOREIGN_CURRENCY_MENTIONED,)

    confidence = EXACT_CONFIDENCE if located.status is AnchorStatus.EXACT else FUZZY_CONFIDENCE
    if resolved is not None:
        confidence -= CITED_TIE_PENALTY
    elif cited_page is not None and page != cited_page:
        confidence -= CITED_PAGE_MISMATCH_PENALTY
        reasons.append(Reason.CITED_PAGE_MISMATCH)

    if page in vision_pages:
        reasons.append(Reason.VISION_TEXT)

    found = set(reasons)
    if found & _VALUE_LEG_FAILURES:
        verdict, confidence = Verdict.FLAGGED_VALUE, FLAGGED_VALUE_CONFIDENCE
    elif Reason.SECOND_READ_DISAGREES in found:
        verdict, confidence = Verdict.FLAGGED_VALUE, SECOND_READ_DISAGREEMENT_CONFIDENCE
    elif Reason.GST_MIXED in found:
        verdict, confidence = Verdict.FLAGGED_GST, FLAGGED_GST_CONFIDENCE
    else:
        verdict = Verdict.VERIFIED if value_checked else Verdict.ANCHORED
        if agreement is True:
            confidence += SECOND_READ_AGREEMENT_BONUS
        confidence = min(confidence, CONFIDENCE_CAP)
        if not value_checked:
            confidence = min(confidence, ANCHOR_ONLY_CAP)
        before_role = confidence
        if Reason.ROLE_UNPROVEN in found:
            verdict, confidence = Verdict.FLAGGED_ROLE, min(confidence, ROLE_UNPROVEN_CAP)

    # The third leg (module docstring, "Semantic support"). It never sees a fact
    # a deterministic leg failed: those verdicts are not in the tuple below.
    support: SupportOutcome | None = None
    if (
        support_checker is not None
        and verdict in (Verdict.VERIFIED, Verdict.ANCHORED, Verdict.FLAGGED_ROLE)
        and (material or verdict is Verdict.FLAGGED_ROLE or _negation_nearby(page_text, start, end))
    ):
        lo, hi = max(0, start - _CONTEXT_REACH), min(len(page_text), end + _CONTEXT_REACH)
        support = support_checker(value=value, span_text=page_text[start:end], page_context=page_text[lo:hi])
        if support.supports == "no":
            reasons.append(_UNSUPPORTED[support.reason_code or SupportReason.OTHER])
            verdict, confidence = Verdict.FLAGGED_VALUE, FLAGGED_VALUE_CONFIDENCE
        elif support.supports == "unclear" or support.confidence < SUPPORT_CONFIDENCE_FLOOR:
            reasons.append(Reason.SUPPORT_UNCLEAR)
            confidence = min(confidence, SUPPORT_UNCLEAR_CAP)
        elif verdict is Verdict.FLAGGED_ROLE and support.role:
            reasons.append(Reason.ROLE_SUPPORTED)
            verdict, confidence = Verdict.VERIFIED, before_role
        found = set(reasons)

    if Reason.VISION_TEXT in found:
        confidence = min(confidence, VISION_TEXT_CAP)

    return FactVerification(
        verdict=verdict,
        confidence=round(confidence, _DECIMALS),
        page=page,
        span=(start, end),
        anchor=anchor if row_anchor is None else row_anchor,
        resolved_anchor=resolved,
        gst=span_gst,
        currency=currency,
        deal_currency=deal,
        agreement=agreement,
        value_checked=value_checked,
        reasons=tuple(reasons),
        warnings=warnings,
        support=support,
    )


def verify_located(
    *,
    value: object,
    page_text: str,
    page: int,
    span: tuple[int, int],
    kind: FigureKind | str | None = None,
    check_value: bool = True,
    support_checker: SupportChecker | None = None,
    material: bool = False,
    ocr_text: bool = False,
) -> FactVerification:
    """Verify a fact whose location is ALREADY KNOWN — no searching.

    `verify_fact` exists because a model-supplied quote has to be FOUND on the
    page, and finding fails three ways: too short to be distinctive, not found
    at all, or found in two places within `AMBIGUITY_MARGIN`. Measured on a real
    corpus those were 8.5%, 6.7% and 14.1% of facts.

    None of them apply to a LINE-ANCHORED fact. `ingest/corpus.clause_at` cuts
    the clause out of the page at known offsets, so the "quote" IS the page.
    Searching for it would hand `find_quote` a string taken from the very text
    it searches, and the ambiguity margin would refuse whenever a schedule row
    repeats — even though the cited line says which occurrence was meant.

    So the anchor is ASSERTED rather than discovered. Measured 2026-09-23 that
    took verified from 79.5% to 92.6%, with every fact anchoring and
    `flagged_ambiguous` reaching zero.

    The page text and raw offsets are taken, not the cut span, because
    `value_in_page_span` reads figures from the WHOLE page and keeps those
    lying inside the span — "Lot 5 June 2027" cut to "5 June 2027" reads as a
    date that the page does not state.

    `check_value=False` is the bool or summary case and reaches ANCHORED
    rather than VERIFIED: the quote is located, the value was not and could not
    be checked against it.

    `support_checker` adds the semantic leg, gated by `material` exactly as in
    `verify_fact` — it costs a model call per fact, so the caller decides which
    facts are worth one. For the corpus path that decision is the subtree
    signal: a fact landing outside the blocks its document primarily feeds is
    where misassignment concentrates.
    """
    start, end = span
    text = page_text[start:end]
    anchor = AnchorResult(
        status=AnchorStatus.EXACT,
        page=page,
        span=span,
        score=1.0,
        matched_text=text,
    )
    basis = gst_basis(text)
    # An OCR'd page is what the fact stood on whatever the verdict, so the
    # reason is recorded on every path out of here - including the two that
    # return early. On the flagged path the cap is already a no-op (0.30 is
    # below it); the reason is not, because it says WHY the value could not be
    # found: the text is a reading of the page.
    ocr_reasons: tuple[Reason, ...] = (Reason.OCR_TEXT,) if ocr_text else ()
    if not check_value:
        return FactVerification(
            verdict=Verdict.ANCHORED,
            confidence=min(0.85, VISION_TEXT_CAP) if ocr_text else 0.85,
            page=page,
            span=span,
            anchor=anchor,
            gst=basis,
            value_checked=False,
            reasons=ocr_reasons,
        )
    if not value_in_page_span(value, page_text, start, end, kind=kind):
        return FactVerification(
            verdict=Verdict.FLAGGED_VALUE,
            confidence=0.30,
            page=page,
            span=span,
            anchor=anchor,
            gst=basis,
            value_checked=True,
            reasons=(Reason.VALUE_NOT_IN_SPAN, *ocr_reasons),
        )

    # The third leg, under the SAME rules `verify_fact` applies: a model never
    # sees a fact a deterministic leg failed (both failing verdicts returned
    # above), and it can never lift one.
    #
    # It is what the deterministic legs structurally cannot do here.
    # `value_in_page_span` proves the value is stated inside the span; it says
    # nothing about whether the span states it OF THIS FIELD. That is the error
    # general extraction uniquely creates — a cost-report figure landing in an
    # ITS field verifies at 0.95, because the figure really is in the quote —
    # and measured across three deal folders on 2026-09-23 it was the one thing
    # no metric in this module could see.
    verdict, confidence = Verdict.VERIFIED, 0.95
    reasons: list[Reason] = []
    support: SupportOutcome | None = None
    # Recorded FIRST so it survives whatever the support leg decides; capped at
    # the end so a support cap can still go lower.
    reasons.extend(ocr_reasons)
    if support_checker is None and material:
        # Triaged as worth a model call, and no checker exists for it. The
        # concept registry covers 40% of the schema (measured 2026-09-24), so
        # this is the common case, not an edge one — and without this line a
        # fact nothing could semantically check reaches VERIFIED at 0.95
        # looking exactly like one that passed. The verdict is unchanged: the
        # deterministic legs did pass. What changes is that the record no
        # longer claims a check that never ran.
        reasons.append(Reason.SUPPORT_UNAVAILABLE)
    if support_checker is not None and (material or _negation_nearby(page_text, start, end)):
        lo, hi = max(0, start - _CONTEXT_REACH), min(len(page_text), end + _CONTEXT_REACH)
        support = support_checker(value=value, span_text=text, page_context=page_text[lo:hi])
        if support.supports == "no":
            reasons.append(_UNSUPPORTED[support.reason_code or SupportReason.OTHER])
            verdict, confidence = Verdict.FLAGGED_VALUE, FLAGGED_VALUE_CONFIDENCE
        elif support.supports == "unclear" or support.confidence < SUPPORT_CONFIDENCE_FLOOR:
            reasons.append(Reason.SUPPORT_UNCLEAR)
            confidence = min(confidence, SUPPORT_UNCLEAR_CAP)

    if ocr_text:
        confidence = min(confidence, VISION_TEXT_CAP)

    return FactVerification(
        verdict=verdict,
        confidence=confidence,
        page=page,
        span=span,
        anchor=anchor,
        gst=basis,
        value_checked=True,
        reasons=tuple(reasons),
        support=support,
    )
