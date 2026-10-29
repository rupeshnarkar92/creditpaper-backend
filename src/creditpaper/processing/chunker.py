"""Sliding-window text chunker. 1500-char window, 200-char overlap.

S3 (Fact Extraction) consumes chunks in groups of ~3 to fit a typical
LLM context. The 200-char overlap prevents facts that straddle a
chunk boundary from being missed.
"""

from __future__ import annotations

CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200


def chunk_text(text: str) -> list[dict]:
    if not text:
        return []
    chunks: list[dict] = []
    start = 0
    idx = 0
    n = len(text)
    while start < n:
        end = min(start + CHUNK_SIZE, n)
        chunks.append(
            {
                "idx": idx,
                "text": text[start:end],
                "start_char": start,
                "end_char": end,
            }
        )
        idx += 1
        if end == n:
            break
        start = end - CHUNK_OVERLAP
    return chunks
