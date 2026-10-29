"""Thesis pre-pass: synthesise the deal's narrative spine ONCE, before the body
sections (Phase A). Every section then inherits the same thesis, so parallel
generation tells one coherent story instead of each section forming (and drifting
on) its own read. The spine is the qualitative analogue of `model.metrics`.

ROBUSTNESS — the spine is the highest-blast-radius artifact: an error here
propagates to every section (the "coherence amplifier", review round). So it is
gated HARDER than any section:
  - it obeys the firm's fidelity rules itself (it is copied downstream verbatim);
  - external/sponsor claims are ATTRIBUTED in the framing (an unattributed claim
    here repeats in every section — the round-1 7-for-1 finding);
  - it carries NO independent figures (sections cite the exact metrics; the spine
    is qualitative — this also stops rounded numbers leaking, e.g. "$13k");
  - a deterministic completeness guard checks it does not MISS a material signal
    the model already knows (covenant near ceiling, day-one deficit, uncontracted
    exit, missing builder financials);
  - a fidelity gate + one corrective redraft fixes issues at the source, before
    propagation (safe here — the fixes are attribution/grounding, not blunting).

It also doubles as the HITL sign-off checkpoint. LLM boundary isolated for CI.
"""

from __future__ import annotations

from creditpaper.agents.constitution import ANALYSIS_MANDATE, PROHIBITIONS, VOICE
from creditpaper.config import prose_model
from creditpaper.model.core import ProseBlock
from creditpaper.model.deal import DealModel, ThesisSpine
from creditpaper.model.flatten import citable_facts
from creditpaper.model.materiality import HIGH, MaterialItem, assess_materiality


def _spine_text(spine: ThesisSpine) -> str:
    parts = [
        spine.overall_read,
        *spine.dominant_risks,
        *spine.key_tensions,
        *spine.canonical_framings.values(),
    ]
    return "\n".join(p for p in parts if p)


def missing_signals(model: DealModel, spine: ThesisSpine) -> list[MaterialItem]:
    """HIGH-tier crux items the spine fails to mention at all. A spine must cover
    the CRUX (computed by the materiality engine), not every medium gap — selective
    depth. Grounds 'doesn't miss the point' in computed materiality, not LLM luck."""
    text = _spine_text(spine).lower()
    return [
        it
        for it in assess_materiality(model)
        if it.score >= HIGH and it.keywords and not any(k in text for k in it.keywords)
    ]


class ThesisAgent:
    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    def _complete(self, system: str, user: str) -> str:
        from creditpaper.llm.client import messages_create

        model = prose_model(self._model_name)
        # Cap is generous on purpose: the thesis is the spine every section
        # inherits, so a truncated spine corrupts the whole paper, and on Opus 5
        # thinking shares this budget with the response. Truncation raises
        # (llm/client.py) rather than silently shortening.
        resp = messages_create(
            tool="agent:thesis",
            stage="thesis",
            model=model,
            max_tokens=16000,
            system=system,
            cache_system=True,
            messages=[{"role": "user", "content": user}],
        )
        return resp.text

    def _system(self) -> str:
        rules = "\n".join(f"  - [{cat}] {rule}" for cat, rule in PROHIBITIONS)
        return (
            "You are the senior credit analyst forming the THESIS for a deal before "
            f"the paper is written.\n\n{VOICE}\n\n{ANALYSIS_MANDATE}\n\n"
            "The spine you write is COPIED into every section, so it must itself obey "
            "the firm's fidelity rules — an unattributed or invented claim here "
            "propagates to all of them:\n" + rules + "\n\n"
            "Your job is the spine, not the paper: the firm's reasoned read, the few "
            "risks that drive the decision, the cross-reads that create tension, and "
            "the canonical framing of items several sections will each touch. Be "
            "concise and decisive. Ground everything in the available facts; do not "
            "invent and do not import outside knowledge."
        )

    def _draft(self, model: DealModel, corrections: list[str] | None = None) -> ThesisSpine:
        facts = citable_facts(model)
        fact_lines = "\n".join(f"  {p} = {v}" for p, v in sorted(facts.items()))
        # The CRUX, computed deal-agnostically by the materiality engine. The spine's
        # dominant risks MUST reflect the top items — this is what stops it missing
        # the point or padding generically.
        crux = assess_materiality(model)[:6]
        crux_lines = "\n".join(f"  [{i.tier}] {i.label}" for i in crux) or "  (none computed)"
        corr = ""
        if corrections:
            corr = (
                "\n\nFIX THESE before finalising (the spine propagates to every "
                "section, so an error here repeats everywhere):\n" + "\n".join(f"- {c}" for c in corrections)
            )
        # Plain-text labelled protocol, NOT JSON: a truncated response degrades to
        # partial fields rather than parsing to nothing. Brevity enforced.
        user = (
            f"AVAILABLE FACTS (path = value):\n{fact_lines}\n\n"
            f"THE CRUX — computed materiality, ranked (your DOMINANT RISKS and TENSIONS "
            f"must reflect the HIGH items; go deep on these, be crisp elsewhere):\n"
            f"{crux_lines}\n\n"
            "Write the deal thesis in THIS exact plain-text format (no JSON, no "
            "preamble, no markdown):\n"
            "OVERALL READ: <=60 words — does the deal hang together for the lender, "
            "and why/why not\n"
            "DOMINANT RISKS:\n- <=12 words\n- <=12 words   (1-3 lines, ranked)\n"
            "KEY TENSIONS:\n- <=15 words   (1-3 lines)\n"
            "FRAMING exit: <=20 words\n"
            "FRAMING covenants: <=20 words\n"
            "FRAMING security: <=20 words\n"
            "FRAMING valuation: <=20 words\n\n"
            "ATTRIBUTE every external or sponsor claim in the framing (write 'the "
            "sponsor states demand and limited supply', NOT 'demand and limited "
            "supply' as fact) — the sections copy your framing verbatim. Do NOT state "
            "specific figures, dollar amounts or ratios — refer to them by meaning "
            "('negligible covenant headroom', 'a day-one shortfall against the as-is "
            "value'); the sections cite the exact metrics. Frame exit, covenants, "
            "security and valuation ONCE so every section is consistent." + corr
        )
        return _parse_spine(self._complete(self._system(), user))

    def build(self, model: DealModel, reviewer=None) -> ThesisSpine:
        """Draft the spine, then GATE it before propagation: completeness (no missed
        material signal) + optional fidelity review. One corrective redraft if either
        flags it — fixing the source prevents N downstream issues."""
        spine = self._draft(model)
        if not spine.present:
            return spine
        issues: list[str] = [
            f"You omitted a HIGH-materiality crux item: {it.label}" for it in missing_signals(model, spine)
        ]
        if reviewer is not None:
            report = reviewer.review(model, ProseBlock(section=0, text=_spine_text(spine)))
            issues += [f'[{f.category}] "{f.quote}": {f.explanation}' for f in report.findings]
        if issues:
            spine = self._draft(model, corrections=issues)
        return spine


def _parse_spine(text: str) -> ThesisSpine:
    overall: list[str] = []
    risks: list[str] = []
    tensions: list[str] = []
    framings: dict[str, str] = {}
    section: str | None = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("overall read:"):
            overall.append(s.split(":", 1)[1].strip())
            section = "overall"
        elif low.startswith("dominant risks"):
            section = "risks"
        elif low.startswith("key tensions"):
            section = "tensions"
        elif low.startswith("framing"):
            rest = s[len("framing") :].lstrip()
            if ":" in rest:
                k, v = rest.split(":", 1)
                if v.strip():
                    framings[k.strip().lower()] = v.strip()
            section = None
        elif s.startswith(("- ", "• ")):
            item = s[2:].strip()
            if section == "risks":
                risks.append(item)
            elif section == "tensions":
                tensions.append(item)
        elif section == "overall":
            overall.append(s)
    read = " ".join(overall).strip()
    # present only if we actually recovered content — an empty spine must not be
    # injected (it would add noise, not coherence).
    return ThesisSpine(
        overall_read=read,
        dominant_risks=risks,
        key_tensions=tensions,
        canonical_framings=framings,
        present=bool(read or risks or framings),
    )
