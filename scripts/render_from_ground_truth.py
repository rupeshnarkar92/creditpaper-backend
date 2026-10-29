"""Phase-0: populate a DealModel from curated ground truth, generate all prose
sections (constitution + claims-fidelity review), and render the full Internal
Credit Memorandum to a real Word .docx with provenance.

SCOPE: build-deal only — the ground-truth path is fixed at
`fixtures/build_deal/ground_truth.json`, matching the Riverton-only registry in
`harness/render_reviewed.py`. Widening both to a shared deal registry is the same
one-line change in each place, once a second deal has curated ground truth.

Resumable: generated sections are cached to output/riverton_prose.json so a
re-run only generates what's missing (saves cost / survives timeouts).
Pass --fresh to regenerate everything.

Run:  .venv\\Scripts\\python scripts\\render_from_ground_truth.py
Output (gitignored): output/riverton_slice.docx
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402

from creditpaper.agents.review import ClaimsFidelityReviewer  # noqa: E402
from creditpaper.agents.section import PROSE_AGENTS  # noqa: E402
from creditpaper.agents.thesis import ThesisAgent  # noqa: E402
from creditpaper.concurrency import pool  # noqa: E402
from creditpaper.harness.populate import populate_from_ground_truth  # noqa: E402
from creditpaper.model.apply import set_prose  # noqa: E402
from creditpaper.model.core import ProseBlock, SectionStatus  # noqa: E402
from creditpaper.model.deal import ThesisSpine  # noqa: E402
from creditpaper.obs.ledger import cost_session  # noqa: E402
from creditpaper.profiles.house_style import seed_3capital_profile  # noqa: E402
from creditpaper.renderer.docx_render import render_deal  # noqa: E402

# Body sections draft in parallel; §9 is the serial synthesis tail (§1/§3 are tables).
BODY_SECTIONS = [2, 4, 5, 6, 7, 8]
TAIL_SECTIONS = [9]

ROOT = Path(__file__).resolve().parents[1]
GT = ROOT / "fixtures" / "build_deal" / "ground_truth.json"
RUBRIC_DIR = ROOT / "docs" / "rubrics"
EXEMPLAR_SRC = ROOT / "fixtures" / "prior_papers" / "_extracted" / "Meridian - Sample St - Credit Paper.txt"
OUT = ROOT / "output" / "riverton_slice.docx"
CACHE = ROOT / "output" / "riverton_prose.json"
THESIS_CACHE = ROOT / "output" / "riverton_thesis.json"
REVIEW_FLAGS = ROOT / "output" / "review_flags.json"  # fidelity gate findings, held for human

RUBRIC_FILES = {
    2: "SECTION_2_DEAL_SUMMARY.md",
    4: "SECTION_4_ZONING_LOCATION.md",
    5: "SECTION_5_BORROWER_ANALYSIS.md",
    6: "SECTION_6_VALUATION_ANALYSIS.md",
    7: "SECTION_7_RISKS_MITIGATIONS.md",
    8: "SECTION_8_OTHER_CONSIDERATIONS.md",
    9: "SECTION_9_CONCLUSION_SCAFFOLD.md",
}
# Heading text in prior papers to slice a form exemplar per section.
EXEMPLAR_HEADINGS = {
    2: "DEAL SUMMARY",
    4: "ZONING",
    5: "BORROWER ANALYSIS",
    6: "VALUATION ANALYSIS",
    7: "RISK",
    8: "OTHER CONSIDERATIONS",
}


def _exemplars() -> dict[int, str]:
    out: dict[int, str] = {}
    if not EXEMPLAR_SRC.exists():
        return out
    t = EXEMPLAR_SRC.read_text(encoding="utf-8")
    for section, heading in EXEMPLAR_HEADINGS.items():
        m = re.search(heading, t, re.IGNORECASE)
        if m:
            out[section] = t[m.end() : m.end() + 1600]
    return out


def _load_cache() -> dict[int, ProseBlock]:
    if not CACHE.exists():
        return {}
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    return {int(k): ProseBlock(**v) for k, v in data.items()}


def _save_cache(prose: dict[int, ProseBlock]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(
        json.dumps({str(s): b.model_dump() for s, b in prose.items()}, indent=2, default=str),
        encoding="utf-8",
    )


def main() -> None:
    # A six-wide section pool of Opus calls ran outside any session, so the run
    # that the product's per-paper cost was estimated from reported $0.00.
    with cost_session(deal_id="ground-truth") as led:
        _main()
    print()
    print(led.summary())


def _main() -> None:
    load_dotenv(ROOT / ".env")
    fresh = "--fresh" in sys.argv
    out_override = None
    if "--out" in sys.argv:
        out_override = OUT.with_name(sys.argv[sys.argv.index("--out") + 1])

    model, report = populate_from_ground_truth(GT)
    print(
        f"populated {len(report.applied)} facts; derived {len(report.derived)}; "
        f"{'reconciles' if not report.inconsistent else 'INCONSISTENT'}"
    )

    profile = seed_3capital_profile()
    profile.section_exemplars = _exemplars()
    playbook_cache = ROOT / "output" / "house_playbooks.json"
    if playbook_cache.exists():
        books = json.loads(playbook_cache.read_text(encoding="utf-8"))
        profile.section_playbooks = {int(k): v for k, v in books.items()}
        print(f"loaded {len(profile.section_playbooks)} house playbooks")
    house_style = profile.model_dump()
    reviewer = ClaimsFidelityReviewer()

    # Thesis pre-pass (serial): the shared spine every section inherits (Phase A).
    if fresh or not THESIS_CACHE.exists():
        print("thesis pre-pass...", flush=True)
        model.thesis = ThesisAgent().build(model, reviewer=reviewer)
        THESIS_CACHE.write_text(model.thesis.model_dump_json(indent=2), encoding="utf-8")
    else:
        model.thesis = ThesisSpine(**json.loads(THESIS_CACHE.read_text(encoding="utf-8")))
    print(f"thesis: present={model.thesis.present}, {len(model.thesis.dominant_risks)} dominant risks")

    cache = {} if fresh else _load_cache()
    for section, block in cache.items():
        set_prose(model, section, block)

    def _gen(section: int):
        agent = PROSE_AGENTS[section]()
        agent.rubric = (RUBRIC_DIR / RUBRIC_FILES[section]).read_text(encoding="utf-8")
        block, rep = agent.draft_and_review(model, findings=[], house_style=house_style, reviewer=reviewer)
        block.status = SectionStatus.DRAFT
        return section, block, rep

    review_flags: dict[int, list[dict]] = {}

    def _record(section: int, block, rep) -> None:
        set_prose(model, section, block)
        cache[section] = block
        review_flags[section] = [
            {"category": f.category, "quote": f.quote, "explanation": f.explanation} for f in rep.findings
        ]
        flags = "clean" if rep.clean else f"{len(rep.findings)} fidelity flag(s) held"
        print(
            f"  section {section}: {len(block.text)} chars, "
            f"{len(block.cited_field_paths)} citations, {flags}",
            flush=True,
        )

    # Body sections (§2,4,5,6,7,8) are independent given the shared model + thesis —
    # draft them in PARALLEL. §1/§3 are deterministic tables (rendered, not generated).
    todo = [s for s in BODY_SECTIONS if s not in cache]
    if todo:
        print(f"drafting {len(todo)} body sections in parallel...", flush=True)
        with pool(min(6, len(todo)), thread_name_prefix="section") as ex:
            futs = {ex.submit(_gen, s): s for s in todo}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    _record(*fut.result())
                except Exception as exc:  # noqa: BLE001 — one section must not lose the run
                    print(f"  !! section {s} failed: {type(exc).__name__}: {str(exc)[:120]}")
        _save_cache(cache)

    # §9 Conclusion is the serial tail — it synthesises the thesis (and the model).
    for s in TAIL_SECTIONS:
        if s in cache:
            continue
        print("drafting conclusion (serial tail)...", flush=True)
        try:
            _record(*_gen(s))
            _save_cache(cache)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! section {s} failed: {type(exc).__name__}: {str(exc)[:120]}")

    if review_flags:
        REVIEW_FLAGS.write_text(
            json.dumps({str(k): v for k, v in review_flags.items()}, indent=2), encoding="utf-8"
        )
        total = sum(len(v) for v in review_flags.values())
        print(f"held {total} fidelity flag(s) for human review -> {REVIEW_FLAGS.name}")

    target = out_override or OUT
    try:
        out = render_deal(model, target, paper_title=profile.paper_title, house_style=profile.model_dump())
    except PermissionError:
        target = OUT.with_name(OUT.stem + "_new.docx")
        print(f"\n(!) {OUT.name} is open/locked — writing to {target.name} instead")
        out = render_deal(model, target, paper_title=profile.paper_title, house_style=profile.model_dump())
    print(f"\nrendered full paper: {out} ({out.stat().st_size:,} bytes)")
    print("sections present:", sorted(model.prose), "(missing = placeholders in the doc)")


if __name__ == "__main__":
    main()
