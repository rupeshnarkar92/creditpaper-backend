"""Generate creditpaper project plan Excel workbook."""

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

wb = openpyxl.Workbook()

# ── Styles ──
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill(start_color="1F3A5F", end_color="1F3A5F", fill_type="solid")
DONE_FILL = PatternFill(start_color="D4EDDA", end_color="D4EDDA", fill_type="solid")
PARTIAL_FILL = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid")
STUB_FILL = PatternFill(start_color="F8D7DA", end_color="F8D7DA", fill_type="solid")
TODO_FILL = PatternFill(start_color="E2E3E5", end_color="E2E3E5", fill_type="solid")
SECTION_FONT = Font(bold=True, size=11, color="1F3A5F")
THIN_BORDER = Border(
    left=Side(style="thin", color="D0D5DB"),
    right=Side(style="thin", color="D0D5DB"),
    top=Side(style="thin", color="D0D5DB"),
    bottom=Side(style="thin", color="D0D5DB"),
)


def style_header(ws, cols):
    for col in range(1, cols + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER
    ws.freeze_panes = "A2"


def style_row(ws, row, cols, fill=None):
    for col in range(1, cols + 1):
        cell = ws.cell(row=row, column=col)
        cell.border = THIN_BORDER
        cell.alignment = Alignment(vertical="top", wrap_text=True)
        if fill:
            cell.fill = fill


def auto_width(ws, cols, max_width=55):
    for col in range(1, cols + 1):
        letter = get_column_letter(col)
        lengths = []
        for row in ws.iter_rows(min_col=col, max_col=col, values_only=False):
            for cell in row:
                if cell.value:
                    lengths.append(len(str(cell.value)))
        best = min(max(lengths) + 2, max_width) if lengths else 15
        ws.column_dimensions[letter].width = best


# ═══════════════════════════════════════════════════════════
# SHEET 1: Overview
# ═══════════════════════════════════════════════════════════
ws = wb.active
ws.title = "Overview"
ws.append(["Module", "Backend", "Frontend", "Status", "Notes"])
style_header(ws, 5)

overview = [
    [
        "Authentication",
        "Complete",
        "Complete",
        "Done",
        "JWT, OAuth (Google/GitHub), email verify, password reset",
    ],
    ["User & Team Mgmt", "Complete", "Complete", "Done", "SuperAdmin CRUD, tenant admin, invitations"],
    [
        "Billing & Payments",
        "Complete",
        "Complete",
        "Done",
        "Stripe integration, 3 tiers, webhooks, usage tracking",
    ],
    ["Onboarding", "Complete", "Complete", "Done", "Brand, voice, papers, preview — 4-step wizard"],
    ["Deals CRUD", "Complete", "Complete", "Done", "Full CRUD + state machine (draft → review → approved)"],
    ["Workspace", "Complete", "Complete", "Done", "Documents, slots, transitions, extraction pipeline"],
    [
        "Document Processing",
        "Complete",
        "Complete",
        "Done",
        "PDF/DOCX/XLSX/image extractors, classify, chunk, OCR",
    ],
    ["Fact Extraction", "Complete", "Complete", "Done", "9 domain extractors, ledger writes, review queue"],
    [
        "Temporal Ledger",
        "Complete",
        "Complete",
        "Done",
        "Bitemporal fact tracking, derivation, approval freeze",
    ],
    ["Paper Generation", "Complete", "Complete", "Done", "Thesis + 7 LLM agents + 2 deterministic sections"],
    ["Templates", "Complete", "Complete", "Done", "CRUD, library, template switching"],
    ["Guardrails", "Complete", "Complete", "Done", "Rules CRUD, settings page"],
    ["Profiles / House Style", "Complete", "Complete", "Done", "House style, tenant profile, writing style"],
    ["Observability", "Complete", "—", "Done", "Cost ledger, timing decorator"],
    ["Harness", "Complete", "—", "Done", "Populate, reconcile, review utilities"],
    [
        "Checks Registry",
        "Partial",
        "—",
        "Partial",
        "17 checks registered, ALL logic=None — no check logic implemented",
    ],
    ["Insights", "—", "Stub", "Stub", "Route hidden, redirects to home — no analytics yet"],
    [
        "SuperAdmin Panel",
        "Complete",
        "Complete",
        "Done",
        "10+ admin pages: users, domains, subscribers, AI jobs, etc.",
    ],
]

for i, row in enumerate(overview, start=2):
    ws.append(row)
    status = row[3]
    fill = DONE_FILL if status == "Done" else PARTIAL_FILL if status == "Partial" else STUB_FILL
    style_row(ws, i, 5, fill)

auto_width(ws, 5)

# ═══════════════════════════════════════════════════════════
# SHEET 2: Backend Detail
# ═══════════════════════════════════════════════════════════
ws2 = wb.create_sheet("Backend Detail")
ws2.append(["Module", "Files / Components", "Lines (est.)", "Status", "Notes"])
style_header(ws2, 5)

backend = [
    [
        "Auth",
        "config.py, middleware.py, dependencies.py, database.py, routes/auth.py, routes/superadmin.py, routes/tenant.py, security/jwt.py, security/password.py, security/tokens.py, security/encryption.py",
        "~1,500",
        "Done",
        "Full JWT/OAuth/email flow",
    ],
    [
        "Auth Models",
        "models/user.py, domain.py, invitation.py, activity_log.py + schemas",
        "~800",
        "Done",
        "ORM + Pydantic schemas",
    ],
    [
        "Auth Store",
        "store/user_store.py, domain_store.py, invitation_store.py, activity_store.py",
        "~600",
        "Done",
        "Async SQLAlchemy queries",
    ],
    [
        "Billing",
        "plans.py, service.py, store.py, stripe_client.py, routes.py",
        "~500",
        "Done",
        "Stripe 3-tier subscriptions",
    ],
    ["Deals", "routes/deals.py, store/*, services/*", "~2,000", "Done", "Full CRUD + state machine"],
    [
        "Workspace",
        "routes/workspace.py, services/workspace.py",
        "~1,200",
        "Done",
        "Documents, slots, transitions",
    ],
    [
        "Document Processing",
        "pipeline.py, classify.py, chunker.py, ocr.py, page_selector.py, models.py, store.py, routes.py",
        "~800",
        "Done",
        "Upload → extract → classify → chunk → store",
    ],
    [
        "Extractors",
        "its.py, valuation.py, builder.py, building_contract.py, qs.py, da.py, insurance.py, sponsor.py, screening.py, base.py, _doc.py",
        "~2,500",
        "Done",
        "One per document type, two-pass extraction",
    ],
    [
        "Fact Extraction",
        "services/fact_extraction.py, fact_store.py",
        "~600",
        "Done",
        "Domain extractors → ledger writes",
    ],
    [
        "Temporal Ledger",
        "service.py (844 lines), routes.py, jobs.py, entities.py, types.py, retrieval.py, worker.py",
        "~1,500",
        "Done",
        "Bitemporal fact tracking",
    ],
    [
        "Paper Generation",
        "deals/services/generation.py, agents/section.py, agents/thesis.py, agents/review.py, agents/constitution.py, agents/proofread.py",
        "~1,200",
        "Done",
        "7 LLM agents + 2 deterministic",
    ],
    [
        "Paper Rendering",
        "paper/theme.py, defaults_store.py, fonts.py, colour.py, logo_fetch.py",
        "~500",
        "Done",
        "Theme, fonts, colors, logo",
    ],
    ["Templates", "routes/templates.py, store/template_store.py", "~400", "Done", "Template CRUD + library"],
    ["Guardrails", "routes/guardrails.py, store/guardrail_store.py", "~300", "Done", "Rules CRUD"],
    [
        "Contracts",
        "base.py, sections.py, citations.py, prose.py, signal.py, stores.py",
        "~500",
        "Done",
        "Type contracts, section taxonomy",
    ],
    [
        "Model",
        "deal.py, core.py, apply.py, citations.py, flatten.py, derive.py, materiality.py, signal.py",
        "~1,000",
        "Done",
        "DealModel, Fact, derivation engine",
    ],
    ["Checks", "registry.py", "~100", "Partial", "17 checks named, logic=None on all"],
    [
        "LLM Client",
        "client.py (760 lines), providers.py, ollama.py, pdf_render.py",
        "~900",
        "Done",
        "Single choke point, multi-provider",
    ],
    ["Observability", "ledger.py, timing.py", "~150", "Done", "Cost tracking, timing"],
    [
        "Harness",
        "populate.py, reconcile.py, review.py, route.py, learn_house_style.py, determinism.py",
        "~800",
        "Done",
        "Demo data, reconciliation, review",
    ],
    ["Profiles", "house_style.py, tenant_house_style.py", "~300", "Done", "House style + tenant profile"],
    [
        "Onboarding",
        "routes/brand.py, routes/papers.py, routes/voice.py, routes/preview.py, routes/state.py",
        "~600",
        "Done",
        "Brand, voice, papers, preview, state",
    ],
    ["Alembic Migrations", "0001–0023 + merge head", "23 migrations", "Done", "Schema mature, all applied"],
]

for i, row in enumerate(backend, start=2):
    ws2.append(row)
    status = row[3]
    fill = DONE_FILL if status == "Done" else PARTIAL_FILL if status == "Partial" else STUB_FILL
    style_row(ws2, i, 5, fill)

auto_width(ws2, 5)

# ═══════════════════════════════════════════════════════════
# SHEET 3: Frontend Detail
# ═══════════════════════════════════════════════════════════
ws3 = wb.create_sheet("Frontend Detail")
ws3.append(["Route / Page", "Component", "Files", "Status", "Notes"])
style_header(ws3, 5)

frontend = [
    [
        "/login, /signup, /check-email, /verify-email, /forgot-password, /reset-password, /set-password, /auth/callback",
        "Auth pages (8)",
        "auth/pages/*, auth/AuthLayout.tsx",
        "Done",
        "Full auth flow",
    ],
    ["/tenant-admin/home", "MyWorkPage", "home/MyWorkPage.tsx", "Done", "Dashboard with deals, activity"],
    ["/tenant-admin/activity", "ActivityPage", "home/ActivityPage.tsx", "Done", "Activity feed"],
    ["/tenant-admin/deals", "DealsListPage", "deals/DealsListPage.tsx", "Done", "Deal list with filters"],
    ["/tenant-admin/deals/new", "NewDealPage", "deals/NewDealPage.tsx", "Done", "Create deal form"],
    [
        "/tenant-admin/deals/:dealId",
        "WorkspacePage",
        "deals/pages/WorkspacePage.tsx",
        "Done",
        "Full workspace: docs, facts, paper, activity",
    ],
    ["/tenant-admin/library", "LibraryPage", "library/LibraryPage.tsx", "Done", "Template library"],
    [
        "/tenant-admin/library/templates/:id",
        "TemplatePage",
        "library/TemplatePage.tsx",
        "Done",
        "Template detail",
    ],
    [
        "/settings/profile",
        "HouseStyleTab",
        "pages/settings/HouseStyleTab.tsx",
        "Done",
        "Brand + writing style",
    ],
    ["/settings/guardrails", "Guardrails", "pages/settings/Guardrails.tsx", "Done", "Guardrail rules"],
    ["/settings/team", "TeamTab", "pages/settings/TeamTab.tsx", "Done", "Team management"],
    ["/settings/billing", "BillingTab", "pages/settings/BillingTab.tsx", "Done", "Billing + plan"],
    ["/settings/account", "AccountTab", "pages/settings/AccountTab.tsx", "Done", "Account settings"],
    ["/onboarding/brand", "BrandPage", "pages/onboarding/BrandPage.tsx", "Done", "Step 1: brand profile"],
    ["/onboarding/papers", "PapersPage", "pages/onboarding/PapersPage.tsx", "Done", "Step 2: papers config"],
    ["/onboarding/profile", "VoicePage", "pages/onboarding/VoicePage.tsx", "Done", "Step 3: voice profile"],
    [
        "/onboarding/preview",
        "SectionsAndSavePage",
        "pages/onboarding/SectionsAndSavePage.tsx",
        "Done",
        "Step 4: preview + save",
    ],
    ["/dashboard", "Dashboard", "admin/Dashboard.tsx", "Done", "Admin dashboard"],
    ["/superadmin/*", "SuperAdmin (10+ pages)", "admin/SuperAdmin*.tsx", "Done", "Full superadmin panel"],
    ["/tenant-admin/insights", "—", "—", "Stub", "Hidden, redirects to home — needs Reports + Audit Log"],
]

for i, row in enumerate(frontend, start=2):
    ws3.append(row)
    status = row[3]
    fill = DONE_FILL if status == "Done" else STUB_FILL
    style_row(ws3, i, 5, fill)

auto_width(ws3, 5)

# ═══════════════════════════════════════════════════════════
# SHEET 4: Remaining Work
# ═══════════════════════════════════════════════════════════
ws4 = wb.create_sheet("Remaining Work")
ws4.append(["#", "Task", "Module", "Priority", "Effort", "Status", "Notes"])
style_header(ws4, 7)

remaining = [
    [
        1,
        "Implement 17 cross-document checks (logic=None)",
        "Checks Registry",
        "High",
        "~3 days",
        "Not Started",
        "D6 in checks/registry.py — all 17 registered but no logic",
    ],
    [
        2,
        "Wire checks into generation pipeline",
        "Paper Generation",
        "High",
        "~1 day",
        "Not Started",
        "generation.py line 284: findings list is empty TODO",
    ],
    [
        3,
        "Create Reports page (Insights)",
        "Insights",
        "Medium",
        "~2 days",
        "Not Started",
        "Deals by stage chart, median turnaround, portfolio exposure",
    ],
    [
        4,
        "Create Audit Log page (Insights)",
        "Insights",
        "Medium",
        "~1 day",
        "Not Started",
        "Timestamp/actor/event/deal/detail table with pagination",
    ],
    [
        5,
        "Add Insights routes + navigation",
        "Insights",
        "Medium",
        "~0.5 day",
        "Not Started",
        "Routes in App.tsx, SubPanel nav links",
    ],
    [
        6,
        "Light/dark mode for Insights pages",
        "Insights",
        "Medium",
        "~0.5 day",
        "Not Started",
        "CSS vars per theme",
    ],
    [
        7,
        "Re-upload deal files to production DB",
        "Data",
        "High",
        "~0.5 day",
        "Not Started",
        "6 of 7 docs archived during merge",
    ],
    [
        8,
        "End-to-end credit paper generation test",
        "Testing",
        "High",
        "~1 day",
        "Not Started",
        "Full flow with NVIDIA provider",
    ],
    [
        9,
        "Scaffold/analyst-action audit in review.py",
        "Agents",
        "Low",
        "~0.5 day",
        "Not Started",
        "review.py line 106: scaffold lines marked TODO",
    ],
    [
        10,
        "Empty agent sub-packages (models/, schemas/, services/, store/, routes/)",
        "Agents",
        "Low",
        "~0 days",
        "Structural",
        "Empty __init__.py placeholders — no work needed yet",
    ],
]

for i, row in enumerate(remaining, start=2):
    ws4.append(row)
    priority = row[3]
    fill = STUB_FILL if priority == "High" else PARTIAL_FILL if priority == "Medium" else TODO_FILL
    style_row(ws4, i, 7, fill)

auto_width(ws4, 7)

# ═══════════════════════════════════════════════════════════
# SHEET 5: Database
# ═══════════════════════════════════════════════════════════
ws5 = wb.create_sheet("Database")
ws5.append(["Migration", "Topic", "Tables Added", "Status"])
style_header(ws5, 4)

migrations = [
    [
        "0001",
        "Core tables",
        "deals, deal_documents, deal_sections, paper_prose, deal_model_snapshots, signals, graph_entities, graph_edges",
        "Applied",
    ],
    ["0002", "Auth tables", "users, domains, invitations, activity_log, verification_tokens", "Applied"],
    ["0003", "Verification tokens", "verification_tokens (enhanced)", "Applied"],
    ["0004a", "Templates & paper types", "templates, template_papers, paper_types", "Applied"],
    ["0004b", "Deal enhancements", "deal_audit_events, deal_events, deal_state_transitions", "Applied"],
    ["0005a", "Guardrail rules", "guardrail_rules", "Applied"],
    ["0005b", "Deal workspace", "deal_workspaces", "Applied"],
    ["0006", "Onboarding doc sections", "onboarding_documents, onboarding_state", "Applied"],
    ["0007", "Document extraction", "deal_document_extractions, deal_document_versions", "Applied"],
    ["0008a", "Extraction status", "extraction_status columns", "Applied"],
    ["0008b", "Template paper extraction", "template extraction columns", "Applied"],
    ["0009", "Training data", "extraction_prompt_versions, extraction_training_examples", "Applied"],
    ["0010", "Fact review queue", "fact_review_queue", "Applied"],
    ["0011", "Paper sections", "deal_paper_sections", "Applied"],
    ["0012", "Temporal ledger", "fact_ledger, ledger_assertions, ledger_approvals", "Applied"],
    [
        "0013",
        "Ledger operations",
        "ledger_chunks, ledger_edges, ledger_entities, ledger_embeddings, ledger_identifiers, ledger_outbox, ledger_supersessions, ledger_entity_links, ledger_entity_link_retractions, ledger_edge_retractions, ledger_match_proposals, ledger_resolutions, ledger_reviews",
        "Applied",
    ],
    ["0014", "Ledger integrity", "Integrity constraints on ledger tables", "Applied"],
    ["0015", "Paper evidence revision", "document_slot_types, document_refused_classes", "Applied"],
    ["0016", "User profile fields", "User profile columns", "Applied"],
    ["0017", "Billing", "billing_accounts, billing_invoices, billable_events", "Applied"],
    ["0018", "Template & paper theme", "platform_defaults", "Applied"],
    ["0019", "Document slots", "document_slots, document_slot_files", "Applied"],
    ["0020", "Deal activity index", "Index on deal_activity_log", "Applied"],
    ["0021", "Function search path", "PostgreSQL function search path pinning", "Applied"],
    ["0022", "Job tenant defaults", "Job tenant role defaults", "Applied"],
    ["0023", "Deal recommendations", "deal_recommendations", "Applied"],
    ["HEAD", "Merge heads", "Single head consolidated", "Applied"],
]

for i, row in enumerate(migrations, start=2):
    ws5.append(row)
    style_row(ws5, i, 4, DONE_FILL)

auto_width(ws5, 4)

# ═══════════════════════════════════════════════════════════
# Save
# ═══════════════════════════════════════════════════════════
out = r"D:\OSProjects\creditpaper\PROJECT_PLAN.xlsx"
wb.save(out)
print(f"Saved: {out}")
