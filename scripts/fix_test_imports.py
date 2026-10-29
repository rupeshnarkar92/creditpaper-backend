"""Fix test imports after the deals/onboarding/signals restructure."""

import re
from pathlib import Path

TESTS = Path("tests")

replacements = [
    (r"creditpaper\.harness\.ingest", "creditpaper.deals.utils.ingest"),
    (r"creditpaper\.harness\.render_reviewed", "creditpaper.deals.utils.render_reviewed"),
    (r"creditpaper\.harness\.signal_store", "creditpaper.signals.utils.store"),
    (r"creditpaper\.harness\.onboarding_store", "creditpaper.onboarding.utils.store"),
    (r"creditpaper\.harness\.papers_store", "creditpaper.onboarding.utils.papers_store"),
    (r"creditpaper\.harness\.prose_store", "creditpaper.deals.utils.prose_store"),
    (r"creditpaper\.api\.deals", "creditpaper.deals.routes.deals"),
    (r"creditpaper\.api\.prose_blocks", "creditpaper.deals.utils.prose_blocks"),
    (r"creditpaper\.api\.onboarding\.(\w+)", r"creditpaper.onboarding.routes.\1"),
    (r"creditpaper\.services\.onboarding\.(\w+)", r"creditpaper.onboarding.services.\1"),
]

for py in TESTS.rglob("*.py"):
    text = py.read_text(encoding="utf-8")
    orig = text
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text)
    if text != orig:
        py.write_text(text, encoding="utf-8")
        print(f"updated: {py}")

print("done")
