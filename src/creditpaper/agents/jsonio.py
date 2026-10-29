"""Robustly extract the first JSON object from a model's text response.

LLMs sometimes wrap JSON in code fences or add a sentence before/after it.
`raw_decode` parses the first complete JSON value and ignores trailing data, so
this tolerates both leading prose and a trailing explanation.
"""

from __future__ import annotations

import json
import re

from creditpaper.obs.timing import timed


@timed("validation")
def extract_json_object(raw: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model output")
    # strict=False tolerates raw control chars (newlines) inside strings
    obj, _ = json.JSONDecoder(strict=False).raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("model output was not a JSON object")
    return obj


def iter_json_objects(raw: str) -> list[dict]:
    """Every complete, balanced ``{...}`` object in the text, each parsed on its own.

    Best-effort and truncation-tolerant: an incomplete trailing object (e.g. a
    response cut off at the model's token limit) is skipped rather than aborting
    the parse. Used to salvage the elements of a findings array the model didn't
    finish, so a long review never crashes a whole section's generation."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()).strip()
    decoder = json.JSONDecoder(strict=False)
    objs: list[dict] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            objs.append(obj)
            i = end
        else:
            i += 1
    return objs
