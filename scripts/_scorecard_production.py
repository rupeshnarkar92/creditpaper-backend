"""Production-faithful pieces of the corpus scorecard.

The first corpus scorecard applied extraction outputs to an in-memory model.
Production does not: each document has a confirmed slot (D3), `commit_outputs`
ranks sources by that slot and holds a material contradiction out of the paper,
and generation reads `load_model`. Scored in memory, whichever document came last
won a shared field, which is how both models "passed" the same wrong facility
amount. These helpers reproduce the production path on the local database:

- `assign_slots`: the production filename/first-page matcher (`slot_matcher.match_file`)
  over the deal type's slots, and the refused classes (D7) production never reads;
- `seed_ledger`: a deal, its documents and versions, and confirmed slot assignments;
- `commit_and_load`: `commit_outputs`, then `load_model`, plus the paths held by an
  open contradiction;
- `score`: the answer key against the loaded model, with collection rows matched by
  value when their key differs, and fields no document holds kept apart;
- `paper_recall`: the share of the signed credit paper's figures found among the
  extracted facts, a path-free completeness measure over hundreds of figures.

Local test database only (DATABASE_URL). Prints nothing; returns counts.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

# Answer-key fields no deal document states: entered by the analyst when the deal is
# created (docs/FIELD_DICTIONARY.md: deal block, HUMAN_ENTRY). Scored apart, never "missing".
NOT_FROM_DOCUMENTS = frozenset({"deal.expected_funding_date"})
ACTOR = "00000000-0000-0000-0000-0000000abc01"


def assign_slots(
    files: Iterable[Path], pages: Mapping[Path, list[dict[str, Any]]], deal_type: str
) -> tuple[dict[Path, str | None], set[Path]]:
    """Each file's slot as production's matcher proposes it, and the files a refused class excludes."""
    from creditpaper.deals.services import slot_matcher
    from creditpaper.deals.services.slot_board import _refused, _slot_types
    from creditpaper.deals.services.slot_taxonomy import slots_for

    slots = slots_for(deal_type or "construction", _slot_types())
    refused_classes = _refused()
    table = slot_matcher.keyword_table()
    assigned: dict[Path, str | None] = {}
    refused: set[Path] = set()
    for path in files:
        names = [path.name, path.stem]
        if slot_matcher.refused_match(names, refused_classes) is not None:
            refused.add(path)
            continue
        first = {int(p["page"]): str(p.get("text") or "") for p in pages.get(path, [])[:3]}
        assigned[path] = slot_matcher.match_file(names, None, slots, table, first_pages=first).slot_key
    return assigned, refused


def seed_ledger(
    tenant_id: str, slot_of: Mapping[Path, str | None]
) -> tuple[str, dict[Path, tuple[str, str]]]:
    """A fresh deal with one document and version per file, and its confirmed slot assignments."""
    from creditpaper.db import cursor

    deal_id = str(uuid.uuid4())
    ids: dict[Path, tuple[str, str]] = {}
    with cursor() as cur:
        cur.execute(
            """INSERT INTO deals (id, tenant_id, name, borrower, deal_type, stage, status, facility_amount,
            document_count, document_required, current_state, state_changed_at, state_changed_by,
            workspace_revision) VALUES (%s, %s, 'Scorecard', 'Scorecard', 'construction', 'draft', 'draft',
            0, 0, 25, 'DRAFT', now(), %s, 0)""",
            (deal_id, tenant_id, ACTOR),
        )
        cur.execute("INSERT INTO deal_workspaces (deal_id) VALUES (%s) ON CONFLICT DO NOTHING", (deal_id,))
        slot_ids: dict[str, str] = {}
        for path, slot in slot_of.items():
            doc, version = str(uuid.uuid4()), str(uuid.uuid4())
            ids[path] = (doc, version)
            cur.execute(
                """INSERT INTO deal_documents(id,tenant_id,deal_id,display_name,created_by)
                VALUES(%s,%s,%s,'Scorecard document',%s)""",
                (doc, tenant_id, deal_id, ACTOR),
            )
            cur.execute(
                """INSERT INTO deal_document_versions(id,tenant_id,deal_id,document_id,version_number,
                original_filename,mime_type,size_bytes,content_sha256,storage_path,uploaded_by,uploaded_at,
                processing_status,extraction_status)
                VALUES(%s,%s,%s,%s,1,'document.pdf','application/pdf',1,%s,'unused',%s,now(),'ready','ready')""",
                (version, tenant_id, deal_id, doc, version, ACTOR),
            )
            if slot is None:
                continue
            if slot not in slot_ids:
                cur.execute(
                    """INSERT INTO document_slots(tenant_id,deal_id,slot_key,status) VALUES(%s,%s,%s,'confirmed')
                    RETURNING id""",
                    (tenant_id, deal_id, slot),
                )
                slot_ids[slot] = str(cur.fetchone()[0])
            cur.execute(
                """INSERT INTO document_slot_files(tenant_id,slot_id,document_id,version_id,role)
                VALUES(%s,%s,%s,%s,'assigned')""",
                (tenant_id, slot_ids[slot], doc, version),
            )
    return deal_id, ids


def commit_and_load(tenant_id: str, deal_id: str, outputs: list[Any]) -> tuple[Any, set[str]]:
    """Commit as production does; the model generation would read, and the paths an open contradiction holds."""
    from creditpaper.ledger import contradictions, service

    service.commit_outputs(tenant_id, deal_id, outputs)
    model = service.load_model(tenant_id, deal_id)
    held = {
        row.concept_path for row in contradictions.list_contradictions(tenant_id, deal_id) if row.blocking
    }
    return model, held


def _key(value: object) -> str:
    from creditpaper.ingest.consensus import _key as consensus_key

    return consensus_key(value)


def _declared_kind(path: str) -> str | None:
    """The kind the field reference declares for a path (number, date, text, bool)."""
    from functools import cache

    @cache
    def kinds() -> dict[str, str]:
        from creditpaper.ingest.harvest_prompt import field_kinds
        from creditpaper.paths import field_reference

        return field_kinds(field_reference().read_text(encoding="utf-8"))

    from creditpaper.ingest.harvest_prompt import normalise_key_path

    return kinds().get(normalise_key_path(path))


def same_figure(expected: object, extracted: object) -> bool:
    """Equal, or equal to the answer key's ROUNDING: a credit paper writes "$16.70m" where the
    valuation states 16,701,266. An expected figure ending in k >= 2 zeros matches any figure
    within half of 10^k (Track 1: reconciled or explained, D11). Text is compared as written."""
    from decimal import Decimal, InvalidOperation

    if _key(expected) == _key(extracted):
        return True
    try:
        e = Decimal(_key(expected))
        a = Decimal(_key(extracted))
    except (InvalidOperation, ValueError):
        return False
    if e == 0 or e != e.to_integral_value():
        return False
    digits = str(int(abs(e)))
    zeros = len(digits) - len(digits.rstrip("0"))
    return zeros >= 2 and abs(a - e) <= Decimal(10) ** zeros / 2


def _collection_values(model: Any, keyed_path: str) -> list[Any]:
    """Every fact at a keyed path, whatever its key: `presales[lot_4].price` -> all presale prices."""
    from creditpaper.model.citations import resolve_path
    from creditpaper.model.core import Fact

    match = re.match(r"^(.*?)\[[^\]]+\](.*)$", keyed_path)
    if not match:
        return []
    collection = resolve_path(model, match.group(1))
    if not isinstance(collection, dict):
        return []
    rest = match.group(2).lstrip(".")
    out: list[Any] = []
    for item in collection.values():
        node = item
        for part in [p for p in rest.split(".") if p]:
            node = getattr(node, part, None)
        if isinstance(node, Fact) and node.value is not None:
            out.append(node)
        elif isinstance(item, Fact) and not rest and item.value is not None:
            out.append(item)
    return out


def stated_in_documents(expected: object, document_figures: set[str] | None) -> bool:
    """Whether any document of the deal states the expected figure (rounding allowed). Text is assumed stated."""
    from creditpaper.ingest.values import figures

    if document_figures is None:
        return True
    wanted = [f.value for f in figures(str(expected))]
    if not wanted:
        return True
    return any(same_figure(w, d) for w in wanted for d in document_figures)


def document_figures(texts: Iterable[str]) -> set[str]:
    """Every figure the deal's documents state, canonical form."""
    from creditpaper.ingest.values import figures

    return {f.value for text in texts for f in figures(text)}


def score(model: Any, gt: Any, held: set[str], doc_figures: set[str] | None = None) -> list[dict[str, Any]]:
    """Each answer-key field against the loaded model, by production's gate.

    An expected figure no document of the deal states (a paper's approved figure
    that differs from the term sheet, a computed one) is `not_in_documents`: no
    extraction could find it, so it is neither missing nor wrong."""
    from creditpaper.config import EXTRACTION_CONFIDENCE_THRESHOLD
    from creditpaper.harness.reconcile import Verdict, reconcile
    from creditpaper.model.citations import resolve_path
    from creditpaper.model.concepts import concept_for
    from creditpaper.model.core import ExtractionMethod, Fact, FactStatus

    def gate(fact: Any) -> bool:
        return (
            fact.status in (FactStatus.EXTRACTED, FactStatus.CONFIRMED)
            and (fact.confidence or 0.0) >= EXTRACTION_CONFIDENCE_THRESHOLD
        )

    rows: list[dict[str, Any]] = []
    for line in reconcile(model, gt).lines:
        if line.verdict is Verdict.UNMODELLED:
            continue
        concept = concept_for(line.path)
        row: dict[str, Any] = {
            "path": line.path,
            "material": bool(concept and concept.material),
            "expected": line.expected,
            "extracted": line.actual,
        }
        correct = line.verdict in (Verdict.MATCH, Verdict.EXPLAINED)
        fact = resolve_path(model, line.path)
        if line.path in NOT_FROM_DOCUMENTS:
            row["category"] = "not_from_documents"
        elif not correct and not stated_in_documents(line.expected, doc_figures):
            row["category"] = "not_in_documents"
        elif line.actual is None or not isinstance(fact, Fact):
            same = [f for f in _collection_values(model, line.path) if same_figure(line.expected, f.value)]
            if same:  # the row is there under another key
                row["category"] = "correct_verified" if gate(same[0]) else "false_flag"
                row["matched_by_value"] = True
            elif concept is not None and concept.path in held or line.path in held:
                row["category"] = "held"
            else:
                row["category"] = "missing_explained" if correct else "missing"
        elif fact.provenance is not None and fact.provenance.method is ExtractionMethod.DERIVED:
            row["category"] = "derived"
        elif correct or same_figure(line.expected, line.actual):
            row["category"] = "correct_verified" if gate(fact) else "false_flag"
            if not correct:
                row["rounded"] = True
        elif isinstance(fact.value, str) and (
            _declared_kind(line.path) == "text" or not re.search(r"\d", str(line.expected))
        ):
            # Prose against prose (a text field, or wording with no figure): different
            # wording is not a wrong fact. Judged by a person.
            row["category"] = "text_differs"
        else:
            row["category"] = "FALSE_PASS" if gate(fact) else "catch"
        rows.append(row)
    return rows


_FIGURE = re.compile(r"(?:\$\s?)?\d[\d,]*(?:\.\d+)?\s?(?:%|m\b|k\b)?", re.IGNORECASE)


def paper_recall(paper_text: str, model: Any) -> dict[str, int]:
    """How many of the signed paper's distinct figures (4+ significant digits, or percentages) the facts hold.

    Path-free on purpose: it asks "did ingestion find the numbers the real paper used",
    which a 15-field answer key cannot. Derived figures (an LVR the paper computed)
    are in the paper and not in any document, so 100% is not the ceiling; it compares runs."""
    from creditpaper.ingest.values import numeric_tokens
    from creditpaper.model.evidence import model_facts

    def tokens(text: str) -> set[str]:
        out = set()
        for raw in _FIGURE.findall(text):
            for tok in numeric_tokens(raw):
                digits = re.sub(r"\D", "", tok)
                if len(digits) >= 4 or "%" in raw:
                    out.add(tok)
        return out

    wanted = tokens(paper_text)
    found: set[str] = set()
    for fact in model_facts(model):
        if fact.value is not None:
            found |= tokens(str(fact.value))
    return {"paper_figures": len(wanted), "found": len(wanted & found)}
