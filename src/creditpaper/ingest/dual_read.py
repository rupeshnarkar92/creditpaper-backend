"""Read the figures that matter twice, with two different model families.

Reading twice with the same model repeats the same mistake: the second read
agrees because it shares the first one's blind spots, and agreement then means
nothing. `llm/registry.py` refuses to let SECOND_READ share a provider with
EXTRACT for that reason, and this module is what spends the second read.

Two things earn one. A material figure always does - the numbers a credit
decision turns on are worth the second call. And any field the first read was
unsure about does, material or not, because low confidence is the signal that a
second opinion is cheap relative to being wrong.

Agreement is what confidence is built from, so a formatting difference must not
read as a disagreement: "$12,400,000" and 12400000 are the same answer. The
tokeniser that decides is the one the value-in-span check uses, so the two
cannot disagree about what a number is.

A deployment with no second family configured gets one read and says so.
`resolve_optional` returns None in that case rather than raising, because a
single-family deployment is a choice - while a misspelled provider is still a
mistake and still raises.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel

from creditpaper.ingest.values import numeric_tokens
from creditpaper.ingest.verify import FactVerification
from creditpaper.llm.registry import Task, default_provider, resolve_optional

# The sections a credit decision turns on. A wrong figure here changes the
# recommendation; a wrong contact name does not.
MATERIAL_PREFIXES: tuple[str, ...] = (
    "economics.",
    "its.",
    "covenants.",
    "metrics.",
    "funding_table.",
    "valuation.",
    "check_valuations.",
    "security.",
    "sop.",
    "ato.",
)
SECOND_READ_CONFIDENCE_FLOOR = 0.85
AGREED_CONFIDENCE_BONUS = 0.04
DISAGREED_CONFIDENCE = 0.40


class DualOutcome(str, Enum):
    AGREED = "agreed"
    DISAGREED = "disagreed"
    SINGLE_READ = "single_read"


class DualRead(BaseModel):
    outcome: DualOutcome
    value: Any
    confidence: float
    second_value: Any = None
    providers: tuple[str, str] | None = None


def is_material(path: str) -> bool:
    return path.startswith(MATERIAL_PREFIXES)


def needs_second_read(path: str, verification: FactVerification) -> bool:
    """Material always; anything the first read was unsure about as well."""
    return is_material(path) or verification.confidence < SECOND_READ_CONFIDENCE_FLOOR


def second_read_available() -> bool:
    """Whether this deployment has a second family configured at all."""
    return resolve_optional(Task.SECOND_READ) is not None


def _agrees(first: object, second: object) -> bool:
    a, b = numeric_tokens(str(first)), numeric_tokens(str(second))
    if a and b:
        return a == b
    return str(first).strip().casefold() == str(second).strip().casefold()


def reconcile(
    *,
    path: str,
    value: Any,
    verification: FactVerification,
    second_value: Any = None,
) -> DualRead:
    if second_value is None:
        return DualRead(
            outcome=DualOutcome.SINGLE_READ,
            value=value,
            confidence=verification.confidence,
            second_value=None,
            providers=None,
        )

    # The families, from the default table rather than the configured models:
    # the rule being asserted is that the two differ, which is a property of the
    # table and holds whether or not a model has been named for either.
    providers = (default_provider(Task.EXTRACT), default_provider(Task.SECOND_READ))

    if _agrees(value, second_value):
        return DualRead(
            outcome=DualOutcome.AGREED,
            value=value,
            confidence=min(0.99, verification.confidence + AGREED_CONFIDENCE_BONUS),
            second_value=second_value,
            providers=providers,
        )
    return DualRead(
        outcome=DualOutcome.DISAGREED,
        value=value,
        confidence=DISAGREED_CONFIDENCE,
        second_value=second_value,
        providers=providers,
    )
