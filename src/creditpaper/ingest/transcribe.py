"""Vision transcription of text-less pages, read once and kept in the parse cache.

A page whose text layer holds fewer than ``TEXT_LESS_CHAR_THRESHOLD`` (20)
non-whitespace characters - a scan, a photo, a drawing - reads as nothing: no
quote can anchor on it and no value can be checked against it. Such a page is
transcribed ONCE through the vision path (``llm.client.messages_create`` with the
page image and the versioned prompt ``prompts/transcribe_page.v1.txt``: plain
text, verbatim, reading order, one line per visual line, table cells separated
by two or more spaces, no commentary).

Storage: the transcriptions of a document are one parse-cache row, keyed by the
document's content hash and ``transcriber_version`` (prompt digest + model). That
version starts with ``parse_cache.VISION_PREFIX``, which marks the row
vision-sourced (``CachedParse.vision_sourced``); every page dict ``fill_text_less``
returns for such a page carries ``"source": "vision"``. A page's text layer is
never replaced when it has one. The row is written only after every text-less
page of the document was transcribed (the parse cache is insert-if-absent: a
partial row would block the complete one forever).

Both consumers read the same filled pages: extraction, so the extractor quotes
from the text the verifier will check, and verification.

Safety. A transcription can itself misread a digit, and the verifier's value
check runs against the transcription: a misread would make a wrong value look
stated. ``verify.verify_fact`` therefore adds ``Reason.VISION_TEXT`` to every
fact anchored on a vision page and caps its confidence at
``verify.VISION_TEXT_CAP``, below ``config.EXTRACTION_CONFIDENCE_THRESHOLD``: an
analyst sees the fact until an independent read agrees.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

from creditpaper.concurrency import pool
from creditpaper.config import extract_model
from creditpaper.ingest import parse_cache
from creditpaper.ingest.probe import Route
from creditpaper.processing.page_selector import TEXT_LESS_CHAR_THRESHOLD

log = logging.getLogger(__name__)

PROMPT_FILE = Path(__file__).with_name("prompts") / "transcribe_page.v1.txt"
VISION_SOURCE = "vision"
MAX_TOKENS = 8000

# 0-based page indices -> one base64 PNG per index, in order.
Render = Callable[[list[int]], list[str]]


@cache
def prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8").strip()


def transcriber_version(model: str | None = None) -> str:
    """The parse-cache version of a transcription: prompt digest + model."""
    digest = hashlib.sha256(prompt().encode("utf-8")).hexdigest()[:12]
    return f"{parse_cache.VISION_PREFIX}{digest}-{extract_model(model)}".replace("/", "_")


def is_text_less(text: str) -> bool:
    return len("".join(text.split())) < TEXT_LESS_CHAR_THRESHOLD


def text_less_pages(pages: Sequence[Mapping[str, Any]]) -> list[int]:
    """1-based numbers of the pages with no usable text layer."""
    return [int(p["page"]) for p in pages if is_text_less(str(p.get("text") or ""))]


def pending_pages(
    tenant_id: str,
    content_hash: str,
    pages: Sequence[Mapping[str, Any]],
    *,
    model: str | None = None,
    seed: Mapping[int, str] | None = None,
) -> list[int]:
    """The text-less pages a fill would pay to transcribe (in neither cache nor seed)."""
    wanted = text_less_pages(pages)
    if not wanted:
        return []
    cached = parse_cache.get(tenant_id, content_hash, transcriber_version(model))
    have = {**(seed or {}), **(cached.pages if cached is not None else {})}
    return [n for n in wanted if n not in have]


def transcribe_page(png_b64: str, *, model: str | None = None, client: Any = None) -> str:
    """One page image -> its plain-text transcription (one vision call)."""
    from creditpaper.llm.client import messages_create

    resp = messages_create(
        client=client,
        tool="ingest:transcribe",
        stage="transcribe",
        model=extract_model(model),
        max_tokens=MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": png_b64},
                    },
                    {"type": "text", "text": prompt()},
                ],
            }
        ],
    )
    return str(resp.text).strip()


def fill_text_less(
    tenant_id: str,
    content_hash: str,
    pages: Sequence[Mapping[str, Any]],
    render: Render,
    *,
    model: str | None = None,
    client: Any = None,
    workers: int = 4,
    seed: Mapping[int, str] | None = None,
) -> list[dict[str, Any]]:
    """``pages`` with every text-less page's text replaced by its transcription.

    Cached transcriptions are served; the rest are rendered, transcribed and, when
    the cache held none for this document, written as one row. ``seed`` holds
    transcriptions by this same ``transcriber_version`` kept elsewhere (the
    scorecard's file backup): used before paying, and written to the cache with
    the rest. Filled pages carry ``"source": "vision"``; every other page is
    returned unchanged.

    A page whose transcription fails (a truncated response, a transport error) is
    returned unchanged - still text-less - and the others are kept: one bad page
    must not throw away the pages already paid for. The cache row is then not
    written (it would be partial, and it is final once written); a caller that
    keeps the returned transcriptions (the scorecard's backup) seeds them back.
    """
    out = [dict(p) for p in pages]
    wanted = text_less_pages(pages)
    if not wanted:
        return out
    version = transcriber_version(model)
    cached = parse_cache.get(tenant_id, content_hash, version)
    texts: dict[int, str] = dict(seed or {})
    if cached is not None:
        texts.update(cached.pages)
    todo = [n for n in wanted if n not in texts]
    if todo:
        images = render([n - 1 for n in todo])
        if len(images) != len(todo):
            raise RuntimeError(f"rendered {len(images)} of {len(todo)} text-less pages")
        failed: list[str] = []
        with pool(min(workers, len(todo)), thread_name_prefix="transcribe") as ex:
            futs = [ex.submit(transcribe_page, img, model=model, client=client) for img in images]
            for n, fut in zip(todo, futs, strict=True):
                try:
                    texts[n] = fut.result()
                except Exception as exc:  # noqa: BLE001 - keep the pages already paid for
                    failed.append(type(exc).__name__)
        if failed:
            log.warning("transcribe: %d page(s) failed (%s); row not cached", len(failed), ",".join(failed))
    complete = all(n in texts for n in wanted)  # a partial row would be final: it waits
    if complete and cached is None:
        parse_cache.put(
            tenant_id,
            content_hash,
            route=Route.SCANNED if len(wanted) == len(pages) else Route.MIXED,
            page_count=len(pages),
            pages={n: texts[n] for n in wanted},
            parser_version=version,
        )
    elif cached is not None and todo:  # the row is final (insert-if-absent): these stay uncached
        log.warning("transcribe: %d text-less page(s) not in the cached row", len(todo))
    for p in out:
        n = int(p["page"])
        if n in texts and n in wanted:
            p["text"] = texts[n]
            p["source"] = VISION_SOURCE
    return out


def vision_pages(pages: Sequence[Mapping[str, Any]]) -> frozenset[int]:
    """The page numbers whose text came from a vision transcription."""
    return frozenset(int(p["page"]) for p in pages if p.get("source") == VISION_SOURCE)
