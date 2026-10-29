"""Run extraction on Forest Knoll documents and capture training data.

Usage:
    python scripts/run_extraction_training.py

This script:
1. Routes Forest Knoll documents to extractors
2. Runs actual extraction using Ollama
3. Captures results as training examples
4. Stores in DB for future improvement
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from creditpaper.harness.route import route_deal
from creditpaper.model.deal import DealModel
from creditpaper.training.service import save_training_example

DEAL_DOCS = Path(r"D:\OSProjects\cp_docs\deal_files-20260910T041607Z-1-001\deal_files\Deal Documents")
DEAL_ID = "forest-knoll-2026"
TENANT_ID = "00000000-0000-0000-0000-000000000001"


def collect_files() -> list[str]:
    """Collect all files from the deal documents folder."""
    files = []
    for f in DEAL_DOCS.rglob("*"):
        if f.is_file() and not f.name.startswith("~"):
            files.append(str(f))
    return files


def run_extraction_for_slot(slot_key: str, doc_path: Path, model: DealModel) -> dict:
    """Run extraction for a single document and return results."""
    from creditpaper.harness.route import EXTRACTOR_REGISTRY

    if slot_key not in EXTRACTOR_REGISTRY:
        return {"error": f"No extractor for slot {slot_key}"}

    extractor_class = EXTRACTOR_REGISTRY[slot_key]
    extractor = extractor_class()

    doc_id = f"{slot_key}:{doc_path.name}"

    try:
        output = extractor.extract(doc_id, doc_path)
        return {
            "fields": {
                k: {"value": str(v.value), "page": v.provenance.page if v.provenance else None}
                for k, v in output.fields.items()
            },
            "flags": output.flags,
            "field_count": len(output.fields),
        }
    except Exception as e:
        return {
            "error": str(e),
            "fields": {},
            "flags": [f"extraction failed: {e}"],
            "field_count": 0,
        }


def main():
    print("=" * 60)
    print("EXTRACTION TRAINING - RUNNING ACTUAL EXTRACTION")
    print("=" * 60)
    print(f"Deal: {DEAL_ID}")
    print(f"Documents: {DEAL_DOCS}")
    print()

    # 1. Collect files
    files = collect_files()
    print(f"Found {len(files)} files")

    # 2. Route documents
    mappings = route_deal(files)
    print(f"\nRouted to {len(mappings)} slots:")
    for slot, paths in mappings.items():
        print(f"  {slot}: {len(paths)} file(s)")
    print()

    # 3. Create empty model
    model = DealModel(deal_id=DEAL_ID, tenant_id=TENANT_ID)

    # 4. Run extraction for each slot
    print("Running extraction...")
    results = {}

    for slot, paths in mappings.items():
        print(f"\n--- {slot.upper()} ---")
        for doc_path in paths:
            print(f"  Extracting: {Path(doc_path).name}")
            t0 = time.time()

            result = run_extraction_for_slot(slot, Path(doc_path), model)
            elapsed = time.time() - t0

            results[slot] = result

            if "error" in result:
                print(f"    ERROR: {result['error']}")
            else:
                print(f"    Extracted {result['field_count']} fields in {elapsed:.1f}s")
                if result["flags"]:
                    print(f"    Flags: {result['flags'][:3]}...")  # Show first 3

            # Save training example
            from creditpaper.extractors.builder import _INSTRUCTION as BUILDER_INSTRUCTION
            from creditpaper.extractors.building_contract import _INSTRUCTION as BC_INSTRUCTION
            from creditpaper.extractors.da import _INSTRUCTION as DA_INSTRUCTION
            from creditpaper.extractors.insurance import _INSTRUCTION as INS_INSTRUCTION
            from creditpaper.extractors.its import _INSTRUCTION as ITS_INSTRUCTION
            from creditpaper.extractors.qs import _INSTRUCTION as QS_INSTRUCTION
            from creditpaper.extractors.screening import _INSTRUCTION as KYC_INSTRUCTION
            from creditpaper.extractors.sponsor import _INSTRUCTION as SPONSOR_INSTRUCTION
            from creditpaper.extractors.valuation import _INSTRUCTION as VAL_INSTRUCTION

            prompts = {
                "its": ITS_INSTRUCTION,
                "valuation": VAL_INSTRUCTION,
                "qs": QS_INSTRUCTION,
                "da": DA_INSTRUCTION,
                "building_contract": BC_INSTRUCTION,
                "builder": BUILDER_INSTRUCTION,
                "insurance": INS_INSTRUCTION,
                "kyc": KYC_INSTRUCTION,
                "sponsor_financials": SPONSOR_INSTRUCTION,
            }

            instruction = prompts.get(slot, "")

            save_training_example(
                tenant_id=TENANT_ID,
                deal_id=DEAL_ID,
                document_id=f"{slot}:{Path(doc_path).name}",
                slot_key=slot,
                model_used="qwen2.5vl:7b",
                instruction_text=instruction,
                input_pdf_path=Path(doc_path),
                expected_output={},  # No ground truth yet
                actual_output=result.get("fields", {}),
                flags=result.get("flags", []),
                quality_score=None,  # To be scored later
                notes=f"Extraction test - {slot}",
            )

    # 5. Summary
    print("\n" + "=" * 60)
    print("EXTRACTION COMPLETE")
    print("=" * 60)
    print("\nResults summary:")
    for slot, result in results.items():
        if "error" in result:
            print(f"  {slot}: ERROR - {result['error'][:50]}")
        else:
            print(f"  {slot}: {result['field_count']} fields extracted")

    print("\nTraining data saved to DB.")
    print("Next steps:")
    print("1. Review extraction quality in DB")
    print("2. Score examples (quality_score)")
    print("3. Add expected_output (ground truth)")
    print("4. Update instruction prompts")
    print("5. Re-run and compare")


if __name__ == "__main__":
    main()
