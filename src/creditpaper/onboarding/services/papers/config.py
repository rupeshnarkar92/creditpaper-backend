"""Thresholds and limits for paper processing, read from environment.

One definition each. Read per call rather than captured at import so a test or a
worker can change a limit without reloading modules — the same rule
`creditpaper.config` follows for model tiers.
"""

from __future__ import annotations

import os

# The product constant. Centralised here rather than scattered as a literal 3.
REQUIRED_VALIDATED_PAPERS = 1

_DEFAULTS: dict[str, str] = {
    "PAPERS_MAX_UPLOAD_SIZE": str(20 * 1024 * 1024),
    "PAPERS_MIN_FILE_SIZE": "512",
    "PAPERS_MAX_PAGES": "400",
    "PAPERS_MIN_WORDS": "300",
    "PAPERS_MIN_PAGES": "2",
    "PAPERS_MIN_TEXT_QUALITY": "0.45",
    "PAPERS_MIN_RULE_SCORE": "0.35",
    "PAPERS_MIN_CREDIT_CONFIDENCE": "0.70",
    "PAPERS_MIN_APPROVAL_CONFIDENCE": "0.65",
    "PAPERS_LVR_TOLERANCE_PCT": "1.0",
    "PAPERS_LLM_MAX_RETRIES": "2",
    "PAPERS_CLAMAV_ENABLED": "false",
    "PAPERS_CLAMAV_HOST": "",
    "PAPERS_CLAMAV_PORT": "3310",
    "PAPERS_EXTRACT_CHAR_BUDGET": "120000",
}


def _raw(key: str) -> str:
    return os.getenv(key) or _DEFAULTS[key]


def _int(key: str) -> int:
    try:
        return int(_raw(key))
    except ValueError:
        return int(_DEFAULTS[key])


def _float(key: str) -> float:
    try:
        return float(_raw(key))
    except ValueError:
        return float(_DEFAULTS[key])


def _bool(key: str) -> bool:
    return _raw(key).strip().lower() in {"1", "true", "yes", "on"}


def max_upload_size() -> int:
    return _int("PAPERS_MAX_UPLOAD_SIZE")


def min_file_size() -> int:
    return _int("PAPERS_MIN_FILE_SIZE")


def max_pages() -> int:
    return _int("PAPERS_MAX_PAGES")


def min_words() -> int:
    return _int("PAPERS_MIN_WORDS")


def min_pages() -> int:
    return _int("PAPERS_MIN_PAGES")


def min_text_quality() -> float:
    return _float("PAPERS_MIN_TEXT_QUALITY")


def min_rule_score() -> float:
    return _float("PAPERS_MIN_RULE_SCORE")


def min_credit_confidence() -> float:
    return _float("PAPERS_MIN_CREDIT_CONFIDENCE")


def min_approval_confidence() -> float:
    return _float("PAPERS_MIN_APPROVAL_CONFIDENCE")


def lvr_tolerance_pct() -> float:
    return _float("PAPERS_LVR_TOLERANCE_PCT")


def llm_max_retries() -> int:
    return _int("PAPERS_LLM_MAX_RETRIES")


def clamav_enabled() -> bool:
    return _bool("PAPERS_CLAMAV_ENABLED")


def clamav_address() -> tuple[str, int]:
    return _raw("PAPERS_CLAMAV_HOST").strip(), _int("PAPERS_CLAMAV_PORT")


def extract_char_budget() -> int:
    return _int("PAPERS_EXTRACT_CHAR_BUDGET")
