"""Fix broken syntax in test imports."""

import re
from pathlib import Path

TESTS = Path("tests")

for py in TESTS.rglob("*.py"):
    text = py.read_text(encoding="utf-8")
    orig = text
    # The previous script produced `from X import Y as Y` but for some tests
    # it produced `from X as Y` which is invalid. Fix it.
    # Pattern: `from <module> as <name>` should be `from <module> import ... as <name>`.
    # We need to re-import the actual symbols.
    bad_pattern = re.compile(r"^from (creditpaper\.\S+(?:\.\S+)*) as (\w+)$", re.MULTILINE)
    for m in bad_pattern.finditer(text):
        module, name = m.group(1), m.group(2)
        # Default: just expose the module as the name
        replacement = f"import {module} as {name}"
        text = text.replace(m.group(0), replacement)
    if text != orig:
        py.write_text(text, encoding="utf-8")
        print(f"updated: {py}")

print("done")
