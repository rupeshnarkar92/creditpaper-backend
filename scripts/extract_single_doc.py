r"""Extract from a single document and save training data.

Usage:
    python scripts/extract_single_doc.py <slot_key> <doc_path>

Example:
    python scripts/extract_single_doc.py its "D:\OSProjects\cp_docs\deal_files-20260910T041607Z-1-001\deal_files\Deal Documents\Term Sheet\20 Forest Knoll - Updated Executed ITS (Construction).pdf"
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from creditpaper.extractors.builder import BuilderExtractor
from creditpaper.extractors.building_contract import BuildingContractExtractor
from creditpaper.extractors.da import DaExtractor
from creditpaper.extractors.insurance import InsuranceExtractor
from creditpaper.extractors.its import ItsExtractor
from creditpaper.extractors.qs import QsExtractor
from creditpaper.extractors.screening import ScreeningExtractor
from creditpaper.extractors.sponsor import SponsorExtractor
from creditpaper.extractors.valuation import ValuationExtractor
from creditpaper.training.service import save_training_example

EXTRACTORS = {
    "its": ItsExtractor,
    "valuation": ValuationExtractor,
    "qs": QsExtractor,
    "da": DaExtractor,
    "building_contract": BuildingContractExtractor,
    "builder": BuilderExtractor,
    "insurance": InsuranceExtractor,
    "kyc": ScreeningExtractor,
    "sponsor_financials": SponsorExtractor,
    "sponsor_structure": SponsorExtractor,
}

DEAL_ID = "forest-knoll-2026"
TENANT_ID = "00000000-0000-0000-0000-000000000001"


def main():
    if len(sys.argv) != 3:
        print("Usage: python scripts/extract_single_doc.py <slot_key> <doc_path>")
        print(f"Available slots: {', '.join(EXTRACTORS.keys())}")
        sys.exit(1)

    slot_key = sys.argv[1]
    doc_path = Path(sys.argv[2])

    if slot_key not in EXTRACTORS:
        print(f"Unknown slot: {slot_key}")
        print(f"Available slots: {', '.join(EXTRACTORS.keys())}")
        sys.exit(1)

    if not doc_path.exists():
        print(f"File not found: {doc_path}")
        sys.exit(1)

    print(f"Extracting {slot_key} from: {doc_path.name}")
    print(f"File size: {doc_path.stat().st_size / 1024 / 1024:.1f} MB")
    print()

    # Run extraction
    extractor_class = EXTRACTORS[slot_key]
    extractor = extractor_class()

    doc_id = f"{slot_key}:{doc_path.name}"

    print("Starting extraction...")
    t0 = time.time()

    try:
        output = extractor.extract(doc_id, doc_path)
        elapsed = time.time() - t0

        print(f"Extraction completed in {elapsed:.1f}s")
        print(f"Fields extracted: {len(output.fields)}")
        print(f"Flags: {len(output.flags)}")

        # Print extracted fields
        print("\nExtracted fields:")
        for path, fact in sorted(output.fields.items()):
            print(f"  {path}: {fact.value}")

        # Print flags
        if output.flags:
            print("\nFlags:")
            for flag in output.flags:
                print(f"  - {flag}")

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
            "sponsor_structure": SPONSOR_INSTRUCTION,
        }

        instruction = prompts.get(slot_key, "")

        example_id = save_training_example(
            tenant_id=TENANT_ID,
            deal_id=DEAL_ID,
            document_id=doc_id,
            slot_key=slot_key,
            model_used=extractor._model_name or "qwen2.5vl:7b",
            instruction_text=instruction,
            input_pdf_path=doc_path,
            expected_output={},  # No ground truth yet
            actual_output={k: str(v.value) for k, v in output.fields.items()},
            flags=output.flags,
            quality_score=None,  # To be scored later
            notes=f"Single document extraction test - {slot_key}",
        )

        print(f"\nTraining example saved: {example_id}")

    except Exception as e:
        elapsed = time.time() - t0
        print(f"Extraction failed after {elapsed:.1f}s: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
