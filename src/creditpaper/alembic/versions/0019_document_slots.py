"""document slot board: slot types seeded from the taxonomy, per-deal slots and slot files.

Revision ID: 0019_document_slots
Revises: 0018_template_and_paper_theme

Contract: RFI list and fact sheet (2026-09-15) section A; docs/specs/slot-taxonomy.

ADDITIVE ONLY. Nothing here rewrites an existing record:
  - `document_slot_types` (platform data, not tenant data): one row per taxonomy slot,
    seeded from the copy of `docs/specs/slot-taxonomy/slot_taxonomy.json` embedded below.
    The copy is frozen with this revision; tests/deals/test_slot_board.py fails if it
    differs from the file. A later taxonomy version is a new migration, never an edit here.
  - `document_refused_classes` (platform data): the classes intake refuses (D7, D5 amended).
  - `document_slots` (tenant data): one row per deal per slot, created lazily on the first
    board read for that deal.
  - `document_slot_files` (tenant data): files proposed for, confirmed into, or considered
    for a slot (`role` assigned | candidate).
"""

# The embedded seed is the taxonomy file verbatim; its description lines are long by design.
# ruff: noqa: E501

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_document_slots"
down_revision: str | None = "0018_template_and_paper_theme"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Generated from docs/specs/slot-taxonomy/slot_taxonomy.json. Do not edit by hand.
TAXONOMY_JSON = """
{
  "version": "2026-09-15.2",
  "status": "seed — for the backend team to implement as document_slot_types; see README.md",
  "source": "The lender's standard RFI template (Borrower, Property, Builder and Legal DD items): the construction RFI for construction, the land RFI for bridge, the residual-stock RFI for residual stock; reconciled against BUILD_PLAN.md §4.2 and FIELD_DICTIONARY.md. Generic item names only; no deal, party or provider names.",
  "deal_types": [
    {
      "key": "construction",
      "label": "Construction"
    },
    {
      "key": "bridge",
      "label": "Bridge"
    },
    {
      "key": "residual_stock",
      "label": "Residual Stock"
    }
  ],
  "categories": [
    {
      "key": "borrower",
      "label": "Borrower"
    },
    {
      "key": "property",
      "label": "Property"
    },
    {
      "key": "builder",
      "label": "Builder"
    },
    {
      "key": "legal",
      "label": "Legal"
    }
  ],
  "slots": [
    {
      "key": "signed_its",
      "name": "Signed Term Sheet",
      "description": "The executed indicative term sheet or letter of offer. Normally from the lender.",
      "category": "legal",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": "its",
      "model_block": "economics, its, covenants",
      "checks": [
        "Signed",
        "Entity names present"
      ]
    },
    {
      "key": "kyc_screening",
      "name": "KYC and AML Screening Conclusions",
      "description": "Credit check and AML screening results for the borrower, guarantors and shareholders over 25%. Raw identity documents such as passports and driver licences are refused (D7).",
      "category": "borrower",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": "kyc",
      "model_block": "screening",
      "checks": [
        "Conclusion present for each party"
      ]
    },
    {
      "key": "corporate_structure",
      "name": "Corporate Structure Diagram",
      "description": "Entities, shareholders, UBOs and trust layers. Normally from the borrower.",
      "category": "borrower",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": "sponsor_structure",
      "model_block": "sponsor.structure",
      "checks": [
        "Cross-referencable to screening conclusions"
      ]
    },
    {
      "key": "company_trust_documents",
      "name": "Company and Trust Documents",
      "description": "Share certificates, trust deeds and constitutions for the borrowing entity and its shareholders. Feeds the corporate structure, including trust layers.",
      "category": "borrower",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": null,
      "model_block": "sponsor.structure",
      "checks": [
        "Entities and trustees match the corporate structure"
      ]
    },
    {
      "key": "borrower_profile",
      "name": "Borrower Corporate Profile",
      "description": "The borrower's profile or resume and track record. Normally from the borrower.",
      "category": "borrower",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": "sponsor_track_record",
      "model_block": "sponsor.claims",
      "checks": []
    },
    {
      "key": "funding_table",
      "name": "Indicative Funding Table",
      "description": "Sources and uses as agreed with the borrower.",
      "category": "borrower",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": null,
      "model_block": "funding_table",
      "checks": [
        "Reconciles to the signed term sheet"
      ]
    },
    {
      "key": "statement_of_position",
      "name": "Statements of Position",
      "description": "Signed and dated, one for each director guarantor, attested by an accountant.",
      "category": "borrower",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": null,
      "model_block": "sop",
      "checks": [
        "Accountant-prepared",
        "Arithmetic check"
      ]
    },
    {
      "key": "borrower_financials",
      "name": "Borrower Financial Accounts",
      "description": "The past two financial years, where the borrowing entity has them.",
      "category": "borrower",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": "sponsor_financials",
      "model_block": "sponsor.financials",
      "checks": [
        "Two years present",
        "Entity matches the borrower"
      ]
    },
    {
      "key": "ato_statements",
      "name": "ATO Portal Statements",
      "description": "Tax portal statements for the borrower and guarantors.",
      "category": "borrower",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": null,
      "model_block": "ato",
      "checks": [
        "Position clear or flagged"
      ]
    },
    {
      "key": "land_tax_clearance",
      "name": "Land Tax Clearance Certificate",
      "description": "Dated and property-specific, no more than three months old.",
      "category": "borrower",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": null,
      "model_block": "rates.land_tax",
      "checks": [
        "Under three months old",
        "Property and entity match"
      ]
    },
    {
      "key": "water_rates",
      "name": "Water Rates Clearance",
      "description": "Clearance certificate or account statement, no more than three months old.",
      "category": "borrower",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": null,
      "model_block": "rates.water",
      "checks": [
        "Under three months old",
        "Property and entity match"
      ]
    },
    {
      "key": "council_rates",
      "name": "Council Rates Receipts",
      "description": "Receipts showing no outstanding rates.",
      "category": "borrower",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": null,
      "model_block": "rates.council",
      "checks": [
        "No outstanding rates",
        "Property and entity match"
      ]
    },
    {
      "key": "equity_evidence",
      "name": "Equity Invoices",
      "description": "Invoices for all equity spent to date, where equity is being claimed. Normally from the borrower.",
      "category": "borrower",
      "applies_to": [
        "construction",
        "bridge"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": null,
      "model_block": "equity_evidence",
      "checks": [
        "Totals reconcile to claimed equity"
      ]
    },
    {
      "key": "purchase_contract",
      "name": "Purchase Contract",
      "description": "The purchaser-signed contract for the site being acquired.",
      "category": "borrower",
      "applies_to": [
        "bridge"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": null,
      "model_block": "purchase_contract",
      "checks": []
    },
    {
      "key": "leases",
      "name": "Lease Agreements",
      "description": "Agreements for any existing tenancies, where applicable.",
      "category": "borrower",
      "applies_to": [
        "bridge"
      ],
      "optional_for": [
        "all"
      ],
      "multi": true,
      "extractor": null,
      "model_block": "leases",
      "checks": []
    },
    {
      "key": "valuation",
      "name": "Valuation Report",
      "description": "Independent valuation no more than three months old, assigned to the lender.",
      "category": "property",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": "valuation",
      "model_block": "valuation, check_valuations",
      "checks": [
        "Under three months old at the expected funding date",
        "Assigned to the lender"
      ]
    },
    {
      "key": "public_liability_insurance",
      "name": "Public Liability Insurance",
      "description": "With the lending SPV noted as an interested party.",
      "category": "property",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": "insurance",
      "model_block": "insurance",
      "checks": [
        "Lending SPV noted",
        "Dates cover the loan term"
      ]
    },
    {
      "key": "qs_report",
      "name": "QS Initial Financier Report",
      "description": "Addressed to the lender. Normally from the quantity surveyor.",
      "category": "property",
      "applies_to": [
        "construction",
        "bridge"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": "qs",
      "model_block": "qs",
      "checks": [
        "Addressed to the lender"
      ]
    },
    {
      "key": "contract_works_insurance",
      "name": "Contract Works and Workers Compensation Insurance",
      "description": "With the lending SPV noted as an interested party.",
      "category": "property",
      "applies_to": [
        "construction",
        "bridge"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": "insurance",
      "model_block": "insurance",
      "checks": [
        "Lending SPV noted",
        "Sum sufficient",
        "Dates cover the build"
      ]
    },
    {
      "key": "development_approval",
      "name": "Development Approval and Stamped Plans",
      "description": "The development approval including stamped DA plans.",
      "category": "property",
      "applies_to": [
        "construction",
        "bridge"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": "da",
      "model_block": "da",
      "checks": [
        "Approval current",
        "Conditions extracted, blockers flagged"
      ]
    },
    {
      "key": "da_submission_pack",
      "name": "DA Submission Pack and Consultant Reports",
      "description": "Including geotechnical, environmental and other consultant reports.",
      "category": "property",
      "applies_to": [
        "construction",
        "bridge"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": null,
      "model_block": "consultants",
      "checks": []
    },
    {
      "key": "authority_contributions",
      "name": "Council Contributions and Authority Fees",
      "description": "Contributions plans, bonding or planning agreements and authority fee arrangements, where applicable.",
      "category": "property",
      "applies_to": [
        "construction",
        "residual_stock"
      ],
      "optional_for": [
        "all"
      ],
      "multi": true,
      "extractor": null,
      "model_block": null,
      "checks": []
    },
    {
      "key": "occupancy_certificate",
      "name": "Occupancy Certificate",
      "description": "The occupation certificate for the completed building.",
      "category": "property",
      "applies_to": [
        "residual_stock"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": null,
      "model_block": null,
      "checks": []
    },
    {
      "key": "building_insurance",
      "name": "Building Insurance",
      "description": "Landlord's insurance or insurance for full replacement value, with the lending SPV noted.",
      "category": "property",
      "applies_to": [
        "residual_stock"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": "insurance",
      "model_block": "insurance",
      "checks": [
        "Lending SPV noted",
        "Full replacement value"
      ]
    },
    {
      "key": "sales_contract_template",
      "name": "Pro-Forma Sales Contract",
      "description": "The standard contract used for sales.",
      "category": "property",
      "applies_to": [
        "construction",
        "residual_stock",
        "bridge"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": null,
      "model_block": null,
      "checks": []
    },
    {
      "key": "presales",
      "name": "Exchanged Presale Contracts",
      "description": "All exchanged presale contracts, with confirmation of deposits paid.",
      "category": "property",
      "applies_to": [
        "construction"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": null,
      "model_block": "presales",
      "checks": [
        "Deposits confirmed",
        "Arm's length",
        "Exchanged and unconditional"
      ]
    },
    {
      "key": "price_list",
      "name": "Approved Sales Price List",
      "description": "The price list approved for the remaining stock.",
      "category": "property",
      "applies_to": [
        "residual_stock"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": null,
      "model_block": "price_list",
      "checks": []
    },
    {
      "key": "contracts_eois",
      "name": "Exchanged Contracts and Expressions of Interest",
      "description": "Signed sale agreements and all other expressions of interest to date.",
      "category": "property",
      "applies_to": [
        "residual_stock"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": null,
      "model_block": "contracts_eois",
      "checks": []
    },
    {
      "key": "deposits_in_trust",
      "name": "Deposits Held in Trust",
      "description": "Confirmation that sale deposits are held in trust, with the undertaking.",
      "category": "property",
      "applies_to": [
        "residual_stock"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": null,
      "model_block": "deposits_trust",
      "checks": [
        "Undertaking present"
      ]
    },
    {
      "key": "marketing",
      "name": "Marketing and Sales Agent",
      "description": "Marketing materials, campaign details, agent appointment and commission arrangements.",
      "category": "property",
      "applies_to": [
        "construction",
        "residual_stock",
        "bridge"
      ],
      "optional_for": [
        "all"
      ],
      "multi": true,
      "extractor": null,
      "model_block": null,
      "checks": []
    },
    {
      "key": "consultant_profiles",
      "name": "Consultant Profiles",
      "description": "Resumes or corporate profiles of the project consultants.",
      "category": "property",
      "applies_to": [
        "construction",
        "bridge"
      ],
      "optional_for": [
        "all"
      ],
      "multi": true,
      "extractor": null,
      "model_block": null,
      "checks": []
    },
    {
      "key": "building_contract",
      "name": "Building Contract",
      "description": "The executed building contract, with the lawyers' review where available. Conditional on a bridge facility, where the build may not be contracted yet.",
      "category": "builder",
      "applies_to": [
        "construction",
        "bridge"
      ],
      "optional_for": [
        "bridge"
      ],
      "multi": true,
      "extractor": "building_contract",
      "model_block": "building_contract",
      "checks": [
        "Contract type and sum present"
      ]
    },
    {
      "key": "construction_certificate",
      "name": "Construction Certificate or Building Permit",
      "description": "Normally follows the development approval.",
      "category": "builder",
      "applies_to": [
        "construction",
        "bridge"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": null,
      "model_block": null,
      "checks": []
    },
    {
      "key": "builder_financials",
      "name": "Builder Financial Statements",
      "description": "The past two financial years.",
      "category": "builder",
      "applies_to": [
        "construction",
        "bridge"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": "builder",
      "model_block": "builder.financials",
      "checks": [
        "Two years present"
      ]
    },
    {
      "key": "builder_licence",
      "name": "Builder Licence",
      "description": "The builder's current licence.",
      "category": "builder",
      "applies_to": [
        "construction",
        "bridge"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": "builder",
      "model_block": "builder.licence",
      "checks": [
        "Licence current",
        "Category matches the project"
      ]
    },
    {
      "key": "builder_profile",
      "name": "Builder Profile and References",
      "description": "Corporate profile, prior building experience and references.",
      "category": "builder",
      "applies_to": [
        "construction"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": "builder",
      "model_block": "builder.references",
      "checks": []
    },
    {
      "key": "site_personnel",
      "name": "Key Site Personnel",
      "description": "Resumes of the site manager, foreman and other key onsite personnel.",
      "category": "builder",
      "applies_to": [
        "construction",
        "bridge"
      ],
      "optional_for": [
        "all"
      ],
      "multi": true,
      "extractor": null,
      "model_block": null,
      "checks": []
    },
    {
      "key": "builder_ato",
      "name": "Builder ATO Statements",
      "description": "Tax portal statements for the builder.",
      "category": "builder",
      "applies_to": [
        "construction"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": null,
      "model_block": null,
      "checks": []
    },
    {
      "key": "tripartite",
      "name": "Tripartite Deed",
      "description": "Agreed between builder, borrower and lender. Draft or executed.",
      "category": "builder",
      "applies_to": [
        "construction"
      ],
      "optional_for": [],
      "multi": false,
      "extractor": null,
      "model_block": "tripartite",
      "checks": [
        "Executed, or flagged as draft"
      ]
    },
    {
      "key": "schedule_of_finishes",
      "name": "Schedule of Finishes",
      "description": "The finishes schedule for the build.",
      "category": "builder",
      "applies_to": [
        "construction"
      ],
      "optional_for": [
        "all"
      ],
      "multi": false,
      "extractor": null,
      "model_block": null,
      "checks": []
    },
    {
      "key": "legal_review",
      "name": "Title Search and Survey Plans",
      "description": "The lawyers' legal review: land title search, survey plans and, where registered, title registrations.",
      "category": "legal",
      "applies_to": [
        "all"
      ],
      "optional_for": [],
      "multi": true,
      "extractor": null,
      "model_block": "security, oc_titles",
      "checks": [
        "First-mortgage position available"
      ]
    },
    {
      "key": "loan_documentation",
      "name": "Loan Documentation",
      "description": "Facility agreement, general security agreement and security documents.",
      "category": "legal",
      "applies_to": [
        "all"
      ],
      "optional_for": [
        "all"
      ],
      "multi": true,
      "extractor": null,
      "model_block": null,
      "checks": []
    },
    {
      "key": "payout_figure",
      "name": "Payout Figure",
      "description": "From the current mortgagee, where the facility refinances existing debt.",
      "category": "legal",
      "applies_to": [
        "all"
      ],
      "optional_for": [
        "all"
      ],
      "multi": false,
      "extractor": null,
      "model_block": "payout",
      "checks": [
        "Matches the funding table"
      ]
    },
    {
      "key": "deed_of_priority",
      "name": "Deed of Priority",
      "description": "With any subordinate lender.",
      "category": "legal",
      "applies_to": [
        "all"
      ],
      "optional_for": [
        "all"
      ],
      "multi": false,
      "extractor": null,
      "model_block": null,
      "checks": []
    },
    {
      "key": "independent_legal_advice",
      "name": "Independent Legal Advice",
      "description": "Certificates of independent legal advice for the guarantors.",
      "category": "legal",
      "applies_to": [
        "all"
      ],
      "optional_for": [
        "all"
      ],
      "multi": true,
      "extractor": null,
      "model_block": null,
      "checks": []
    },
    {
      "key": "rfi_sheet",
      "name": "RFI Sheet",
      "description": "The deal's RFI control sheet. Item statuses feed the disclosed outstanding-items list.",
      "category": "legal",
      "applies_to": [
        "all"
      ],
      "optional_for": [
        "all"
      ],
      "multi": false,
      "extractor": null,
      "model_block": "rfi",
      "checks": []
    }
  ],
  "refused_classes": [
    {
      "key": "raw_identity",
      "match": [
        "passport",
        "driver licence",
        "driver's licence",
        "medicare card",
        "birth certificate",
        "photo id"
      ],
      "reason": "Raw identity documents are not accepted. Upload the screening conclusion to KYC and AML Screening Conclusions instead.",
      "decision": "D7"
    },
    {
      "key": "pricing_model",
      "match": [
        "pricing model"
      ],
      "reason": "Pricing models are not read. Pricing comes from the signed term sheet.",
      "decision": "D5 (amended 2026-06-10)"
    }
  ]
}
"""

TAXONOMY: dict[str, Any] = json.loads(TAXONOMY_JSON)

SLOT_STATUSES = ("empty", "proposed", "confirmed", "processing", "failed", "withdrawn")


def refused_class_name(key: str) -> str:
    return key.replace("_", " ").capitalize()


def upgrade() -> None:
    op.create_table(
        "document_slot_types",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("applies_to", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("optional_for", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("multi", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("extractor", sa.Text(), nullable=True),
        sa.Column("model_block", sa.Text(), nullable=True),
        sa.Column("checks", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "category IN ('borrower', 'property', 'builder', 'legal')",
            name="ck_document_slot_types_category",
        ),
    )
    op.create_table(
        "document_refused_classes",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("match", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("taxonomy_version", sa.Text(), nullable=False),
    )

    version = TAXONOMY["version"]
    insert_slot = sa.text(
        "INSERT INTO document_slot_types (key, name, description, category, applies_to, optional_for, "
        "multi, extractor, model_block, checks, sort_order, taxonomy_version) VALUES "
        "(:key, :name, :description, :category, :applies_to, :optional_for, :multi, :extractor, "
        ":model_block, CAST(:checks AS jsonb), :sort_order, :version)"
    ).bindparams(
        sa.bindparam("applies_to", type_=postgresql.ARRAY(sa.Text())),
        sa.bindparam("optional_for", type_=postgresql.ARRAY(sa.Text())),
    )
    for order, slot in enumerate(TAXONOMY["slots"], start=1):
        op.execute(
            insert_slot.bindparams(
                key=slot["key"],
                name=slot["name"],
                description=slot["description"],
                category=slot["category"],
                applies_to=list(slot["applies_to"]),
                optional_for=list(slot["optional_for"]),
                multi=bool(slot["multi"]),
                extractor=slot["extractor"],
                model_block=slot["model_block"],
                checks=json.dumps(slot["checks"]),
                sort_order=order,
                version=version,
            )
        )
    insert_refused = sa.text(
        "INSERT INTO document_refused_classes (key, name, reason, match, decision, taxonomy_version) "
        "VALUES (:key, :name, :reason, :match, :decision, :version)"
    ).bindparams(sa.bindparam("match", type_=postgresql.ARRAY(sa.Text())))
    for refused in TAXONOMY["refused_classes"]:
        op.execute(
            insert_refused.bindparams(
                key=refused["key"],
                name=refused_class_name(refused["key"]),
                reason=refused["reason"],
                match=list(refused["match"]),
                decision=refused.get("decision"),
                version=version,
            )
        )

    op.create_table(
        "document_slots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", sa.Text(), nullable=False),
        sa.Column("slot_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="empty"),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("match_reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["slot_key"], ["document_slot_types.key"]),
        sa.UniqueConstraint("deal_id", "slot_key", name="uq_document_slots_deal_slot"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in SLOT_STATUSES) + ")",
            name="ck_document_slots_status",
        ),
    )
    op.create_index("ix_document_slots_tenant_deal", "document_slots", ["tenant_id", "deal_id"])

    op.create_table(
        "document_slot_files",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("match_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["slot_id"], ["document_slots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["deal_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["deal_document_versions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("slot_id", "document_id", "role", name="uq_document_slot_files_slot_doc_role"),
        sa.CheckConstraint("role IN ('assigned', 'candidate')", name="ck_document_slot_files_role"),
    )
    op.create_index(
        "ix_document_slot_files_tenant_document", "document_slot_files", ["tenant_id", "document_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_document_slot_files_tenant_document", table_name="document_slot_files")
    op.drop_table("document_slot_files")
    op.drop_index("ix_document_slots_tenant_deal", table_name="document_slots")
    op.drop_table("document_slots")
    op.drop_table("document_refused_classes")
    op.drop_table("document_slot_types")
