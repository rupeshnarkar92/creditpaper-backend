"""Derived fields: facts computed from other anchored facts (DECISIONS.md 2026-06-15).

Equity is not asserted, it is derived. The funding identity is

    total = senior + equity

so given any TWO of the three on a line, the engine computes the THIRD — whichever
is missing. Derived facts carry ExtractionMethod.DERIVED provenance recording the
formula and the input field paths, so they stay citable (D4) and renderable, and a
check can re-run the derivation to confirm the stored value (tamper-evident).

When all three are present (over-determined) the engine validates the identity
instead of computing, flagging any mismatch — exactly the cross-check that catches a
funding table whose line items do not sum to its stated totals.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from creditpaper.model.citations import resolve_path
from creditpaper.model.core import ExtractionMethod, Fact, FactStatus, Provenance
from creditpaper.model.deal import DealModel, DealType, DeliveryModel, FundingLine, deal_type_str

_TRIPLE = ("total", "senior", "equity")

# Funding-identity rounding band: the senior column may differ from the stated
# facility by dollar-level rounding without being an error (see derive_economics).
_FUNDING_ROUNDING_TOL = Decimal("2")


def _present(f: Fact | None) -> bool:
    return f is not None and f.value is not None and f.status != FactStatus.MISSING


def derived_fact(value: Decimal | str, formula: str, inputs: list[str], sources: Sequence[Fact] = ()) -> Fact:
    """A Fact established by computation; the anchor is the formula, not a page.

    A derived fact is only as settled as what it was computed from. This used to
    hardcode `EXTRACTED` at confidence 1.0 regardless, which LAUNDERED a flag: a
    metric derived from a FLAGGED input came out looking settled, so a contested
    valuation rendered as a neutral LVR and `disclosure_citations` no longer saw
    it — bypassing check #17's requirement that every flagged fact be disclosed,
    via the one path that produces the paper's headline figures.

    So the status is the weakest of the inputs and the confidence is the minimum.
    Where no sources are supplied the old behaviour stands, which keeps the
    function usable for values genuinely derived from nothing else.
    """
    status = FactStatus.EXTRACTED
    confidence = 1.0
    if sources:
        if any(s.status == FactStatus.FLAGGED for s in sources):
            status = FactStatus.FLAGGED
        known = [s.confidence for s in sources if s.confidence is not None]
        confidence = min(known) if known else 1.0
    return Fact(
        value=value,
        status=status,
        confidence=confidence,
        provenance=Provenance(
            document_id="(derived)",
            location=formula,
            method=ExtractionMethod.DERIVED,
            inputs=list(inputs),
        ),
    )


@dataclass
class DerivationReport:
    derived: list[str] = field(default_factory=list)
    # (path, expected, found) where an over-determined identity did not hold:
    inconsistent: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not self.inconsistent


def _derive_line(line: FundingLine, base: str) -> tuple[str, Fact] | None:
    """Return (column, Fact) for the one missing column, or None if not exactly
    one of the three is missing."""
    present = {name for name in _TRIPLE if _present(getattr(line, name))}
    if present == {"senior", "equity"}:
        return "total", derived_fact(
            line.senior.value + line.equity.value,
            "total = senior + equity",
            [f"{base}.senior", f"{base}.equity"],
            [line.senior, line.equity],
        )
    if present == {"total", "equity"}:
        return "senior", derived_fact(
            line.total.value - line.equity.value,
            "senior = total - equity",
            [f"{base}.total", f"{base}.equity"],
            [line.total, line.equity],
        )
    if present == {"total", "senior"}:
        return "equity", derived_fact(
            line.total.value - line.senior.value,
            "equity = total - senior",
            [f"{base}.total", f"{base}.senior"],
            [line.total, line.senior],
        )
    return None


def derive_economics(model: DealModel) -> DerivationReport:
    """Fill the missing third of every funding-table line and compute the
    aggregates (total_cost, senior_total, equity_total) as DERIVED facts.
    Over-determined lines are validated, not recomputed. Idempotent."""
    report = DerivationReport()
    ft = model.economics.funding_table

    for key, line in ft.items():
        base = f"economics.funding_table[{key}]"
        present = {name for name in _TRIPLE if _present(getattr(line, name))}
        if len(present) == 3:
            expected = line.senior.value + line.equity.value
            if line.total.value != expected:
                report.inconsistent.append((f"{base}.total", expected, line.total.value))
            continue
        result = _derive_line(line, base)
        if result is not None:
            col, fact = result
            setattr(line, col, fact)
            report.derived.append(f"{base}.{col}")

    if not ft:
        return report

    def _sum(col: str) -> tuple[Decimal, list[str], list[Fact]]:
        total = Decimal(0)
        inputs: list[str] = []
        sources: list[Fact] = []
        for key, line in ft.items():
            f = getattr(line, col)
            if _present(f):
                total += f.value
                inputs.append(f"economics.funding_table[{key}].{col}")
                sources.append(f)
        return total, inputs, sources

    tc, tci, tcs = _sum("total")
    st, sti, sts = _sum("senior")
    eq, eqi, eqs = _sum("equity")
    model.economics.total_cost = derived_fact(tc, "total_cost = sum(line.total)", tci, tcs)
    model.economics.senior_total = derived_fact(st, "senior_total = sum(line.senior)", sti, sts)
    model.economics.equity_total = derived_fact(eq, "equity_total = sum(line.equity)", eqi, eqs)
    report.derived += [
        "economics.total_cost",
        "economics.senior_total",
        "economics.equity_total",
    ]

    # Cross-check: the funding table's senior column should sum to the facility.
    # Tolerate a couple of dollars of rounding — the ITS often states the facility
    # rounded while listing lines to the dollar (a $1 gap held the whole Wattlefield
    # reconciliation, 2026-06-16). The band is deliberately narrow: rounding is O($1),
    # while real misreads are a whole funding line (the original shipped error was $19,
    # and a double-count is millions), so the catch still fires on genuine errors.
    fa = model.economics.facility_amount
    if _present(fa) and abs(st - Decimal(fa.value)) > _FUNDING_ROUNDING_TOL:
        report.inconsistent.append(("economics.senior_total", fa.value, st))

    return report


def _cov(model: DealModel, kind: str) -> tuple[str | None, object | None, list[str]]:
    """The governing LVR / LCR covenant, plus any rivals for the same headline.

    Returns the CANONICAL key (`lvr_max` / `lcr_max`) when present, falling back to
    any key with the prefix. Previously this returned the first match in dict
    order, so which covenant governed the ceiling, headroom and breach figures
    depended on extractor insertion order — an ITS stating two LVR covenants could
    produce different headline numbers run to run, with the provenance confidently
    naming the wrong one.

    Rivals are returned rather than silently dropped: two LVR covenants is a real
    thing an ITS can say, and which one binds is a credit question, not something
    to settle by dict ordering.
    """
    matches = [k for k in model.covenants if k.startswith(kind)]
    if not matches:
        return None, None, []
    canonical = f"{kind}_max"
    key = canonical if canonical in matches else sorted(matches)[0]
    return key, model.covenants[key], sorted(k for k in matches if k != key)


def _pct(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _whole(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def derive_metrics(model: DealModel) -> DerivationReport:
    """Compute the covenant + security sensitivities ONCE as DERIVED facts on
    `model.metrics`, so every section cites one canonical figure instead of
    recomputing in prose (review round 1, 2026-06-16). Each metric is skipped if
    its inputs are absent. Idempotent."""
    report = DerivationReport()
    m = model.metrics
    fa = model.economics.facility_amount
    tc = model.economics.total_cost
    basis_value, basis_key = _ongoing_basis(model)
    lvr_key, lvr_cov, lvr_rivals = _cov(model, "lvr")
    lcr_key, lcr_cov, lcr_rivals = _cov(model, "lcr")
    for kind, governing, rivals in (("lvr", lvr_key, lvr_rivals), ("lcr", lcr_key, lcr_rivals)):
        if rivals:
            # Not an arithmetic mismatch, but the same class of problem the
            # report exists to surface: a headline figure whose basis is not
            # obviously the right one. Which covenant binds is a credit question.
            report.inconsistent.append(
                (f"covenants[{governing}] governs {kind}_*; also present: {rivals}", Decimal(0), Decimal(0))
            )

    def _sources(inputs: list[str]) -> list[Fact]:
        """The Facts the input paths name. Resolved here rather than threaded
        through every call site, so the status a metric inherits can never drift
        from the provenance it records — they are the same list."""
        found = []
        for path in inputs:
            target = resolve_path(model, path)
            if isinstance(target, Fact):
                found.append(target)
        return found

    def emit(attr: str, value: Decimal | str, formula: str, inputs: list[str]) -> None:
        setattr(m, attr, derived_fact(value, formula, inputs, _sources(inputs)))
        report.derived.append(f"metrics.{attr}")

    # LVR family. The denominator is the basis selected for THIS loan, and every
    # figure in the family names it — a headroom figure taken on a different basis
    # from the LVR beside it answers a different question (DECISIONS.md 2026-08-21).
    if _present(fa) and basis_value is not None and basis_key is not None:
        f_in = "economics.facility_amount"
        a_in = f"valuation.values_by_basis[{basis_key}]"
        emit("lvr_basis", basis_key, "lvr_basis = the ongoing LVR denominator for this loan", [a_in])
        emit(
            "lvr_pct",
            _pct(Decimal(fa.value) / basis_value * 100),
            f"lvr_pct = facility / {basis_key} * 100",
            [f_in, a_in],
        )
        # A blank or "n/a" covenant cell misparsed as 0 divided straight into
        # `breach_value` and raised DivisionByZero out of derive_metrics, which
        # ingest does not catch — one unreadable cell and there is no paper at all.
        # A threshold of zero or less is also not a covenant anyone wrote. Hold it
        # and skip only the covenant-dependent LVR figures: the LCR family and the
        # day-one position have different inputs and are still computable.
        thr = (
            Decimal(lvr_cov.threshold_value.value) / 100
            if lvr_cov is not None and _present(lvr_cov.threshold_value)
            else None
        )
        if thr is not None and thr <= 0:
            report.inconsistent.append(
                (
                    f"covenants[{lvr_key}].threshold_value is not a usable covenant",
                    Decimal(0),
                    Decimal(lvr_cov.threshold_value.value),
                )
            )
            thr = None
        if thr is not None:
            t_in = f"covenants[{lvr_key}].threshold_value"
            ceiling = thr * basis_value
            breach_value = Decimal(fa.value) / thr
            emit(
                "lvr_ceiling_value",
                _whole(ceiling),
                f"lvr_ceiling_value = (threshold/100) * {basis_key}",
                [t_in, a_in],
            )
            emit(
                "lvr_headroom_amount",
                _whole(ceiling - Decimal(fa.value)),
                "lvr_headroom_amount = lvr_ceiling_value - facility",
                [t_in, a_in, f_in],
            )
            emit(
                "lvr_breach_value",
                _whole(breach_value),
                "lvr_breach_value = facility / (threshold/100)",
                [f_in, t_in],
            )
            emit(
                "lvr_breach_drop_pct",
                _pct((basis_value - breach_value) / basis_value * 100),
                f"lvr_breach_drop_pct = ({basis_key} - lvr_breach_value) / {basis_key} * 100",
                [a_in, f_in, t_in],
            )

    # LCR family (needs facility + total_cost; headroom needs the threshold).
    if _present(fa) and _present(tc) and tc.value:
        f_in = "economics.facility_amount"
        c_in = "economics.total_cost"
        emit(
            "lcr_pct",
            _pct(Decimal(fa.value) / tc.value * 100),
            "lcr_pct = facility / total_cost * 100",
            [f_in, c_in],
        )
        if lcr_cov is not None and _present(lcr_cov.threshold_value):
            t_in = f"covenants[{lcr_key}].threshold_value"
            ceiling = Decimal(lcr_cov.threshold_value.value) / 100 * tc.value
            emit(
                "lcr_ceiling_amount",
                _whole(ceiling),
                "lcr_ceiling_amount = (threshold/100) * total_cost",
                [t_in, c_in],
            )
            emit(
                "lcr_headroom_amount",
                _whole(ceiling - Decimal(fa.value)),
                "lcr_headroom_amount = lcr_ceiling_amount - facility",
                [t_in, c_in, f_in],
            )

    # Day-one position against the as-is value: LVR on the DAY-ONE ADVANCE, not the
    # total facility. A construction facility funds future construction drawn
    # progressively, so at close only the land/refinance line is drawn; the total
    # facility against as-is overstates the day-one position massively (caught on a
    # blind land-refinance+construction deal, 2026-06-16). For a deal drawn at close
    # (bridge / residual stock) the advance IS the facility.
    adv, adv_path = _day_one_advance(model)
    sec, sec_key = _standing_security(model)
    if adv is not None and sec is not None and sec and adv_path and sec_key:
        sec_path = f"valuation.values_by_basis[{sec_key}]"
        emit(
            "asis_lvr_pct",
            _pct(adv / sec * 100),
            f"asis_lvr_pct = day_one_advance / {sec_key} * 100",
            [adv_path, sec_path],
        )
        gap = adv - sec
        if gap > 0:  # a real day-one shortfall: over-advanced vs standing security
            emit(
                "asis_security_gap",
                _whole(gap),
                f"asis_security_gap = day_one_advance - {sec_key}",
                [adv_path, sec_path],
            )

    return report


# The ongoing LVR denominator, in preference order per deal type (DECISIONS.md
# 2026-08-21: "as if complete value or project related site value depending on the
# loan"). A construction facility is measured against the finished project; a land
# or site-secured facility has no as-if-complete value and is measured against the
# project-related site value. There is deliberately NO fallback to `as_is` — that
# is the day-one basis, and using it here would silently answer a different
# question with a plausible number.
_ONGOING_BASES: dict[str | None, tuple[str, ...]] = {
    "construction": ("as_if_complete", "prsv"),
    "residual_stock": ("as_if_complete_one_line", "as_if_complete"),
    "bridge": ("prsv", "as_if_complete"),
    None: ("as_if_complete", "prsv"),
}


def _ongoing_basis(model: DealModel) -> tuple[Decimal | None, str | None]:
    """The value the LVR is taken against for the remainder of the loan, and the
    basis key it came from. Returns (None, None) when no permitted basis is
    populated — the metric is then not emitted at all."""
    vb = model.valuation.values_by_basis
    order = _ONGOING_BASES.get(deal_type_str(model), _ONGOING_BASES[None])
    for key in order:
        f = vb.get(key)
        if _present(f) and f.value:
            return Decimal(f.value), key
    return None, None


def _standing_security(model: DealModel) -> tuple[Decimal | None, str | None]:
    """The value of the security as it stands at close — what a DAY-ONE LVR is
    taken against. Always the as-is valuation (DECISIONS.md 2026-08-21).

    One alias, for residual stock only: the asset is already complete, so no
    separate as-is basis exists and the valuer's in-one-line figure IS its as-is
    position. This is a substitution of basis, so it is named in the formula
    rather than applied silently — the previous version fell through
    `as_is -> as_if_complete_one_line` for every deal type "in order of
    conservatism", which let a completed-value basis render as a day-one LVR with
    nothing to show it had happened.
    """
    vb = model.valuation.values_by_basis
    f = vb.get("as_is")
    if _present(f):
        return Decimal(f.value), "as_is"
    if deal_type_str(model) == "residual_stock":
        f = vb.get("as_if_complete_one_line")
        if _present(f):
            return Decimal(f.value), "as_if_complete_one_line"
    return None, None


def _day_one_advance(model: DealModel) -> tuple[Decimal | None, str | None]:
    """The senior drawn at financial close. For a construction facility this is the
    land/refinance line (the rest funds future construction, drawn progressively);
    for a deal drawn at close (bridge / residual stock) it is the full facility."""
    dt_str = deal_type_str(model)
    if dt_str is None:
        # Without a deal type we cannot know what is drawn at close. Returning the
        # full facility here computed a 200% day-one LVR where the true figure was
        # 60%, and fabricated a security gap that drove materiality to
        # "over-advanced at close". Only >100% was caught downstream, so a
        # land-rich deal reported a believable number. Emit nothing instead.
        return None, None
    if dt_str == "construction":
        land = model.economics.funding_table.get("land")
        if land is not None and _present(land.senior):
            return Decimal(land.senior.value), "economics.funding_table[land].senior"
        return None, None
    fa = model.economics.facility_amount
    if _present(fa):
        return Decimal(fa.value), "economics.facility_amount"
    return None, None


def derive_delivery_model(model: DealModel) -> DerivationReport:
    """Classify the construction DELIVERY MODEL from artifacts — NOT from whether the
    builder is the sponsor (memory: construction-delivery-model-drives-builder-dd):
    owner-builder (no building contract and no third-party builder), related-party
    builder (a contract + the builder is a related group entity), or independent builder
    (arm's-length). Drives builder-DD extent, the material-document check and §2/§7
    framing. Construction deals only; derived like the metrics, citable (D4)."""
    rep = DerivationReport()
    if deal_type_str(model) != DealType.CONSTRUCTION.value:
        return rep  # delivery model is a construction concept

    bc = model.building_contract
    has_contract = _present(bc.contract_type) or _present(bc.contract_sum)
    b = model.builder
    has_builder = (
        _present(b.licence.number) or _present(b.site_manager) or _present(b.financials.cash_position)
    )
    # related = the builder shows up as a related GROUP entity (role mentions 'build')
    # in the sponsor structure, or a related-party flag names it.
    ents = model.sponsor.structure.entities.values()
    related = any(_present(e.role) and "build" in str(e.role.value).lower() for e in ents) or any(
        "build" in str(f.value).lower()
        for f in model.sponsor.structure.related_party_flags.values()
        if _present(f)
    )

    if related and (has_contract or has_builder):
        value, formula = (
            DeliveryModel.RELATED_PARTY_BUILDER,
            "related-party: building contract/builder + builder is a related group entity",
        )
    elif has_contract or has_builder:
        value, formula = (
            DeliveryModel.INDEPENDENT_BUILDER,
            "independent: building contract / third-party builder present, arm's-length",
        )
    else:
        value, formula = (
            DeliveryModel.OWNER_BUILDER,
            "owner-builder: no building contract and no third-party builder",
        )

    model.deal.delivery_model = Fact(
        value=value,
        status=FactStatus.EXTRACTED,
        confidence=1.0,
        provenance=Provenance(
            document_id="(derived)",
            location=formula,
            method=ExtractionMethod.DERIVED,
            inputs=[
                "building_contract.contract_type",
                "building_contract.contract_sum",
                "builder.licence.number",
                "sponsor.structure.entities",
            ],
        ),
    )
    rep.derived.append("deal.delivery_model")
    return rep
