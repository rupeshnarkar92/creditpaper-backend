"""Deal registry for the review API.

The implementation moved to `harness/prose_store.py`. It lived here, and the
ENGINE imported it from the HTTP edge (`harness/render_reviewed.py` ->
`api.deals`) while `api/main.py` imported `render_reviewed_paper` back out of
`harness` — a package-level cycle with the engine depending on the platform, and
the single thing preventing the `contracts/` vs `engine/` split.

Kept as a re-export so the API's own import site is unchanged; the dependency now
runs one way (api -> harness) and `OUTPUT_DIR` is no longer a module constant
computed from `__file__`.
"""

from __future__ import annotations

from creditpaper.deals.utils.prose_store import (
    list_deal_ids,
    load_prose,
    prose_path,
    validate_deal_id,
)
from creditpaper.paths import output_dir

__all__ = [
    "list_deal_ids",
    "load_prose",
    "output_dir",
    "prose_path",
    "validate_deal_id",
]
