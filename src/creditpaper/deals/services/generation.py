"""Credit paper generation service.

Orchestrates thesis + section agents, stores prose in deal_paper_sections.
Postgres leases serialize generation and survive process restarts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import Future
from datetime import UTC, datetime
from typing import Any

from creditpaper.agents.constitution import constitution_text
from creditpaper.agents.section import PROSE_AGENTS
from creditpaper.agents.thesis import ThesisAgent
from creditpaper.concurrency import pool
from creditpaper.contracts.sections import SECTION_NUMBERS
from creditpaper.db import cursor
from creditpaper.guardrails.services.guardrail_service import evaluate_rule
from creditpaper.ledger import jobs
from creditpaper.model.deal import deal_type_str
from creditpaper.model.flatten import citable_facts

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GenerationJobNotFound(Exception):
    """Raised when the job_id does not exist or does not belong to the deal."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_generation(
    *, deal_id: str, tenant_id: str, sections: list[int] | None = None, actor_id: str | None = None
) -> str:
    """Start paper generation for a deal. Returns the job_id (uuid).

    Launches a background thread that runs ThesisAgent (if needed) then each
    section agent in parallel. Sections default to all nine if not specified.
    """
    _require_deal(tenant_id, deal_id, writable=True)
    if sections is None:
        sections = list(SECTION_NUMBERS)
    if (
        not sections
        or len(sections) > len(SECTION_NUMBERS)
        or any(s not in SECTION_NUMBERS for s in sections)
    ):
        raise ValueError("Invalid section selection")
    sections = list(dict.fromkeys(sections))

    payload = {"sections": sections}
    if actor_id is not None:
        payload["actor_id"] = actor_id
    job_id = jobs.enqueue(tenant_id, deal_id, "generation", "paper", payload)
    jobs.dispatch(tenant_id, job_id)
    return job_id


def get_generation_status(*, deal_id: str, job_id: str, tenant_id: str) -> dict[str, Any]:
    """Return the status dict for a generation job.

    Raises GenerationJobNotFound if the job_id is unknown or does not belong
    to the given deal.
    """
    from creditpaper.ledger.types import NotFound

    try:
        return jobs.status(tenant_id, deal_id, job_id)
    except NotFound:
        raise GenerationJobNotFound("Generation job not found") from None


def get_all_sections(*, deal_id: str, tenant_id: str) -> list[dict[str, Any]]:
    """Return all generated sections for a deal, ordered by section number."""
    _require_deal(tenant_id, deal_id)
    with cursor() as cur:
        cur.execute(
            "SELECT section, status, prose, citations, error "
            "FROM deal_paper_sections WHERE deal_id = %s ORDER BY section",
            (deal_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "section": r[0],
            "status": r[1],
            "prose": r[2],
            "citations": r[3] or [],
            "error": r[4],
        }
        for r in rows
    ]


def regenerate_section(
    *, deal_id: str, tenant_id: str, section_num: int, actor_id: str | None = None
) -> dict[str, Any]:
    """Regenerate a single section. Raises ValueError for out-of-range section."""
    if section_num not in SECTION_NUMBERS:
        raise ValueError(f"Invalid section number: {section_num}")
    _require_deal(tenant_id, deal_id, writable=True)

    job_id = start_generation(deal_id=deal_id, tenant_id=tenant_id, sections=[section_num], actor_id=actor_id)
    return {"status": "processing", "section": section_num, "job_id": job_id}


# ---------------------------------------------------------------------------
# Background generation
# ---------------------------------------------------------------------------


def _run_generation(
    job_id: str,
    deal_id: str,
    tenant_id: str,
    sections: list[int],
    lock: threading.Lock | None = None,
    actor_id: str | None = None,
) -> dict:
    """Background generation thread."""
    from creditpaper.llm.tenant_config import provider_session

    with provider_session(tenant_id):
        return _run_generation_scoped(job_id, deal_id, tenant_id, sections, lock, actor_id)


def _run_generation_scoped(
    job_id: str,
    deal_id: str,
    tenant_id: str,
    sections: list[int],
    lock: threading.Lock | None = None,
    actor_id: str | None = None,
) -> dict:
    from creditpaper.obs.ledger import cost_session

    with cost_session(deal_id=deal_id):
        return _run_generation_with_ledger(job_id, deal_id, tenant_id, sections, lock, actor_id)


def _run_generation_with_ledger(job_id, deal_id, tenant_id, sections, lock, actor_id=None):
    gen_start = time.monotonic()
    job = {"status": "running", "sections": {str(s): "pending" for s in sections}}
    try:
        # Load model
        model = _load_model(deal_id, tenant_id)
        if model is None:
            _record_generation_event(
                tenant_id,
                deal_id,
                "paper.generation_failed",
                {"error": "model_not_found", "job_id": job_id},
            )
            return {"status": "failed", "error": "Deal model not found"}

        # Snapshot active guardrails before drafting: later rule changes cannot rewrite this paper's basis.
        guardrail_rules, constitution_version = _generation_guardrail_snapshot(tenant_id, deal_id)

        # Load house style
        house_style = _load_house_style(tenant_id)

        # Phase A: Thesis pre-pass (if any prose sections that need it are queued)
        prose_sections_needed = {s for s in sections if s in PROSE_AGENTS}
        if prose_sections_needed & {2, 5, 7, 8}:
            job["thesis"] = "processing"
            jobs.progress(job)
            thesis_start = time.monotonic()
            try:
                agent = ThesisAgent()
                spine = agent.build(model)
                model.thesis = spine
                job["thesis"] = "ready"
                logger.info("Thesis completed in %.1fs", time.monotonic() - thesis_start)
            except Exception as exc:
                logger.error("Thesis failed: %s", type(exc).__name__)
                _record_generation_event(
                    tenant_id,
                    deal_id,
                    "paper.generation_failed",
                    {"error": "thesis_failed", "job_id": job_id},
                )
                return {"status": "failed", "error": "Thesis generation failed"}

        # Phase B: Generate each section in parallel
        section_times: dict[int, float] = {}

        def gen_section(section_num: int) -> tuple[int, str, str, list[dict[str, Any]]]:
            sec_start = time.monotonic()
            try:
                prose, evaluations = _generate_section(
                    model, tenant_id, section_num, house_style, guardrail_rules
                )
                elapsed = time.monotonic() - sec_start
                section_times[section_num] = elapsed
                logger.info("Section %d completed in %.1fs", section_num, elapsed)
                return (section_num, "ready", prose, evaluations)
            except Exception as exc:
                elapsed = time.monotonic() - sec_start
                section_times[section_num] = elapsed
                logger.error("Section %d failed in %.1fs: %s", section_num, elapsed, type(exc).__name__)
                return (section_num, "failed", "Section generation failed", [])

        futures: dict[Future[tuple[int, str, str, list[dict[str, Any]]]], int] = {}
        with pool(max_workers=len(sections) or 1, thread_name_prefix="gen-section") as p:
            for s in sections:
                f = p.submit(gen_section, s)
                futures[f] = s

            for future in futures:
                section_num, status, result, evaluations = future.result()
                _upsert_section(
                    deal_id,
                    section_num,
                    status,
                    prose=result if status == "ready" else None,
                    error=result if status == "failed" else None,
                    tenant_id=tenant_id,
                    expected_revision=model.model_version,
                    generation_job_id=job_id,
                    actor_id=actor_id,
                    guardrail_snapshot={
                        "rules": guardrail_rules,
                        "evaluations": evaluations,
                        "evaluated_at": datetime.now(UTC).isoformat(),
                    },
                    constitution_version=constitution_version,
                )
                job["sections"][str(section_num)] = status
                jobs.progress(job)

        total_elapsed = time.monotonic() - gen_start
        job["status"] = "failed" if "failed" in job["sections"].values() else "complete"
        if job["status"] == "complete":
            _advance_generation_state(tenant_id, deal_id, actor_id, sections)
        if len(sections) == 1:
            ready = job["sections"][str(sections[0])] == "ready"
            error = None if ready else "generation_error"
            _record_section_event(tenant_id, deal_id, sections[0], error=error)
        else:
            statuses = list(job["sections"].values())
            _record_generation_event(
                tenant_id,
                deal_id,
                "paper.generated",
                {
                    "sections_count": sum(1 for v in statuses if v == "ready"),
                    "failed_count": sum(1 for v in statuses if v == "failed"),
                    "job_id": job_id,
                },
            )
        job["timing"] = {
            "total_s": round(total_elapsed, 1),
            "sections": {str(k): round(v, 1) for k, v in section_times.items()},
        }
        logger.info(
            "Generation complete for deal %s in %.1fs (sections: %s)",
            deal_id,
            total_elapsed,
            ", ".join(f"s{k}={v:.1f}s" for k, v in sorted(section_times.items())),
        )
        return job
    except Exception as exc:
        logger.error("Generation failed: %s", type(exc).__name__)
        from creditpaper.ledger.types import BadInput, Hold, NotFound

        _record_generation_event(
            tenant_id, deal_id, "paper.generation_failed", {"error": "generation_error", "job_id": job_id}
        )
        return {
            "status": "failed",
            "error": "Generation failed; retry after resolving the deal status",
            "error_type": type(exc).__name__,
            "retryable": not isinstance(exc, (BadInput, Hold, NotFound)),
        }
    finally:
        if lock:
            lock.release()


# ---------------------------------------------------------------------------
# Activity (best-effort: a recording failure never reaches the generation job)
# ---------------------------------------------------------------------------


def _record_generation_event(tenant_id: str, deal_id: str, action: str, metadata: dict[str, Any]) -> None:
    from creditpaper.deals.services.activity import record_deal_event_best_effort

    record_deal_event_best_effort(
        tenant_id=tenant_id,
        deal_id=deal_id,
        actor_id=None,
        action=action,
        entity_type="deal",
        entity_id=deal_id,
        metadata=metadata,
    )


def _record_section_event(
    tenant_id: str, deal_id: str, section_num: int, *, error: str | None = None
) -> None:
    """`paper.section_generated` / `paper.section_failed` with the template section's key and
    title. `error` is a short code, never the exception text."""
    try:
        from creditpaper.deals.services.paper_sections import section_ref

        ref = section_ref(tenant_id, deal_id, section_num)
    except Exception:  # noqa: BLE001 — the label falls back to the engine number
        ref = {"key": None, "title": f"Section {section_num}"}
    metadata: dict[str, Any] = {**ref, "section": section_num}
    if error is not None:
        metadata["error"] = error
    action = "paper.section_failed" if error is not None else "paper.section_generated"
    _record_generation_event(tenant_id, deal_id, action, metadata)


# ---------------------------------------------------------------------------
# Section generation
# ---------------------------------------------------------------------------


def _generate_section(
    model: Any,
    tenant_id: str,
    section_num: int,
    house_style: dict | None,
    guardrail_rules: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Generate prose against the generation-time rule snapshot and return its evaluation record."""
    if section_num in PROSE_AGENTS:
        agent_cls = PROSE_AGENTS[section_num]
        agent = agent_cls(tenant_id=tenant_id)
        agent._generation_guardrail_rules = guardrail_rules
        findings: list = []  # TODO: load from checks registry when wired
        prose = agent.draft(model, findings, house_style=house_style).text
    else:
        prose = _deterministic_section(model, section_num)
    evaluations = _evaluate_guardrails(
        tenant_id=tenant_id, section_num=section_num, text=prose, model=model, rules=guardrail_rules
    )
    return prose, evaluations


def _evaluate_guardrails(
    *,
    tenant_id: str,
    section_num: int,
    text: str,
    model: Any,
    rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run active firm guardrails against drafted prose and record results.

    Returns the list of flagged evaluations (may be empty). Evaluation
    failures are logged but never block generation.
    """
    if not rules:
        return []

    facts = citable_facts(model)
    property_type = deal_type_str(model)
    section_titles = [PROSE_AGENTS[n].title for n in sorted(PROSE_AGENTS)]
    flagged: list[dict] = []

    for rule in rules:
        try:
            ev = evaluate_rule(
                rule,
                text=text,
                order=section_titles,
                facts=facts,
                property_type=property_type,
            )
        except Exception:
            logger.warning(
                "guardrail evaluation: rule %s raised for section %d",
                rule.get("id", "?"),
                section_num,
            )
            flagged.append(
                {
                    "rule_id": str(rule["id"]),
                    "kind": rule["kind"],
                    "severity": rule.get("severity", "info"),
                    "result": "error",
                    "measured": {"reason": "evaluation failed"},
                }
            )
            try:
                from creditpaper.db import tenant_cursor, tenant_uuid

                with tenant_cursor(tenant_id) as cur:
                    cur.execute(
                        "SELECT public.record_paper_guardrail_evaluation(%s, %s, %s, %s, %s::jsonb)",
                        (
                            tenant_uuid(tenant_id),
                            rule["id"],
                            section_num,
                            "error",
                            json.dumps({"reason": "evaluation failed"}),
                        ),
                    )
            except Exception:
                logger.warning(
                    "guardrail evaluation: failed to record error for rule %s",
                    rule.get("id", "?"),
                )
            continue

        try:
            from creditpaper.db import tenant_cursor, tenant_uuid

            with tenant_cursor(tenant_id) as cur:
                cur.execute(
                    "SELECT public.record_paper_guardrail_evaluation(%s, %s, %s, %s, %s::jsonb)",
                    (
                        tenant_uuid(tenant_id),
                        rule["id"],
                        section_num,
                        ev["result"],
                        json.dumps(ev.get("measured") or {}),
                    ),
                )
        except Exception:
            logger.warning(
                "guardrail evaluation: failed to record result for rule %s",
                rule.get("id", "?"),
            )
            ev = {"result": "error", "measured": {"reason": "evaluation could not be persisted"}}

        evaluation = {
            "rule_id": str(rule["id"]),
            "kind": rule["kind"],
            "severity": rule.get("severity", "info"),
            **ev,
        }
        # Approval relies on the complete generation-time evaluation snapshot.
        flagged.append(evaluation)

    if flagged:
        logger.info(
            "Section %d: %d guardrail evaluation(s) captured",
            section_num,
            len(flagged),
        )

    return flagged


def _generation_guardrail_snapshot(tenant_id: str, deal_id: str) -> tuple[list[dict[str, Any]], str]:
    from creditpaper.db import tenant_cursor, tenant_uuid

    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.get_paper_generation_context(%s, %s)", (tid, deal_id))
        row = cur.fetchone()
    if not row or not row[0]:
        raise RuntimeError("Generation rules are unavailable")
    payload = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    rules = payload.get("guardrail_snapshot") or []
    return rules, hashlib.sha256(constitution_text().encode("utf-8")).hexdigest()


def _advance_generation_state(
    tenant_id: str, deal_id: str, actor_id: str | None, generated_sections: list[int]
) -> None:
    if actor_id is None:
        return
    from creditpaper.db import tenant_cursor, tenant_uuid
    from creditpaper.deals.services.deal_templates import effective_template
    from creditpaper.deals.services.section_resolver import map_sections
    from creditpaper.deals.store import deal_template_store

    tid = tenant_uuid(tenant_id)
    deal = deal_template_store.get_deal(str(tid), deal_id)
    if not deal:
        return
    _, _, snapshot = effective_template(str(tid), deal)
    required = sorted(
        {section for section in map_sections(snapshot.get("sections") or []).values() if section is not None}
    )
    if not required or not set(required).issubset(generated_sections):
        return
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT public.complete_paper_generation_stage(%s, %s, %s, %s)",
            (tid, deal_id, tenant_uuid(actor_id), required),
        )


def _deterministic_section(model: Any, section_num: int) -> str:
    """Generate deterministic sections (1, 3) from model facts."""
    if section_num == 1:
        return _render_snapshot(model)
    elif section_num == 3:
        return _render_funding(model)
    return ""


def _val(fact: Any) -> Any:
    """Extract the raw value from a Fact, or return None."""
    if fact is None:
        return None
    return getattr(fact, "value", fact)


def _render_snapshot(model: Any) -> str:
    """Section 1: Deal Snapshot — deterministic from model.deal + model.economics."""
    lines = ["## 1. Deal Snapshot", ""]
    d = model.deal
    e = model.economics
    if d:
        lines.append(f"**Deal Name:** {_val(d.name) or 'N/A'}")
        deal_type = _val(d.type)
        dt_label = deal_type.value if hasattr(deal_type, "value") else deal_type or "N/A"
        lines.append(f"**Deal Type:** {dt_label}")
    if e:
        fa = _val(e.facility_amount)
        lines.append(f"**Facility Amount:** ${fa:,.0f}" if fa else "**Facility Amount:** N/A")
        ir = _val(e.interest_rate)
        lines.append(f"**Interest Rate:** {ir}" if ir else "**Interest Rate:** N/A")
        tm = _val(e.term_months)
        lines.append(f"**Term:** {tm} months" if tm else "**Term:** N/A")
    return "\n".join(lines)


def _render_funding(model: Any) -> str:
    """Section 3: Funding Snapshot — deterministic from model.economics.funding_table."""
    lines = ["## 3. Funding Snapshot", ""]
    e = model.economics
    if e and e.funding_table:
        lines.append("| Source | Total | Senior | Equity |")
        lines.append("|--------|-------|--------|--------|")
        for key, f in e.funding_table.items():
            label = f.label if hasattr(f, "label") else str(key)
            total = _val(f.total)
            senior = _val(f.senior)
            equity = _val(f.equity)
            t = f"${total:,.0f}" if total else "N/A"
            s = f"${senior:,.0f}" if senior else "N/A"
            eq = f"${equity:,.0f}" if equity else "N/A"
            lines.append(f"| {label} | {t} | {s} | {eq} |")
    else:
        lines.append("No funding data available.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _load_model(deal_id: str, tenant_id: str) -> Any:
    """Use the current ledger projection; legacy deals are readable until imported."""
    from creditpaper.ledger.service import load_model

    return load_model(tenant_id, deal_id)


def _require_deal(tenant_id: str, deal_id: str, *, writable: bool = False) -> None:
    from creditpaper.db import tenant_cursor
    from creditpaper.ledger.types import Conflict, NotFound

    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT current_state FROM deals WHERE tenant_id=%s AND id=%s", (tenant_id, deal_id))
        row = cur.fetchone()
        if not row:
            raise NotFound("Deal not found")
        if writable and row[0] in {"APPROVED", "ISSUED", "ARCHIVED"}:
            raise Conflict("Approved or archived papers cannot be regenerated; reopen review first")


def _load_house_style(tenant_id: str) -> dict | None:
    """The tenant's house style (D10): its learned voice profile, else the platform default
    voice. Firm name from the brand profile, else the firm's company name, else "the lender".

    A failed read of one input is treated as that input being absent, so generation still
    runs on the platform voice. Never the 3Capital seed.
    """
    from creditpaper.profiles.tenant_house_style import build_house_style

    return build_house_style(
        voice_profile=_read_or_none("voice profile", _tenant_voice_profile, tenant_id),
        platform_voice=_read_or_none("platform voice", _platform_voice, tenant_id),
        brand_profile=_read_or_none("brand profile", _tenant_brand_profile, tenant_id),
        company=_read_or_none("company name", _tenant_company, tenant_id),
    )


def _read_or_none(label: str, read: Any, tenant_id: str) -> Any:
    try:
        return read(tenant_id)
    except Exception as exc:  # noqa: BLE001 — a missing style input never stops generation
        logger.warning("house style: %s unavailable (%s)", label, type(exc).__name__)
        return None


def _tenant_voice_profile(tenant_id: str) -> dict | None:
    from creditpaper.onboarding.utils.store import load_voice_profile

    return load_voice_profile(tenant_id)


def _tenant_brand_profile(tenant_id: str) -> dict | None:
    from creditpaper.onboarding.utils.store import load_brand_profile

    return load_brand_profile(tenant_id)


def _platform_voice(_tenant_id: str) -> dict | None:
    from creditpaper.paper.defaults_store import load_current

    return load_current("voice").get("payload")


def _tenant_company(tenant_id: str) -> str | None:
    with cursor() as cur:
        cur.execute("SELECT company FROM domains WHERE id = %s", (str(tenant_id),))
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def _upsert_section(
    deal_id: str,
    section_num: int,
    status: str,
    *,
    prose: str | None = None,
    error: str | None = None,
    tenant_id: str,
    expected_revision: int,
    generation_job_id: str | None = None,
    actor_id: str | None = None,
    guardrail_snapshot: dict[str, Any] | None = None,
    constitution_version: str = "unknown",
) -> None:
    """Save one section atomically, archiving an existing edit before replacement."""
    from creditpaper.db import tenant_cursor, tenant_uuid
    from creditpaper.ledger.service import lock_deal
    from creditpaper.ledger.types import Conflict

    with tenant_cursor(tenant_id) as cur:
        revision = lock_deal(cur, tenant_id, deal_id)
        jobs.fence(cur)
        cur.execute("SELECT current_state FROM deals WHERE tenant_id=%s AND id=%s", (tenant_id, deal_id))
        state_row = cur.fetchone()
        if (
            not state_row
            or state_row[0] in {"APPROVED", "ISSUED", "ARCHIVED"}
            or (revision and revision != expected_revision)
        ):
            raise Conflict("Deal changed while generation was running; regenerate from current evidence")
        cur.execute(
            "SELECT public.save_generated_paper_section(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                tenant_uuid(tenant_id),
                deal_id,
                section_num,
                status,
                prose,
                error,
                revision,
                generation_job_id,
                actor_id,
                constitution_version,
                json.dumps(guardrail_snapshot or {}),
            ),
        )
