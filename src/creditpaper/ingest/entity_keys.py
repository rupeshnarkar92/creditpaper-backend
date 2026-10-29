"""One entity, one collection key — whatever the document called it that time.

`contracts/citations.slugify` makes a key deterministic given the SAME label:
"J. Smith" is always `j_smith`. It does nothing about the same entity arriving
under different labels, and a real deal folder never says a name the same way
twice:

    Example Holdings Pty Ltd  /  Example Holdings  /  EXAMPLE HOLDINGS PTY. LTD.
    /  Example Holdings Pty Ltd ACN 123 456 789  /  the Guarantor

Five labels, five slugs, five entries in `its.guarantors[]` — for one guarantor.

Measured 2026-09-22, three passes over 200 windows: flapping paths were **96%
keyed**, and 264 of 318 keyed paths were unstable against 19 of 33 plain ones.
With anchoring solved and values steady, key drift is what remains.

WHY A CANONICAL FORM AND NOT A REGISTRY
---------------------------------------
The obvious design is a per-deal registry where the first mention wins and
later ones resolve to it. It is also wrong, and wrong in exactly the way being
fixed: the canonical key would depend on ENCOUNTER ORDER, so a run that met
"Example Holdings" first and one that met "Example Holdings Pty Ltd" first
would disagree — two runs, two key spaces, the same instability under a new
name.

So the key is a PURE FUNCTION of the label. Order cannot matter because order
is not an input. Nothing is stored, nothing is shared between windows, and two
runs agree because they compute rather than remember.

WHAT IS DELIBERATELY REFUSED
----------------------------
"the Guarantor", "the Borrower", "the Company" identify nothing. A key of
`the_guarantor` is not a name, it is a role that every deal has, and merging
two deals' facts under it would be worse than leaving them apart.
`canonical_key` returns None for these, so the caller can flag the fact instead
of inventing an identity for it — the same refusal as D4's "no anchor, no
field", applied to keys.
"""

from __future__ import annotations

import re

from creditpaper.contracts.citations import safe_slug

# Australian company suffixes, plus the common international ones a sponsor's
# group structure throws up. Stripped from the END only: "Ltd" inside a name
# ("Ltd Brands Pty Ltd") is part of the name.
_SUFFIXES = (
    "pty ltd",
    "pty limited",
    "proprietary limited",
    "pty",
    "ltd",
    "limited",
    "no liability",
    "nl",
    "incorporated",
    "inc",
    "llc",
    "plc",
    "llp",
    "trustee",
    "as trustee",
)

# A company number quoted inside a name. Australian ACN/ABN are 9 and 11
# digits, usually spaced. They identify the entity precisely, but they are not
# always present, so a key built WITH one would not match the same entity
# mentioned without it.
_COMPANY_NUMBER = re.compile(r"\b(?:a\.?c\.?n\.?|a\.?b\.?n\.?)\.?\s*[\d\s]{9,}", re.I)

# "X as trustee for the Y Trust" — the ENTITY is X; the trust is a separate
# thing with its own key. Dropping the tail keeps "Acme Pty Ltd ATF Smith
# Family Trust" and "Acme Pty Ltd" as one entity, which they are.
_TRUSTEE_TAIL = re.compile(r"\s+(?:as\s+trustee\s+for|atf|a\.t\.f\.?)\b.*$", re.I)

# Labels that name a ROLE, not a party. Refused outright — see the module note.
_ROLE_WORDS = frozenset(
    {
        "borrower",
        "guarantor",
        "guarantors",
        "lender",
        "mortgagor",
        "mortgagee",
        "company",
        "trustee",
        "applicant",
        "builder",
        "vendor",
        "purchaser",
        "valuer",
        "sponsor",
        "party",
        "director",
        "shareholder",
        # The leading article is stripped before this set is consulted, so
        # "the Property" arrives as "property" and the bare forms are the ones
        # that matter. Listing "the property" as well would never match.
        "property",
        "subject property",
        "premises",
        "site",
        "unknown",
        "n a",
        "na",
        "none",
        "tbc",
        "tba",
    }
)

_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.I)
_PUNCT = re.compile(r"[^\w\s]")


def _strip_suffixes(text: str) -> str:
    """Remove trailing legal suffixes, repeatedly.

    Repeatedly because "Pty. Ltd." reduces to "pty ltd" which is one suffix,
    but "Example Holdings Pty Limited Ltd" — which real documents do produce —
    needs two passes.
    """
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -len(suffix) - 1].strip()
                changed = True
    return text


def normalise_entity(label: str) -> str:
    """The comparable form of an entity name. Lowercase, no legal furniture."""
    text = label.strip().casefold()
    text = _COMPANY_NUMBER.sub(" ", text)
    text = _TRUSTEE_TAIL.sub("", text)
    text = _PUNCT.sub(" ", text)
    text = " ".join(text.split())
    text = _LEADING_ARTICLE.sub("", text)
    text = _strip_suffixes(text)
    return " ".join(text.split())


def is_role_word(label: str) -> bool:
    """True when the label names a role rather than a party."""
    return normalise_entity(label) in _ROLE_WORDS


def canonical_key(label: str) -> str | None:
    """The collection key for an entity, or None if the label identifies nobody.

    Pure: the same label always gives the same key, and no other label's
    existence changes it. That is what makes two runs agree without sharing
    state.

    None is a real answer, not a failure to be papered over. "the Guarantor" is
    a role every deal has; keying facts under it would merge parties that have
    nothing to do with each other, which is worse than leaving the fact
    unkeyed and flagged.
    """
    if not label or not label.strip():
        return None
    normalised = normalise_entity(label)
    if not normalised or normalised in _ROLE_WORDS:
        return None
    key = safe_slug(normalised)
    return key or None


def canonicalise_path(path: str) -> str:
    """Rewrite every `[key]` in a citation path to its canonical form.

    A path can carry more than one key — `sponsor.structure.entities[x].roles[y]`
    — and each is canonicalised independently. A key that canonicalises to
    nothing is left exactly as it was: this function's job is to MERGE
    duplicates, and silently dropping a key would change which field a fact
    lands on.
    """

    def replace(match: re.Match[str]) -> str:
        key = canonical_key(match.group(1))
        return f"[{key}]" if key else match.group(0)

    return re.sub(r"\[([^\]]*)\]", replace, path)
