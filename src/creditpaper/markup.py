"""The line grammar agents write and everything downstream reads.

This is a WIRE FORMAT, not prompt text. The agents emit it, the docx renderer
parses it, the review API parses it, and the D8 guard has to know which lines are
scaffold. It lived in `agents/constitution.py` — a module of prompt strings — and
was re-parsed ad hoc in four places, each with its own `.strip()` on the constant
and its own idea of which line kinds exist.

The cost of that was two parsers for one grammar. `renderer/docx_render.py`
handled `### ` subheadings, markdown pipe tables and Section 7's
`Assessment:`/`Mitigant:` fields; `api/prose_blocks.py` handled none of them and
dropped each into `paragraph`, so the review editor showed a reviewer raw `### `
and `|---|` markup and offered no structure for the risk table. Same text, two
answers, and no way to notice.

It is also the root cause of the historical `>>`/`[ANALYST]` leak class: a parser
that does not know a prefix folds it into the previous cell as raw markup.

One definition, one classifier. A consumer may ignore kinds it does not render,
but it can no longer disagree about what a line IS.
"""

from __future__ import annotations

from enum import Enum

# Written with the trailing space the agents actually emit. Match on the stripped
# form — a line is scaffold whether or not the model left the space in.
SCAFFOLD_PREFIX = ">> "
ANALYST_PREFIX = "[ANALYST] "
HEADING_PREFIX = "## "
SUBHEADING_PREFIX = "### "
TABLE_PREFIX = "|"


class LineKind(str, Enum):
    """What a line of generated prose is.

    `SUBHEADING` and `TABLE_ROW` exist because the renderer already treated them
    as distinct while the API did not — the divergence this module removes.
    """

    BLANK = "blank"
    HEADING = "heading"  # "## " — a numbered H2 in the paper
    SUBHEADING = "subheading"  # "### " — a numbered H3
    SCAFFOLD = "scaffold"  # ">> " — template structure with {slots}
    ANALYST_ACTION = "analyst_action"  # "[ANALYST] " — a human action, feeds the WIP register
    TABLE_ROW = "table_row"  # "|" — a markdown table row
    PARAGRAPH = "paragraph"


def classify_line(line: str) -> tuple[LineKind, str]:
    """Classify one line and return its body with the marker removed.

    Order matters: `SUBHEADING` is tested before `HEADING`, because "### x"
    starts with "##" and would otherwise read as a heading whose body begins with
    "#". The renderer got this right and the API never had the case at all.
    """
    s = line.strip()
    if not s:
        return LineKind.BLANK, ""
    if s.startswith(SUBHEADING_PREFIX):
        return LineKind.SUBHEADING, s[len(SUBHEADING_PREFIX) :].strip()
    if s.startswith(HEADING_PREFIX):
        return LineKind.HEADING, s[len(HEADING_PREFIX) :].strip()
    if s.startswith(SCAFFOLD_PREFIX.strip()):
        return LineKind.SCAFFOLD, s[len(SCAFFOLD_PREFIX.strip()) :].strip()
    if s.startswith(ANALYST_PREFIX.strip()):
        return LineKind.ANALYST_ACTION, s[len(ANALYST_PREFIX.strip()) :].strip()
    if s.startswith(TABLE_PREFIX):
        return LineKind.TABLE_ROW, s
    return LineKind.PARAGRAPH, s


def is_scaffolding(line: str) -> bool:
    """True for lines that are structure or instruction rather than prose.

    Used by the fidelity reviewer (which audits scaffold-stripped prose) and by
    the D8 machine-recommendation guard, which must not fire on a scaffold line
    telling the analyst where the recommendation goes.
    """
    kind, _ = classify_line(line)
    return kind in (LineKind.SCAFFOLD, LineKind.ANALYST_ACTION)


def strip_scaffold_lines(text: str) -> str:
    """`text` with scaffold and analyst-action lines removed."""
    return "\n".join(line for line in text.split("\n") if not is_scaffolding(line))
