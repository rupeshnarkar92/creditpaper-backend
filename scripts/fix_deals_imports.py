"""Fix imports after deals restructure."""

import re
from pathlib import Path

BASE = Path("src/creditpaper")
ROUTES_BASE = Path("apps/credit-web/src")  # not needed but skip

replacements = [
    (r"from creditpaper\.api\.deals import", "from creditpaper.deals.routes.deals import"),
    (r"from creditpaper\.api\.prose_blocks import", "from creditpaper.deals.utils.prose_blocks import"),
    (r"from creditpaper\.harness\.ingest import", "from creditpaper.deals.utils.ingest import"),
    (
        r"from creditpaper\.harness\.render_reviewed import",
        "from creditpaper.deals.utils.render_reviewed import",
    ),
    (r"from creditpaper\.harness\.signal_store import", "from creditpaper.signals.utils.store import"),
]

for py in BASE.rglob("*.py"):
    text = py.read_text(encoding="utf-8")
    orig = text
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text)
    if text != orig:
        py.write_text(text, encoding="utf-8")
        print(f"updated: {py}")

print("done")
