"""The one instruction a general extraction pass sends, assembled in one place.

This lived in `scripts/general_extraction_probe.py` until 2026-09-22 and had to
move: `ingest/fact_cache.py` keys stored facts on `PROMPT_VERSION`, and a
version constant guarding a prompt that lives in a script guards nothing. A
stale cache serving facts drawn under a previous instruction is silent and
indefinitely wrong, so the prompt and the version that identifies it belong
together and under test.

WHAT IT SAYS, and why each part is where it is:

  * The FIELD LIST goes FIRST. It is ~5,400 tokens and it is the cacheable
    bulk, so its position costs nothing. When it came last, the model generated
    with the path list freshest and the anchor rule distant, and 27.0% of facts
    came back with a quote too short to verify.
  * No document type is named, and the model is told it is not being told. The
    whole point of general extraction is that a document produces facts without
    a filename keyword deciding whether it is read at all.
  * The ANCHOR RULE is shared with the nine per-document extractors
    (`extractors/_doc.ANCHOR_RULE`), not restated. Nine prompts each writing
    their own version is how they drifted from the verifier the first time.
  * The bracket-key example is explicit because "substitute a slug for
    `[<key>]`" was read as replacing the whole bracket token, and the model
    returned `valuation.comp_set.riddell_street.sale_price` — the right field
    with a key the citation grammar rejects.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping

from creditpaper.extractors._doc import ANCHOR_RULE
from creditpaper.ingest.anchor import MIN_QUOTE_WORDS
from creditpaper.model.concepts import REGISTRY, Concept

# `| `path` | `Type` |` — the generated reference's only meaningful line shape.
_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|$")

# `Fact[X]` is a LEAF the model may write to. Anything else is a container —
# `valuation.comp_set[<key>]` is a `ComparableAttributes`, not a value — and
# offering it invites a write that cannot be applied.
_LEAF = re.compile(r"^Fact\[(.+)\]$")

# Type as a HEADING over a group, never as a marker on the line.
#
# The first version appended a sigil — `economics.facility_amount#` — and told
# the model the marker was not part of the path. It copied it anyway: 163 of
# 390 returned paths were real paths with a trailing `#`, `@` or `?`, and path
# validity read as 58% when nothing was actually wrong with the extraction.
#
# That is the third time in this file's history that the model reproduced the
# format it was shown rather than the format it was told about (a dotted
# collection key, then a substituted `[<key>]` token, then this). The rule that
# generalises: SHOW EXACTLY THE STRING YOU WANT BACK. A path line is now
# nothing but a path.
_GROUP = {
    "Decimal": "NUMBERS",
    "int": "NUMBERS",
    "date": "DATES",
    "bool": "TRUE/FALSE",
}
_GROUP_ORDER = ("NUMBERS", "DATES", "TRUE/FALSE", "TEXT")

# One `[...]` of a path. Named so `normalise_key_path` and `fixed_keys` cannot
# disagree about what a key is.
_KEY_IN_PATH = re.compile(r"\[[^\]]*\]")


# --- collections whose keys are FIXED -------------------------------------------------
#
# `valuation.values_by_basis[<key>]` has five legal keys and the prompt never
# said so, so the model invented one from whatever the page called the figure.
# Measured 2026-09-25 on blind_03, one value under three keys:
#
#     [as_if_complete_in_one_line_exclusive_gst]        $19,080,000
#     [as_if_complete_exclusive_of_gst]                 $19,080,000
#     [market_value_as_if_complete_in_one_line_...]     $19,080,000
#
# One basis, three spellings, three facts. 107 of 772 facts - 14% - were one
# figure filed under more than one key, and it is worse than noise: none of
# those keys matches a concept, so `concept_for` returns None and the fact gets
# no materiality, no semantic support check and no place in the five-basis
# design that `values_by_basis` exists to enforce.
#
# The registry already knows the legal keys. This puts them in the prompt.


def fixed_keys(registry: Mapping[str, Concept] | None = None) -> dict[str, tuple[str, ...]]:
    """`{path shape: the keys the registry names for it}`.

    A concept with a CONCRETE key (`values_by_basis[as_is]`) fixes that
    collection's vocabulary; one with `[*]` does not, and a collection holding
    any wildcard is left open. A comparable sale is keyed by its address and no
    list could enumerate those, so offering a closed list there would be a lie.
    """
    concrete: dict[str, set[str]] = {}
    wildcard: set[str] = set()
    for path in registry if registry is not None else REGISTRY:
        for match in _KEY_IN_PATH.finditer(path):
            key = match.group()[1:-1]
            shape = normalise_key_path(path)
            if key == "*":
                wildcard.add(shape)
            else:
                concrete.setdefault(shape, set()).add(key)
    return {shape: tuple(sorted(keys)) for shape, keys in concrete.items() if shape not in wildcard}


@functools.cache
def _fixed_keys() -> dict[str, tuple[str, ...]]:
    """`fixed_keys()` computed once. Cached rather than assigned at import,
    because `normalise_key_path` is defined further down this module."""
    return fixed_keys()


def closed_keys_block(markdown: str) -> str:
    """The closed collections and their legal keys, as a SEPARATE section.

    NOT appended to the path lines, and that is the whole point. The first
    version of this wrote `valuation.values_by_basis[<key>]  keys: as_is|...`
    and `test_no_path_is_lost_or_altered` refused it, correctly: this module's
    own history records that a sigil added to a path line came back COPIED —
    163 of 390 returned paths carried a trailing `#`, `@` or `?` and path
    validity read as 58% when nothing was wrong with the extraction.

    A path line is nothing but a path. The vocabulary lives here, where it
    cannot be mistaken for part of one.
    """
    closed = _fixed_keys()
    shapes = sorted(shape for shape in closed if _offered(shape, markdown))
    if not shapes:
        return ""
    lines = [
        "CLOSED KEYS. These collections accept ONLY the keys listed. Use one "
        "exactly as written; if none of them is what the page states, omit the "
        "fact rather than inventing a key — a key that is not listed matches no "
        "field and the figure is lost."
    ]
    lines += [f"{shape} -> {'|'.join(closed[shape])}" for shape in shapes]
    return "\n".join(lines)


def _offered(shape: str, markdown: str) -> bool:
    """Is this collection actually in the field list the model was given?

    A concept can name a path the schema no longer offers; listing its keys
    would then be an instruction about a field that cannot be written.
    """
    return shape in {normalise_key_path(p) for p in _leaf_paths(markdown)}


def _leaf_paths(markdown: str) -> list[str]:
    """Every LEAF path the reference offers, in file order.

    Shared by `compact_reference` and `closed_keys_block` so they cannot
    disagree about which paths the model was actually given.
    """
    out: list[str] = []
    for raw in markdown.splitlines():
        match = _ROW.match(raw.strip())
        if match and _LEAF.match(match.group(2)):
            out.append(match.group(1))
    return out


def compact_reference(markdown: str) -> str:
    """The generated field reference as the smallest unambiguous path list.

    Measured 2026-09-22: the markdown reference is ~5,050 tokens and is carried
    on EVERY call. At 10-page windows that was a third of the input; at the
    2-page windows the recall measurement argued for, it is five times the size
    of the content it is attached to.

    Three things go, none of them information the model can act on:

      * the prose header, which explains the file to a human reader;
      * 92 CONTAINER rows out of 383. `valuation.comp_set[<key>]` types as
        `ComparableAttributes` — it is a place facts live, not a fact. A path
        ending there cannot be applied, so offering it can only produce
        rejects;
      * markdown pipes, backticks, and `Fact[...]` spelled out 285 times.

    Types survive as a single character, because "is this a number or prose" is
    the only part of the type the model acts on.

    Takes the generated markdown rather than reading it, so this stays pure and
    there is no second artifact to drift from `model/deal.py`.
    """
    grouped: dict[str, list[str]] = {name: [] for name in _GROUP_ORDER}
    for raw in markdown.splitlines():
        match = _ROW.match(raw.strip())
        if not match:
            continue
        path, type_name = match.group(1), match.group(2)
        leaf = _LEAF.match(type_name)
        if not leaf:
            continue
        grouped[_GROUP.get(leaf.group(1), "TEXT")].append(path)

    out: list[str] = []
    for name in _GROUP_ORDER:
        paths = grouped[name]
        if not paths:
            continue
        out.append(f"[{name}]")
        out.extend(paths)
    return "\n".join(out)


def field_kinds(markdown: str) -> dict[str, str]:
    """`{path with keys normalised: "number" | "date" | "bool" | "text"}`.

    The same parse that builds the prompt's `[NUMBERS]` / `[DATES]` headings,
    exposed so the value a model returns can be parsed TO ITS DECLARED TYPE
    rather than guessed at.

    `_doc.add_fact` guesses: it strips `$ , %` and tries `Decimal`, falling back
    to a string. That turns a licence number or a title reference into a
    `Decimal`, which `apply._coerce_to_declared` then has to turn back — its
    own comment records the workaround. Knowing the declared kind removes the
    guess in both directions.

    Keys are normalised to `[<key>]` so a concrete path
    (`its.guarantors[acme]`) finds its schema entry.
    """
    out: dict[str, str] = {}
    for raw in markdown.splitlines():
        match = _ROW.match(raw.strip())
        if not match:
            continue
        leaf = _LEAF.match(match.group(2))
        if not leaf:
            continue
        group = _GROUP.get(leaf.group(1), "TEXT")
        kind = {"NUMBERS": "number", "DATES": "date", "TRUE/FALSE": "bool"}.get(group, "text")
        out[normalise_key_path(match.group(1))] = kind
    return out


def normalise_key_path(path: str) -> str:
    """Any `[...]` becomes `[<key>]`, so a concrete path matches the schema."""
    return _KEY_IN_PATH.sub("[<key>]", path)


_KEY_RULE_TEXT = (
    "- Where the list shows `[<key>]`, KEEP THE SQUARE BRACKETS and put a "
    "lowercase underscore_slug inside them. Correct: "
    "`valuation.comp_set[riddell_street].sale_price`. Wrong: "
    "`valuation.comp_set.riddell_street.sale_price` — a dotted key is not "
    "a valid path and the fact is rejected.\n"
)
_KEY_RULE_STRUCTURED = (
    "- Where the list shows `[<key>]`, choose the path exactly as listed, "
    "`[<key>]` included, and put one lowercase underscore_slug per `[<key>]` "
    "in `keys`, in order. Correct: path `valuation.comp_set[<key>].sale_price` "
    'with keys ["riddell_street"]. A path with no `[<key>]` has empty keys.\n'
)
_RETURN_TEXT = (
    "Return JSON, and nothing else:\n"
    '{"facts": [{\n'
    '  "path":  "<a path copied exactly from the field list above>",\n'
    '  "value": <the value as stated>,\n'
    '  "line":  <the number of the line the value appears on>\n'
    "}]}"
)
_RETURN_STRUCTURED = (
    "Reply in the required format: one entry per fact, with its path, its keys, "
    "the value as stated, and the number of the line the value appears on."
)


def build_system_lines(reference: str, *, structured: bool = False) -> str:
    """The LINE-ANCHORED variant: the model points, we cut the clause.

    `build_system` asks for a verbatim clause, which makes anchoring the
    model's job and fails two measured ways — a quote too short to locate and a
    quote that does not locate at all, together 15-30% of facts depending on
    configuration.

    Here the window arrives with every line numbered and the model returns the
    LINE the value sits on. `corpus.clause_at` then cuts the clause from the
    page text we already hold, so it anchors by construction and always meets
    the word minimum. The model also writes a number instead of a sentence,
    and output is the dominant cost at small windows.

    What survives is the check worth keeping: whether the value is actually on
    the line that was cited.
    """
    return (
        "You extract facts from commercial property finance documents into a "
        "typed deal model.\n\n"
        "FIELD LIST — the only paths you may use. The [NUMBERS], [DATES], "
        "[TRUE/FALSE] and [TEXT] headings say what KIND of value each path "
        "below them holds. Copy a path exactly as written; the headings are "
        "not part of any path.\n\n"
        f"{compact_reference(reference)}\n\n"
        f"{closed_keys_block(reference)}\n\n"
        "=====\n\n"
        "You will be given some consecutive pages from ONE document, with "
        "every line numbered as `N| text`. You are NOT told what kind of "
        "document it is — work that out from the pages.\n\n"
        "LINE RULE — applied to EVERY value you return.\n"
        "`line` is the number of the line the value APPEARS ON. Do not quote "
        "the text; give the number. If the value spans two lines, give the "
        "line the value itself starts on, not the line its label is on.\n"
        "The number must be one shown in the input. A line number that is not "
        "in the input discards the fact.\n\n"
        "RULES\n"
        "- `path` MUST be copied exactly from the field list above. Never "
        "invent a path. If a fact you can see has no path in the list, omit "
        "it.\n"
        f"{_KEY_RULE_STRUCTURED if structured else _KEY_RULE_TEXT}"
        "- Return only facts the pages STATE. Do not infer, calculate or "
        "carry over knowledge.\n"
        "- COLLECTIONS. A path containing `[<key>]` is a LIST. When the pages "
        "show a table or list of them, return EVERY row, not a selection.\n"
        "- Key each row on the most stable identifier the ROW ITSELF states — "
        "a street address, a lot number, a party name — as a lowercase "
        "underscore_slug. Never key on a row's position (`row_1`, "
        "`comparable_2`): position is not a property of the thing, so two "
        "reads that order the table differently describe one row as two.\n"
        '- Return {"facts": []} if these pages state nothing in the list.\n\n'
        f"{_RETURN_STRUCTURED if structured else _RETURN_TEXT}"
    )


def path_templates(reference: str) -> list[str]:
    """Every leaf path the reference offers as a template (`[<key>]` per key), deduplicated in file order.

    The structured reply's `path` enum: the model chooses a template and names the keys separately, so
    a path outside the field list cannot be returned at all."""
    return list(dict.fromkeys(normalise_key_path(p) for p in _leaf_paths(reference)))


def build_system(reference: str) -> str:
    """The system prompt, byte-identical on every call so it serves from cache.

    `reference` is `docs/FIELD_REFERENCE.generated.md` — generated from
    `model/deal.py`, never hand-maintained, so the vocabulary cannot drift from
    the schema. Passed in rather than read here to keep this pure.
    """
    return (
        "You extract facts from commercial property finance documents into a "
        "typed deal model.\n\n"
        "FIELD LIST — the only paths you may use. The [NUMBERS], [DATES], "
        "[TRUE/FALSE] and [TEXT] headings say what KIND of value each path "
        "below them holds. Copy a path exactly as written; the headings are "
        "not part of any path.\n\n"
        f"{compact_reference(reference)}\n\n"
        f"{closed_keys_block(reference)}\n\n"
        "=====\n\n"
        "You will be given some consecutive pages from ONE document. You are "
        "NOT told what kind of document it is — work that out from the pages.\n\n"
        f"{ANCHOR_RULE}\n\n"
        "RULES\n"
        "- `path` MUST be copied exactly from the field list above. Never "
        "invent a path. If a fact you can see has no path in the list, omit "
        "it.\n"
        "- Where the list shows `[<key>]`, KEEP THE SQUARE BRACKETS and put a "
        "lowercase underscore_slug inside them. Correct: "
        "`valuation.comp_set[riddell_street].sale_price`. Wrong: "
        "`valuation.comp_set.riddell_street.sale_price` — a dotted key is not "
        "a valid path and the fact is rejected.\n"
        "- `page` is the page number as labelled in the input.\n"
        "- Return only facts the pages STATE. Do not infer, calculate or "
        "carry over knowledge.\n"
        '- Return {"facts": []} if these pages state nothing in the list.\n\n'
        "Return JSON, and nothing else:\n"
        '{"facts": [{\n'
        '  "path":   "<a path copied exactly from the field list above>",\n'
        '  "value":  <the value as stated>,\n'
        f'  "clause": "<the complete sentence or whole table row containing the '
        f'value, verbatim, AT LEAST {MIN_QUOTE_WORDS} WORDS>",\n'
        '  "page":   <the page number labelled in the input>\n'
        "}]}"
    )
