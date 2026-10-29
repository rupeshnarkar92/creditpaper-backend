"""The citation-path grammar — published, so both sides validate identically.

The GRAMMAR crosses the seam; RESOLUTION does not. A platform screen needs to
check that a path is well-formed before sending it, and needs to build keys the
same way the engine does. It has no business walking a `DealModel`, so
`resolve_path`, `invalid_citations` and `disclosure_citations` stay in
`model/citations.py` — they are engine internals that happen to share a subject.

Splitting on that line is deliberate: the grammar is a promise to a consumer, the
resolver is an implementation detail that will change when findings replace prose
blocks.
"""

from __future__ import annotations

import hashlib
import re

_SEGMENT = r"[a-z][a-z0-9_]*(\[[a-z][a-z0-9_]*\])?"
PATH_GRAMMAR = re.compile(rf"^{_SEGMENT}(\.{_SEGMENT})*$")


def is_valid_path(path: str) -> bool:
    """True if the path matches the citation-path grammar (syntax only —
    says nothing about whether it resolves against a model)."""
    return bool(PATH_GRAMMAR.match(path))


def slugify(label: str) -> str:
    """The one way collection keys are made: 'J. Smith' -> 'j_smith',
    'Establishment Fee (2%)' -> 'establishment_fee_2'. Lowercase snake_case,
    leading digits prefixed so the key satisfies the grammar."""
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        raise ValueError(f"label {label!r} produces an empty key")
    if slug[0].isdigit():
        slug = f"k_{slug}"
    return slug


def safe_slug(label: str) -> str:
    """`slugify`, but total: never raises, for use at extraction time.

    `slugify` raises on a label that reduces to nothing — correct as a contract,
    fatal as a call. Extractors build keyed paths inside loops with no guard, so
    one label made of punctuation (a guarantor recorded as "(ACN 123 456 789)
    Smith Pty Ltd", from which the ITS extractor takes the part before the
    bracket) raised out of `extract()` and discarded EVERY fact from that
    document — the whole ITS lost to one odd string.

    The fallback is a deterministic hash of the label, so the same label yields
    the same key on every run and the fact keeps its value and its anchor.
    Discarding the item instead would lose a party to the deal in order to avoid
    an ugly key; the key is an internal addressing detail, the value is what the
    paper renders. `unnamed` is in the key so a reader of a provenance path can
    see that the source label was unusable rather than wondering what it meant.
    """
    try:
        return slugify(label)
    except ValueError:
        digest = hashlib.sha1(label.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        return f"k_unnamed_{digest}"
