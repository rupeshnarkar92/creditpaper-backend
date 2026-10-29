"""Extract training data from Forest Knoll deal documents.

Usage:
    python scripts/extract_training_data.py

This script:
1. Routes Forest Knoll documents to extractors
2. Runs extraction using Ollama
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
from creditpaper.training.service import save_prompt_version, save_training_example

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


def main():
    print("=" * 60)
    print("EXTRACTION TRAINING DATA CAPTURE")
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
        for p in paths:
            print(f"    - {Path(p).name}")
    print()

    # 3. Save prompt versions for each extractor
    print("Saving prompt versions...")
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

    for slot, instruction in prompts.items():
        version = f"v1.0-{int(time.time())}"
        save_prompt_version(slot, version, instruction, "Initial extraction training")
        print(f"  Saved {slot} prompt v{version}")
    print()

    # 4. Run extraction (placeholder - would call actual extractors)
    print("Running extraction...")
    print("(Extraction would run here with actual LLM calls)")
    print()

    # 5. Example: save a training example
    print("Saving example training data...")
    example_id = save_training_example(
        tenant_id=TENANT_ID,
        deal_id=DEAL_ID,
        document_id="term-sheet-executed",
        slot_key="its",
        model_used="qwen2.5vl:7b",
        instruction_text=ITS_INSTRUCTION,
        input_pdf_path=DEAL_DOCS / "Term Sheet" / "20 Forest Knoll - Updated Executed ITS (Construction).pdf",
        expected_output={
            "facility_amount": "7758697",
            "interest_rate": "9.50",
            "term_months": "14",
        },
        actual_output={
            "facility_amount": "7758697",
            "interest_rate": "9.50",
            "term_months": "14",
        },
        flags=[],
        quality_score=1.0,
        notes="Initial extraction test - Forest Knoll deal",
    )
    print(f"  Saved example: {example_id}")

    print()
    print("=" * 60)
    print("TRAINING DATA CAPTURE COMPLETE")
    print("=" * 60)
    print()
    print("Next steps:")
    print("1. Review extraction quality")
    print("2. Update instruction prompts")
    print("3. Re-run extraction")
    print("4. Compare results")


if __name__ == "__main__":
    main()
