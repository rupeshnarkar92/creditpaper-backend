"""Report every alembic revision id longer than 32 chars (varchar(32) limit)."""

import pathlib
import re

pat = re.compile(r'^revision[^=]*=\s*["\']([^"\']+)["\']', re.M)
over = []
for f in sorted(pathlib.Path("src/creditpaper/alembic/versions").glob("*.py")):
    m = pat.search(f.read_text(encoding="utf-8"))
    if m and len(m.group(1)) > 32:
        over.append((len(m.group(1)), m.group(1), f.name))
if over:
    for n, rid, name in over:
        print(f"{n}  {rid}  ({name})")
    raise SystemExit(1)
print("all revision ids <= 32 chars")
