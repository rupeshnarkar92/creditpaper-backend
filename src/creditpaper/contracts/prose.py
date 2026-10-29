"""Section prose as the platform sees it, and the D8 guard that governs it.

`ProseBlock` crosses the seam: the review surface renders it, edits it, and posts
signals against it. `Fact` and `Provenance` do NOT — they are how the engine
establishes what it knows, and the platform never constructs one.

That split is the useful line. A platform developer needs to know a block has
text, a status, an authorship and cited paths. They do not need to know that a
Fact cannot exist without an anchor, because they will never make one.

D8 lives here rather than in the engine because it is a promise to the reader, not
an implementation choice: the Section 9 credit recommendation is human-authored,
and a block whose drafted body states one cannot reach APPROVED.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import model_validator

from creditpaper.contracts.base import DealBase
from creditpaper.markup import is_scaffolding


class SectionStatus(str, Enum):
    """Lifecycle of a per-section prose block."""

    DRAFT = "draft"
    EVIDENCE_INCOMPLETE = "evidence-incomplete"
    REGENERATED_SINCE_EDIT = "regenerated-since-edit"
    HUMAN_EDITED = "human-edited"
    APPROVED = "approved"


class Authorship(str, Enum):
    """Who wrote this, as a reader needs to know it (DECISIONS.md 2026-08-21).

    ORTHOGONAL to provenance, and the distinction is easy to get wrong. A finding
    can be AI-authored AND fully cited: provenance answers "what is this claim
    based on", authorship answers "who composed the sentence". Marking prose
    AI_GENERATED is not a weaker citation claim, it is a different axis, and
    conflating them would either suppress honest marking or imply that
    human-written text needs no anchor.

    Distinct from `SectionStatus` too, which tracks a workflow position — a block
    can be APPROVED and still be AI-authored, which is precisely the case a reader
    most needs marked.
    """

    DETERMINISTIC = "deterministic"  # rendered from anchored facts; no model wrote it
    AI_GENERATED = "ai_generated"
    HUMAN_EDITED = "human_edited"  # AI draft a human has since corrected in place
    HUMAN_AUTHORED = "human_authored"  # written by a person from scratch (D8's §9 recommendation)


# D8, detection half. The prose that RENDERS ABOVE the human recommendation must
# not itself contain one — an agent writing "we recommend approval" put a
# machine-authored credit recommendation into the paper while D8 was formally
# satisfied by whatever the human typed into the separate field.
#
# Matches assertive, firm-voice recommendations only. Section 9's job is to stop
# short of the recommendation AND to tell the analyst where to write it, so
# talking *about* the recommendation is not merely allowed, it is required — a
# guard that fired on the scaffold would break the one thing the section must do.
_MACHINE_RECOMMENDATION = re.compile(
    r"\b(?:"
    r"we\s+(?:would\s+|do\s+not\s+|don't\s+)?(?:recommend|support|endorse|decline)\b"
    r"|we\s+are\s+comfortable\s+recommending\b"
    r"|it\s+is\s+recommended\b"
    r"|(?:approval|the\s+facility|this\s+transaction|the\s+transaction)\s+is\s+recommended\b"
    r"|recommend(?:s|ed|ing)?\s+(?:approval|proceeding|that\s+the\s+facility|the\s+facility)\b"
    r")",
    re.IGNORECASE,
)

# Scaffold and analyst-action lines, via the shared grammar. These were literals
# here because the model layer must not depend on the generation layer, which was
# the right instinct and the wrong fix — `creditpaper.markup` is now the neutral
# home for the wire format, so the guard and the parsers cannot disagree about
# what a scaffold line is.


def machine_recommendations(text: str) -> list[str]:
    """Sentences in `text` that state a credit recommendation in the firm's voice.

    Empty for scaffold lines and for prose that merely refers to the
    recommendation as something the analyst will author.
    """
    found: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if is_scaffolding(stripped):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", stripped):
            if _MACHINE_RECOMMENDATION.search(sentence):
                found.append(sentence.strip())
    return found


class ProseBlock(DealBase):
    section: int
    text: str = ""
    status: SectionStatus = SectionStatus.DRAFT
    # Who wrote this, for the reader. Defaults to AI_GENERATED because that is
    # what a prose block is unless something says otherwise, and the honest
    # default is the one that over-marks rather than under-marks: a section
    # wrongly marked AI-generated is a small annoyance, one wrongly presented as
    # human-written is a misrepresentation.
    authorship: Authorship = Authorship.AI_GENERATED
    # D8: section 9 cannot reach APPROVED without a human-authored recommendation.
    human_recommendation: str | None = None
    cited_field_paths: list[str] = []  # dotted paths into the deal model; renderer resolves

    @model_validator(mode="after")
    def _enforce_d8(self) -> ProseBlock:
        if self.section != 9 or self.status != SectionStatus.APPROVED:
            return self
        if not (self.human_recommendation and self.human_recommendation.strip()):
            raise ValueError("Section 9 cannot be approved without a human-authored recommendation (D8)")
        # The body renders directly above the human recommendation, so a machine
        # recommendation there is indistinguishable to a reader from the real one.
        # Checked at APPROVED rather than at construction so a draft can still be
        # produced, held, and shown to a human with the offending sentence named.
        if machine := machine_recommendations(self.text):
            raise ValueError(
                "Section 9 cannot be approved: the drafted body states a credit "
                f"recommendation, which is human-authored (D8) — {machine[0]!r}"
            )
        return self
