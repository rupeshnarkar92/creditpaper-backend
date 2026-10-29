"""Render an updated .docx reflecting in-platform edits — deterministic, free.

No LLM/API calls: `populate_from_ground_truth` builds the full DealModel from
the curated ground truth (same recipe `scripts/render_from_ground_truth.py`
uses), cached prose is loaded and spliced with any human edits captured as
Signals, and the existing renderer runs unmodified. Riverton-only for now
(mirrors the `_DEALS` registry pattern in `scripts/generate_paper.py`);
extending to another deal is adding a ground-truth path here once it has a
prose cache.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from creditpaper.deals.utils.prose_store import load_prose
from creditpaper.harness.populate import populate_from_ground_truth
from creditpaper.model.apply import set_prose
from creditpaper.model.core import Authorship, ProseBlock, SectionStatus
from creditpaper.paths import output_dir, repo_root
from creditpaper.renderer.docx_render import render_deal
from creditpaper.renderer.paper_spec import DEFAULT_SPEC, PaperSpec
from creditpaper.signals.utils.store import latest_edit_text

# Checkout-only: the ground truth it resolves lives under gitignored fixtures/.
ROOT = repo_root()

_GT_PATHS: dict[str, Path] = {
    "riverton": ROOT / "fixtures" / "build_deal" / "ground_truth.json",
}


def render_reviewed_paper(deal_id: str, spec: PaperSpec | None = None, *, tenant_id: str) -> Path:
    """`spec` is the paper theme as a PaperSpec (`PaperSpec.from_theme`); None keeps the
    renderer default."""
    if deal_id not in _GT_PATHS:
        raise ValueError(f"no ground-truth path registered for deal '{deal_id}'")
    prose = load_prose(deal_id, tenant_id=tenant_id)
    if prose is None:
        raise ValueError(f"no cached prose for deal '{deal_id}'")

    model, _report = populate_from_ground_truth(_GT_PATHS[deal_id])

    for key, block_json in prose.items():
        section = int(key)
        edited = latest_edit_text(deal_id, section, tenant_id=tenant_id)
        if edited is not None:
            # Authorship moves with the edit, not just the workflow status: the
            # status says where the block is in the process, authorship says who
            # wrote what the reader is holding.
            block_json = {
                **block_json,
                "text": edited,
                "status": SectionStatus.HUMAN_EDITED,
                "authorship": Authorship.HUMAN_EDITED,
            }
        set_prose(model, section, ProseBlock(**block_json))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    from creditpaper.db import tenant_uuid

    out_path = output_dir() / str(tenant_uuid(tenant_id)) / f"{deal_id}_reviewed_{stamp}.docx"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    render_deal(model, out_path, spec=spec or DEFAULT_SPEC)
    return out_path
