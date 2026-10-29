# extractors

One extractor per document type. Each extracts typed `Fact` values with provenance anchors.

## What it does
- Reads a deal document (PDF, DOCX, etc.)
- Returns an `ExtractionOutput` with facts, anchors, and confidence
- Never writes to the `DealModel` — that's `model/apply.py`'s job

## One file per document type
- `da.py` — Directors' Agreement
- `qs.py` — Quantity Surveyor report
- `its.py` — Indicative Term Sheet
- `valuation.py` — Valuation report
- `building_contract.py` — Building contract
- `screening.py` — KYC screening results
- `sponsor.py` — Sponsor / borrower info
- `insurance.py` — Insurance certificate

## Rules (D4)
- Two-pass mandatory: structural pass → targeted extraction
- Provenance anchors at parse time. No anchor, no field.
- Extractors emit `ExtractionOutput`; they never touch the model.

## Fixtures
Each extractor ships with fixtures from real deal files (gitignored, never committed).

## Owner
Engine team, #engine channel.
