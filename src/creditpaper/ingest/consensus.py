"""Read each window more than once, and keep what the passes agree on.

Three measurements arrived at the same place. Anchoring is solved (1.1% anchor
divergence), values are steady (6.3% divergent), and entity keys merge to
nothing (0 paths, measured). What remains is FLAPPING — 75% of paths present in
some passes and absent from others — and it has nothing behind it. It is not a
defect with a cause to fix. **One pass is not a complete read.**

`google/langextract` reaches the same conclusion from the other direction and
defaults to `extraction_passes=3`. On this corpus three passes find 74-88% more
paths than one.

TWO THINGS A PASS COUNT BUYS, and the second is the one that was missing.

  RECALL. Passes are UNIONED, never intersected. A fact seen once in three
  passes is a fact that was found, not a fact two passes disagreed with — they
  did not disagree, they did not look. Intersecting would discard most of what
  multi-pass exists to gather.

  AGREEMENT. How many passes saw a fact is a confidence signal we have never
  had. `Beyond Logprobs` (arXiv 2606.24420) lists four signals a production
  extractor combines — self-consistency, logprobs, source grounding and
  validation rules — and until now this pipeline had exactly one of them.

And a third thing that falls out: when two passes read the SAME path to
DIFFERENT values, a single pass reports whichever it drew and looks certain.
Across passes that becomes visible, and `ConsensusFact.conflicting` carries it
to a human instead of hiding it.

DETERMINISM. Merging is a pure function of the draws in the order given.
Majority wins; a tie is broken by earliest pass, which is stable because passes
are ordered. Nothing here depends on timing, and with `ingest/fact_cache.py`
storing the MERGED result the N passes are paid for once and replayed forever.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import BaseModel


class ConsensusFact(BaseModel):
    """One fact, with how much of the reading agreed about it."""

    path: str
    value: Any
    line: int | None = None
    seen_in: int
    passes: int
    # Every OTHER value some pass read at this path, most common first. Empty
    # when the passes agreed. A single pass cannot produce this and so cannot
    # report it: it draws one value and has nothing to compare against.
    conflicting: tuple[Any, ...] = ()

    @property
    def agreement(self) -> float:
        """Share of passes that found this fact. 1.0 when all did."""
        return self.seen_in / self.passes if self.passes else 0.0

    @property
    def unanimous(self) -> bool:
        return self.seen_in == self.passes and not self.conflicting

    @property
    def contested(self) -> bool:
        """Passes found it and disagreed about the value — the worst case.

        Worse than a fact only one pass found, because every pass was looking
        at the same text and read it differently, and a single-pass run would
        have reported one of them with no sign anything was wrong.
        """
        return bool(self.conflicting)


def _key(value: Any) -> str:
    """Comparison key for a value. Formatting must not read as disagreement."""
    from creditpaper.ingest.values import numeric_tokens

    text = str(value).strip()
    tokens = numeric_tokens(text)
    # "$12,400,000" and 12400000 are the same answer, and the tokeniser that
    # decides is the one `value_in_span` uses, so the two cannot disagree about
    # what a number is.
    return tokens[0] if len(tokens) == 1 else text.casefold()


def merge(draws: list[list[dict[str, Any]]]) -> list[ConsensusFact]:
    """Union the passes, resolving each path to its majority value.

    `draws` is one list of raw fact dicts per pass, in pass order. Order
    matters only for tie-breaking, and only so the result is stable.

    Returns facts sorted by path, so two runs over the same draws produce
    byte-identical output.
    """
    passes = len(draws)
    if not passes:
        return []

    # path -> value key -> (first value seen, first line seen, count)
    seen: dict[str, dict[str, tuple[Any, int | None, int]]] = {}
    for draw in draws:
        # Within ONE pass a repeated path is one reading, not two votes: a
        # window that mentions the contract sum twice must not out-vote a pass
        # that mentioned it once.
        this_pass: dict[str, tuple[Any, int | None]] = {}
        for item in draw:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            value = item.get("value")
            if not path or value is None:
                continue
            line = item.get("line")
            this_pass.setdefault(path, (value, line if isinstance(line, int) else None))
        for path, (value, line) in this_pass.items():
            bucket = seen.setdefault(path, {})
            k = _key(value)
            if k in bucket:
                first_value, first_line, count = bucket[k]
                bucket[k] = (first_value, first_line, count + 1)
            else:
                bucket[k] = (value, line, 1)

    out: list[ConsensusFact] = []
    for path in sorted(seen):
        bucket = seen[path]
        # Majority, then earliest-seen. `bucket` preserves insertion order, so
        # the tie-break is "whichever pass read it first" — stable, and not a
        # function of which value happens to sort lower.
        ranked = sorted(bucket.items(), key=lambda kv: -kv[1][2])
        top_count = ranked[0][1][2]
        winner = next(k for k, v in bucket.items() if v[2] == top_count)
        value, line, count = bucket[winner]
        others = tuple(v[0] for k, v in bucket.items() if k != winner)
        out.append(
            ConsensusFact(
                path=path,
                value=value,
                line=line,
                seen_in=count,
                passes=passes,
                conflicting=others,
            )
        )
    return out


class ConsensusReport(BaseModel):
    facts: tuple[ConsensusFact, ...]
    passes: int

    @property
    def unanimous(self) -> int:
        return sum(1 for f in self.facts if f.unanimous)

    @property
    def contested(self) -> int:
        return sum(1 for f in self.facts if f.contested)

    def by_agreement(self) -> dict[int, int]:
        """`{passes that saw it: how many facts}` — the distribution a
        confidence band is drawn from."""
        return dict(Counter(f.seen_in for f in self.facts))


def report(draws: list[list[dict[str, Any]]]) -> ConsensusReport:
    return ConsensusReport(facts=tuple(merge(draws)), passes=len(draws))
