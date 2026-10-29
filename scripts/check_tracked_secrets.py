"""Fail closed when tracked files contain obvious credential literals.

The scanner reports paths and line numbers only; it never prints matched text.
Use provider-native scanners for high-entropy and historical-secret coverage.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".env.example", ".env.local.example", "package-lock.json", "uv.lock"}
PATTERNS = (
    re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:sk|rk|pk)[-_][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bgh[opsu]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)\b(password|secret|api[_-]?key|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
)
PLACEHOLDER_MARKERS = (
    "service-secret",
    "do-not-return-this",
    "shared-signing-key",
    "secret-provider-key",
    "tenant-secret",
    "whsec_test_placeholder",
    "sk-test-never-return-this",
)


def main() -> int:
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT, text=False).decode().split("\0")
    findings: list[str] = []
    for name in files:
        if not name or name in SKIP or name.startswith(".git/"):
            continue
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, 1):
            if any(marker in line.lower() for marker in PLACEHOLDER_MARKERS):
                continue
            if any(pattern.search(line) for pattern in PATTERNS):
                findings.append(f"{name}:{number}")
    if findings:
        print("Tracked credential-like literals found:")
        print("\n".join(findings))
        return 1
    print("No tracked credential-like literals found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
