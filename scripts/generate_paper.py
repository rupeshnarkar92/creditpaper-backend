"""Full product flow on a deal (`--deal`): documents -> route -> ingest -> derive ->
generate (thesis + sections, with the claims-fidelity gate) -> render .docx — the
real path that replaces hand-curation, with the cost ledger open across BOTH phases.

Reports the per-deal COST OF SALE = ingestion $ + generation $, and the wall-clock
for each phase (ingestion and generation run as sequential phases; within each,
work is parallel). Generation runs off whatever the live extractors produced — with
only ITS + valuation wired today the borrower/QS/DA-dependent sections will lean on
the scaffold mechanism, and the WIP register shows what richer extractor coverage
would fill. Fixtures are tenant data (gitignored); nothing here is committed.

Run:  .venv/Scripts/python scripts/generate_paper.py
Output (gitignored): output/riverton_full.docx
"""

from __future__ import annotations

import re
import sys
from concurrent.futures import as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from creditpaper.harness.ingest import ingest  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from creditpaper.agents.proofread import Proofreader  # noqa: E402
from creditpaper.agents.review import ClaimsFidelityReviewer  # noqa: E402
from creditpaper.agents.section import PROSE_AGENTS  # noqa: E402
from creditpaper.agents.thesis import ThesisAgent  # noqa: E402
from creditpaper.concurrency import pool  # noqa: E402
from creditpaper.harness.route import route_deal  # noqa: E402
from creditpaper.model import DealModel  # noqa: E402
from creditpaper.model.apply import set_prose  # noqa: E402
from creditpaper.model.core import SectionStatus  # noqa: E402
from creditpaper.obs.ledger import cost_session  # noqa: E402
from creditpaper.profiles.house_style import seed_3capital_profile  # noqa: E402
from creditpaper.renderer.docx_render import render_deal  # noqa: E402

RUBRIC_DIR = ROOT / "docs/rubrics"
EXEMPLAR_SRC = ROOT / "fixtures/prior_papers/_extracted/Meridian - Sample St - Credit Paper.txt"

# Default deal is Riverton; pass --deal <dir> --deal-id <id> --out <name> to run any deal
# (the same pipeline must work on any deal — north star, not a Riverton generator).
_DEALS = {
    "riverton": ("fixtures/build_deal/deal_files", "riverton-kbl01", "output/riverton_full.docx"),
    "wattlefield": (
        "fixtures/blind/blind_01/deal_files",
        "wattlefield-wbr01",
        "output/wattlefield_full.docx",
    ),
}


def _deal_config() -> tuple[Path, str, Path]:
    if "--deal" in sys.argv:
        key = sys.argv[sys.argv.index("--deal") + 1]
        if key in _DEALS:
            d, did, out = _DEALS[key]
            return ROOT / d, did, ROOT / out
        did = sys.argv[sys.argv.index("--deal-id") + 1] if "--deal-id" in sys.argv else "deal"
        out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "output/deal_full.docx"
        return ROOT / key, did, ROOT / out
    d, did, out = _DEALS["riverton"]
    return ROOT / d, did, ROOT / out


BODY_SECTIONS = [2, 4, 5, 6, 7, 8]
TAIL_SECTIONS = [9]
RUBRIC_FILES = {
    2: "SECTION_2_DEAL_SUMMARY.md",
    4: "SECTION_4_ZONING_LOCATION.md",
    5: "SECTION_5_BORROWER_ANALYSIS.md",
    6: "SECTION_6_VALUATION_ANALYSIS.md",
    7: "SECTION_7_RISKS_MITIGATIONS.md",
    8: "SECTION_8_OTHER_CONSIDERATIONS.md",
    9: "SECTION_9_CONCLUSION_SCAFFOLD.md",
}
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


def main() -> int:
    load_dotenv(ROOT / ".env")
    deal_dir, deal_id, out_path = _deal_config()
    if not deal_dir.exists():
        print(f"missing deal dir: {deal_dir}")
        return 1

    # -- Phase 1: ingestion (documents -> typed model) -------------------------
    files = [str(p) for p in deal_dir.rglob("*.pdf")] + [str(p) for p in deal_dir.rglob("*.PDF")]
    files = sorted(set(files))
    mapping = route_deal(files)
    print(f"DEAL {deal_id} — {deal_dir.name}")
    model = DealModel(tenant_id="hyperion", deal_id=deal_id)
    print(f"INGEST — {len(files)} PDFs, routed {sorted(mapping)} …", flush=True)
    rep = ingest(model, mapping)
    print(
        f"  {len(rep.applied)} facts applied; reconciled={rep.reconciled}; "
        f"${rep.cost_usd:.4f}, {rep.wall_clock_s:.0f}s"
    )
    # Print the ingest ledger too, not just the total: the per-extractor "peak
    # out" figures are what size each extractor's max_tokens cap, and without
    # them a run tells you extraction didn't truncate but not how close it came.
    if rep.cost_summary:
        print(rep.cost_summary)
    if rep.reconciliation:
        for r in rep.reconciliation:
            print(f"    ! {r}")

    # -- Phase 2: generation (model -> prose), under its own ledger -------------
    profile = seed_3capital_profile()
    profile.section_exemplars = _exemplars()
    house_style = profile.model_dump()
    reviewer = ClaimsFidelityReviewer()

    with cost_session(deal_id=model.deal_id) as gen:
        print("GENERATE — thesis pre-pass …", flush=True)
        model.thesis = ThesisAgent().build(model, reviewer=reviewer)
        print(
            f"  thesis present={model.thesis.present}, {len(model.thesis.dominant_risks)} dominant risks",
            flush=True,
        )

        def _gen(section: int):
            agent = PROSE_AGENTS[section]()
            agent.rubric = (RUBRIC_DIR / RUBRIC_FILES[section]).read_text(encoding="utf-8")
            block, rev = agent.draft_and_review(
                model, findings=[], house_style=house_style, reviewer=reviewer
            )
            block.status = SectionStatus.DRAFT
            return section, block, rev

        def _record(section: int, block, rev) -> None:
            set_prose(model, section, block)
            flags = "clean" if rev.clean else f"{len(rev.findings)} fidelity flag(s) held"
            print(
                f"  section {section}: {len(block.text)} chars, "
                f"{len(block.cited_field_paths)} citations, {flags}",
                flush=True,
            )

        print(f"  drafting {len(BODY_SECTIONS)} body sections in parallel …", flush=True)
        with pool(6, thread_name_prefix="section") as ex:
            futs = {ex.submit(_gen, s): s for s in BODY_SECTIONS}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    _record(*fut.result())
                except Exception as exc:  # noqa: BLE001 — one section must not lose the run
                    print(f"  !! section {s} failed: {type(exc).__name__}: {str(exc)[:140]}")

        for s in TAIL_SECTIONS:
            print("  drafting conclusion (serial tail) …", flush=True)
            try:
                _record(*_gen(s))
            except Exception as exc:  # noqa: BLE001
                print(f"  !! section {s} failed: {type(exc).__name__}: {str(exc)[:140]}")

        # Whole-paper QC: spelling/grammar only, figures & citations protected.
        print("  proofreading (spelling/grammar QC) …", flush=True)
        pr = Proofreader().proofread(model.prose)
        # `rejected` and `failed` mean opposite things and must not be conflated in
        # the output: a rejection is the deterministic figure/citation guard doing
        # its job (good news); a failure means a section's QC call never ran at all.
        print(
            f"  proofread: {len(pr.applied)} fix(es) applied, {len(pr.rejected)} refused, "
            f"{len(pr.failed)} section(s) FAILED (unproofed)",
            flush=True,
        )
        for c in pr.applied:
            print(f"    §{c.section} [{c.kind}] {c.before!r} -> {c.after!r}")
        for r in pr.rejected:
            print(f"    {r}")
        for f in pr.failed:
            print(f"    !! §{f.section} FAILED proofread (unproofed): {f.error_type}: {f.message[:140]}")

    # -- Render (deterministic, no LLM) ----------------------------------------
    target = out_path
    try:
        out = render_deal(model, target, paper_title=profile.paper_title, house_style=profile.model_dump())
    except PermissionError:
        target = out_path.with_name(out_path.stem + "_new.docx")
        out = render_deal(model, target, paper_title=profile.paper_title, house_style=profile.model_dump())
    print(f"\nrendered: {out} ({out.stat().st_size:,} bytes); sections present {sorted(model.prose)}")

    # -- Cost of sale ----------------------------------------------------------
    total = rep.cost_usd + gen.total_usd
    wall = rep.wall_clock_s + gen.wall_clock_s
    print("\n" + "=" * 70)
    print(
        f"COST OF SALE ({deal_id}, this run): ${total:.4f}  |  wall-clock {wall:.0f}s (~{wall / 60:.1f} min)"
    )
    print(f"  ingestion:  ${rep.cost_usd:.4f}  /  {rep.wall_clock_s:.0f}s")
    print(f"  generation: ${gen.total_usd:.4f}  /  {gen.wall_clock_s:.0f}s")
    print("=" * 70)
    print(gen.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
