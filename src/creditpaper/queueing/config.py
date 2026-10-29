"""Celery queue names and routing kept independent of business modules."""

from __future__ import annotations

import os

from dotenv import load_dotenv

from creditpaper.env_file import env_file_path

load_dotenv(env_file_path(), override=False)

INTAKE_QUEUE = "documents.intake"
PARSE_QUEUE = "documents.parse"
OCR_QUEUE = "documents.ocr"
FACTS_QUEUE = "facts.extract"
GENERATION_QUEUE = "paper.generate"

QUEUE_BY_KIND = {
    "archive_import": INTAKE_QUEUE,
    # Matching is deterministic intake reconciliation, not model extraction.
    # Keeping it off facts.extract prevents a long Ollama call from delaying
    # document availability after parsing prerequisites finish.
    "archive_match": INTAKE_QUEUE,
    "parse": PARSE_QUEUE,
    "parse_ocr": OCR_QUEUE,
    "document": FACTS_QUEUE,
    "extraction": FACTS_QUEUE,
    "corpus_extraction": FACTS_QUEUE,
    "ingest_window": FACTS_QUEUE,
    "ingest_speculate": FACTS_QUEUE,
    "ingest_propose": FACTS_QUEUE,
    "generation": GENERATION_QUEUE,
}


def broker_url() -> str:
    return os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")


def result_backend() -> str:
    return os.getenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")


def queue_for_kind(kind: str) -> str:
    return QUEUE_BY_KIND.get(kind, FACTS_QUEUE)


def migrated_kinds() -> list[str]:
    """Job kinds delivered through Celery instead of the legacy DB worker."""
    return [
        kind.strip()
        for kind in os.getenv(
            "CELERY_MIGRATED_KINDS",
            "archive_import,parse,parse_ocr,archive_match",
        ).split(",")
        if kind.strip()
    ]
