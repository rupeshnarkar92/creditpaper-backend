"""python-docx renderer (Build Plan §9, D2). Consumes the deal model and the
Brand Profile. Deliberately plain construction for Canva import: proper heading
styles, flat tables, inline images; no text boxes, no nested tables, no
header/footer tricks. Citations resolved from model anchors. Also produces the
numbers audit view (every figure + source anchor; the AFSL artefact).
Lands in Wave 2."""
