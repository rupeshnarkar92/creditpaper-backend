"""List all registered routes in the FastAPI app."""

import sys

sys.path.insert(0, "src")

from creditpaper.api.main import app

for r in app.routes:
    if hasattr(r, "path"):
        print(f"{','.join(r.methods or []):10} {r.path}")
