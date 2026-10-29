"""Build a small synthetic 2-page credit paper PDF for testing."""

import io
import sys

sys.path.insert(0, "src")

sys.path.insert(0, ".")
from tests.pdf_builder import Text, build_pdf  # noqa: E402

pages = []
for body in [
    "INTERNAL CREDIT PAPER\nAcme Capital - 14 Marlowe Crescent, Coogee NSW\n"
    "First Mortgage Residential Construction Facility\n\n"
    "Loan amount: $7,500,000\nSponsor equity: $7,000,000\nLVR: 45% against 45% ceiling.\n"
    "Approved by Credit Committee on 2026-06-15.\n\n"
    "The facility binds against the leverage covenant at funding with negligible headroom.",
    "SECURITY\nThe facility is secured by a first ranking mortgage over the subject site. "
    "General security agreement, assignment of contracts and insurances, and personal "
    "guarantees from the sponsors. Valuation by CBRE dated 2026-05-20, as-if-complete $16.7M.\n\n"
    "Risk mitigants: pre-sales at 80% of net sellable area, fixed-price construction contract, "
    "and parental guarantee from Acme Holdings Pty Ltd.",
]:
    pages.append([Text(72, 72, body)])

buf = io.BytesIO(build_pdf(*pages))

out = r"D:\OSProjects\creditpaper\output\smoke_small.pdf"
with open(out, "wb") as f:
    f.write(buf.getvalue())
print(f"wrote {len(buf.getvalue())} bytes to {out}")
