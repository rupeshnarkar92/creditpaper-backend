"""Fix 'from creditpaper.harness import X' style imports."""

import re
from pathlib import Path

TESTS = Path("tests")

# Map old harness module names to new locations
mapping = {
    "onboarding_store": "creditpaper.onboarding.utils.store",
    "signal_store": "creditpaper.signals.utils.store",
    "render_reviewed": "creditpaper.deals.utils.render_reviewed",
    "prose_store": "creditpaper.deals.utils.prose_store",
    "papers_store": "creditpaper.onboarding.utils.papers_store",
    "ingest": "creditpaper.deals.utils.ingest",
    "route": "creditpaper.deals.utils.route",
    "populate": "creditpaper.deals.utils.populate",
    "review": "creditpaper.deals.utils.review",
    "reconcile": "creditpaper.deals.utils.reconcile",
    "learn_house_style": "creditpaper.onboarding.utils.learn_house_style",
    "determinism": "creditpaper.deals.utils.determinism",
}

for py in TESTS.rglob("*.py"):
    text = py.read_text(encoding="utf-8")
    orig = text
    # Replace multi-module imports
    pattern = re.compile(r"from creditpaper\.harness import ([^\n]+)")
    for m in pattern.finditer(text):
        old_imports = [s.strip() for s in m.group(1).split(",")]
        new_imports = []
        for imp in old_imports:
            if imp in mapping:
                new_imports.append(f"{mapping[imp]} as {imp}")
            else:
                new_imports.append(imp)  # leave unchanged
        new_line = "from " + ", ".join(new_imports)
        text = text.replace(m.group(0), new_line)
    if text != orig:
        py.write_text(text, encoding="utf-8")
        print(f"updated: {py}")

print("done")
