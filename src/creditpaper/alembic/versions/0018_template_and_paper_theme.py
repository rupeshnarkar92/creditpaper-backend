"""platform defaults, deal template columns, paper types aligned to deal types.

Revision ID: 0018_template_and_paper_theme
Revises: 0017_billing

Spec: docs/superpowers/specs/2026-09-15-deal-template-and-paper-theme-design.md §1, §2.

ADDITIVE ONLY. Nothing here rewrites an existing record:
  - `platform_defaults` (new table, platform data, not tenant data) seeded with one
    current row per kind: the Creditpaper Standard template, the default paper theme
    and the standard voice. Runtime fallbacks read these rows instead of constants.
  - `deals.template_id` / `template_source` / `template_snapshot`: new nullable
    columns, no backfill. A deal without a snapshot resolves its template at read time.
  - `paper_types`: adds `residual_stock` and marks `term_loan`, `acquisition` and
    `refinance` inactive (the one allowed update, on the platform seed table only).
    Templates that already carry those values keep them.

The seeded payloads contain no firm names.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_template_and_paper_theme"
down_revision: str | None = "0017_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _section(number: int, name: str, subsections: list[str]) -> dict[str, Any]:
    return {
        "number": f"{number:02d}",
        "name": name,
        # The engine section this heading is written by. Hidden from the analyst;
        # the paper-sections API joins generated output onto the template by it.
        "engine_section": number,
        "subsections": [{"number": f"{number}.{i}", "name": s} for i, s in enumerate(subsections, start=1)],
    }


STANDARD_TEMPLATE: dict[str, Any] = {
    "name": "Creditpaper Standard",
    "sections": [
        _section(1, "Deal Snapshot", ["Key Terms", "Facility Summary"]),
        _section(
            2,
            "Deal Summary",
            ["Funding Overview", "Project Sponsors", "Project Status", "Quantity Surveyor", "Exit Strategy"],
        ),
        _section(3, "Funding Snapshot", ["Funding Table"]),
        _section(4, "Zoning & Location", ["Location & Size", "Site History and Approvals", "Planning"]),
        _section(
            5,
            "Borrower Analysis",
            ["Guarantors / Project Sponsors", "Ownership Structure", "Sponsor Financial Position"],
        ),
        _section(
            6,
            "Valuation Analysis",
            [
                "Project Description",
                "Market Overview",
                "'As Is' Market Value",
                "'As If Complete' Market Value",
            ],
        ),
        _section(7, "Risks & Mitigations", ["Key Risks", "Mitigants"]),
        _section(
            8,
            "Other Considerations",
            [
                "Security Structure",
                "Proposed Covenants",
                "AML & KYC Conclusions",
                "Environmental Considerations",
            ],
        ),
        _section(9, "Conclusion", ["Credit Thesis", "Recommendation"]),
    ],
}

DEFAULT_PAPER_THEME: dict[str, Any] = {
    "fonts": {
        "heading": {"family": "Source Serif 4"},
        "body": {"family": "Source Sans 3"},
    },
    # The pair a scraped font falls back to when it is not in the self-hosted set,
    # chosen by the scraped font's category.
    "font_defaults": {"serif": "Source Serif 4", "sans": "Source Sans 3"},
    "colours": {
        "heading": "#1F3A5F",
        "accent": "#2F6690",
        "text": "#1A1A1A",
        "muted": "#595959",
        "rule": "#D0D5DB",
    },
    "typography": {
        "cover_title_pt": 26,
        "h1_pt": 16,
        "h2_pt": 12.5,
        "h3_pt": 11,
        "body_pt": 10.5,
        "line_height": 1.45,
        "table_pt": 9.5,
        "caption_pt": 8.5,
        "heading_weight": 600,
    },
    "page": {
        "size": "A4",
        "margin_mm": 20,
        "running_header": True,
        "page_numbers": True,
        "footer_note": "Confidential",
    },
}

STANDARD_VOICE: dict[str, Any] = {
    "placeholder": True,
    "register": "formal; third person",
    "tense": "present for current state, past for completed diligence",
    "sentences": "plain and declarative",
    "tone": "neutral; findings, not enthusiasm",
    "note": "Recorded for later use by generation. Not consumed in this build.",
}

_SEEDS: list[tuple[str, dict[str, Any]]] = [
    ("template", STANDARD_TEMPLATE),
    ("paper_theme", DEFAULT_PAPER_THEME),
    ("voice", STANDARD_VOICE),
]


def upgrade() -> None:
    op.create_table(
        "platform_defaults",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('template', 'paper_theme', 'voice')", name="ck_platform_defaults_kind"),
        sa.UniqueConstraint("kind", "version", name="uq_platform_defaults_kind_version"),
    )
    op.create_index(
        "uq_platform_defaults_current_per_kind",
        "platform_defaults",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("is_current = true"),
    )

    insert = sa.text(
        "INSERT INTO platform_defaults (kind, version, is_current, payload) "
        "VALUES (:kind, 1, true, CAST(:payload AS jsonb))"
    )
    for kind, payload in _SEEDS:
        op.execute(insert.bindparams(kind=kind, payload=json.dumps(payload)))

    op.add_column("deals", sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("deals", sa.Column("template_source", sa.Text(), nullable=True))
    op.add_column("deals", sa.Column("template_snapshot", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(
        "ck_deals_template_source",
        "deals",
        "template_source IS NULL OR template_source IN "
        "('firm_type_match', 'firm_default', 'platform_standard', 'analyst_choice')",
    )

    op.execute(
        "INSERT INTO paper_types (id, label, description, is_active, sort_order) VALUES "
        "('residual_stock', 'Residual Stock', 'Secured against completed, unsold stock', TRUE, 30) "
        "ON CONFLICT (id) DO NOTHING"
    )
    op.execute(
        "UPDATE paper_types SET is_active = FALSE WHERE id IN ('term_loan', 'acquisition', 'refinance')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE paper_types SET is_active = TRUE WHERE id IN ('term_loan', 'acquisition', 'refinance')"
    )
    op.execute("DELETE FROM paper_types WHERE id = 'residual_stock'")

    op.drop_constraint("ck_deals_template_source", "deals", type_="check")
    op.drop_column("deals", "template_snapshot")
    op.drop_column("deals", "template_source")
    op.drop_column("deals", "template_id")

    op.drop_index("uq_platform_defaults_current_per_kind", table_name="platform_defaults")
    op.drop_table("platform_defaults")
