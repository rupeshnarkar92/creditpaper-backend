"""Where the engine writes. One definition, resolved from configuration.

`Path(__file__).resolve().parents[3] / "output"` was written out three times —
in `api/deals.py`, `harness/render_reviewed.py` and `harness/signal_store.py` —
and every copy assumed the package sits in a `src/` checkout. Installed as a
wheel, `parents[3]` resolves inside `site-packages`, so the app would write deal
artefacts next to the library. That breaks precisely when the platform team
installs the package rather than running it from the repo, which is the handover
scenario.

`CREDITPAPER_OUTPUT_DIR` overrides; the default is `./output` relative to the
working directory, which is what a CLI run from the repo root already expects.
Resolved per call rather than captured at import so a test or a worker can point
it elsewhere without reloading modules.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_VAR = "CREDITPAPER_OUTPUT_DIR"


def output_dir() -> Path:
    """The directory for generated artefacts: prose caches, signals, papers."""
    configured = os.getenv(_ENV_VAR)
    return Path(configured) if configured else Path.cwd() / "output"


def ensure_output_dir(*parts: str) -> Path:
    """`output_dir()/parts`, created if absent. Returns the directory."""
    path = output_dir().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def repo_root() -> Path:
    """The source checkout root — for inputs that only exist in a checkout.

    Distinct from `output_dir()` on purpose. Curated ground truth lives under
    gitignored `fixtures/`, so anything reading it is inherently checkout-only and
    cannot work from an installed wheel. Saying that here is more honest than a
    `parents[3]` buried in a module that reads as though it works anywhere.
    """
    return Path(__file__).resolve().parents[2]


def field_reference() -> Path:
    """The generated field reference — the corpus prompt's allowlist.

    A FUNCTION, not a module constant, for the reason `repo_root` is: it is
    generated into the checkout by `scripts/gen_field_reference.py` and does
    not ship in a wheel, so resolving it at import time would make importing
    this module fail differently depending on where it was installed.

    Every path the deal model offers, with its declared type. The corpus path
    builds its system prompt from this file AND parses returned values to the
    types it declares, so the prompt and the parse cannot disagree.
    """
    return repo_root() / "docs" / "FIELD_REFERENCE.generated.md"
