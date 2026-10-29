"""Fix imports after onboarding restructure.

Run once from project root: uv run python scripts/fix_onboarding_imports.py
"""

import re
from pathlib import Path

ONBOARDING_DIR = Path("src/creditpaper/onboarding")
ROUTES_DIR = Path("src/creditpaper/api")  # any stragglers

replacements = [
    # service imports
    (r"from creditpaper\.services\.onboarding\.", "from creditpaper.onboarding.services."),
    (r"from creditpaper\.services\.onboarding import", "from creditpaper.onboarding.services import"),
    # store imports
    (r"from creditpaper\.harness\.onboarding_store import", "from creditpaper.onboarding.utils.store import"),
    (
        r"from creditpaper\.harness import onboarding_store",
        "from creditpaper.onboarding.utils import store as onboarding_store",
    ),
]

for base in [ONBOARDING_DIR, ROUTES_DIR]:
    for py in base.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        orig = text
        for pattern, repl in replacements:
            text = re.sub(pattern, repl, text)
        if text != orig:
            py.write_text(text, encoding="utf-8")
            print(f"updated: {py}")

print("done")
