"""Distil the per-section 'house playbook' from a firm's prior approved papers (D10).

The corpus is whatever this directory holds; the BANKED size is **~3 papers plus the
website, not 15** (`DECISIONS.md` 2026-06-16 — "learned once from ~3 papers + the
website (NOT 15)"). This docstring said 15 for two months after that ruling. It
matters because `slice_paper_sections` tolerates a missed heading on the assumption
of a large corpus, and three papers is not a large corpus.

Reads fixtures/prior_papers/_extracted/*.txt, EXCLUDING the build deal and the three
blind validation deals (no Track-2 contamination), distils a playbook per prose
section, and caches them to output/house_playbooks.json (gitignored, tenant-derived).

`learn_playbooks` already takes `papers` as an argument rather than reading the disk,
so pointing it at a tenant's uploaded papers is a caller change, not a rewrite. This
script is the local harness; onboarding is the other caller.

Run:  .venv\\Scripts\\python scripts\\learn_house_style.py
Re-run is cheap to skip with the cache; pass --relearn to rebuild.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402

from creditpaper.harness.learn_house_style import PLAYBOOK_SECTIONS, learn_playbooks  # noqa: E402
from creditpaper.obs.ledger import cost_session  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "fixtures" / "prior_papers" / "_extracted"
CACHE = ROOT / "output" / "house_playbooks.json"

# Never let the build deal or a blind validation deal into the learning corpus
# (Track-2 contamination). The exclusion list names real deals, so it lives with the
# fixtures it matches — gitignored — rather than in source. One case-insensitive
# substring per line, matched against the file stem; blank lines and `#` comments
# ignored.
#
# This file is REQUIRED. A missing or empty list aborts rather than learning from
# everything: the failure mode it guards against is silent (a build-deal paper in the
# style corpus contaminates Track 2 invisibly), so absence must be loud.
EXCLUDE_FILE = ROOT / "fixtures" / "corpus_exclude.txt"


def _exclusions() -> tuple[str, ...]:
    if not EXCLUDE_FILE.exists():
        raise SystemExit(
            f"missing {EXCLUDE_FILE.relative_to(ROOT)} — the corpus exclusion list.\n"
            "It names the build deal and the blind validation deals, so it is gitignored\n"
            "with the fixtures. Create it with one case-insensitive substring per line\n"
            "matching the file stems that must never enter the style corpus.\n"
            "Refusing to learn: an un-excluded validation deal contaminates Track 2 silently."
        )
    lines = [
        s.strip().lower()
        for s in EXCLUDE_FILE.read_text(encoding="utf-8").splitlines()
        if s.strip() and not s.lstrip().startswith("#")
    ]
    if not lines:
        raise SystemExit(f"{EXCLUDE_FILE.relative_to(ROOT)} is empty — refusing to learn (see above).")
    return tuple(lines)


def load_corpus() -> dict[str, str]:
    exclude = _exclusions()
    papers: dict[str, str] = {}
    for txt in sorted(CORPUS.glob("*.txt")):
        if any(bad in txt.stem.lower() for bad in exclude):
            print(f"  excluded from corpus (validation/build deal): {txt.stem}")
            continue
        papers[txt.stem] = txt.read_text(encoding="utf-8", errors="ignore")
    return papers


def main() -> None:
    load_dotenv(ROOT / ".env")
    if CACHE.exists() and "--relearn" not in sys.argv:
        print(f"cache present ({CACHE.name}); pass --relearn to rebuild")
        return
    papers = load_corpus()
    print(f"learning from {len(papers)} prior papers...")
    # This distils one Opus call per section at 16k max_tokens. It ran with no
    # session open, so all of it reported as $0.00 in every cost figure the
    # per-paper price was reasoned from.
    with cost_session(deal_id="house-style") as led:
        books = learn_playbooks(papers)
    print(led.summary())
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({str(k): v for k, v in books.items()}, indent=2), encoding="utf-8")
    for section, text in sorted(books.items()):
        print(f"  section {section}: {len(text)} chars distilled")

    # Report what did NOT distil. A section whose heading none of the papers matched
    # yields no slices, and `learn_playbooks` drops it without comment — invisible at
    # fifteen papers, likely at three. Silence here reads as success and ships a
    # playbook with holes in it.
    missing = [s for s in PLAYBOOK_SECTIONS if s not in books]
    if missing:
        print(
            f"\n  !! NO PLAYBOOK for section(s) {missing} — no paper in the corpus had a\n"
            f"     heading matching SECTION_KEYWORDS, so nothing was learned for them.\n"
            f"     Those sections will draft on the base profile alone. Check the papers'\n"
            f"     heading wording before trusting the result."
        )
    print(f"\nwrote {CACHE} ({len(books)} of {len(PLAYBOOK_SECTIONS)} playbooks)")


if __name__ == "__main__":
    main()
