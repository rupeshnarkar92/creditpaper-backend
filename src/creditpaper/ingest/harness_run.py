"""Run the ingestion path over a folder of documents and report what it found.

Used by `scripts/ingest_accuracy.py`, and by `scripts/validate_track1.py` in its
ingest mode, so both exercise the same path rather than two that drift.

Almost nothing happens here. Routing a folder to slots is `harness/route.py`'s
job, and extracting, applying, deriving and reconciling is
`deals/utils/ingest.py::ingest`'s - in parallel, under a cost session that
records the spend and the wall-clock the user actually waits. This function is
the folder-shaped entry point to those two, and returns the model so the caller
can reconcile it against ground truth.

The folder is always supplied by the caller; nothing here knows where tenant
documents live. An empty folder is a valid run that found nothing, which is what
makes the harness safe to exercise in tests.
"""

from __future__ import annotations

from pathlib import Path

from creditpaper.model import DealModel

DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"
SUPPORTED_SUFFIXES = {".pdf"}


def run_deal_folder(
    folder: Path,
    *,
    deal_id: str = "deal-under-test",
    tenant_id: str = DEFAULT_TENANT,
) -> tuple[DealModel, float]:
    """Route, extract and apply a folder; return the model and what it cost."""
    if not folder.exists() or not folder.is_dir():
        raise FileNotFoundError(f"deal folder not found: {folder}")

    from creditpaper.deals.utils.ingest import ingest
    from creditpaper.harness.route import route_deal

    model = DealModel(tenant_id=tenant_id, deal_id=deal_id)
    # Typed as the union route_deal accepts: list is invariant, so a
    # list[str] is not a list[str | Path].
    files: list[str | Path] = sorted(
        {str(p) for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES}
    )
    if not files:
        # Not an error: a folder with nothing readable in it found nothing, and
        # saying so is more useful than raising at the caller.
        return model, 0.0

    report = ingest(model, route_deal(files))
    return model, report.cost_usd
