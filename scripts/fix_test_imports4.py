"""Fix all 'from <mod> as <name>' broken imports.

The pattern was: my earlier script produced `from <mod> as <name>` (invalid)
instead of `import <mod> as <name>` (valid). Fix.
"""

import re
from pathlib import Path

TESTS = Path("tests")
SRC = Path("src/creditpaper")

# Pattern: `from <dotted.path> as <name>` (missing `import`)
pattern = re.compile(r"^from ((?:\w+\.)+\w+) as (\w+)$", re.MULTILINE)

for base in [TESTS, SRC]:
    for py in base.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        orig = text
        text = pattern.sub(r"import \1 as \2", text)
        if text != orig:
            py.write_text(text, encoding="utf-8")
            print(f"updated: {py}")

print("done")
