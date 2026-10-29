"""The labelled set for scripts/support_eval.py: negation and role sentences.

Every sentence is invented text. Most come from the Task 4 reviewers' probe
corpora (round 3 re-review, round 4 re-review and round 5: the negation, role
and after-value attacks), plus the ten escapes test_verify.py deferred to the
semantic leg. The label is the answer to the support question - does this
sentence state that FIELD = VALUE? - not the deterministic verifier's verdict.
Sentences whose answer a careful analyst would not settle ("it is not unlikely
that the limit will be ...", "does not include GST approval") are left out.
"""

# ruff: noqa: E501 - one labelled case per line reads better than wrapped ones
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Case:
    id: str
    category: str  # negation | role | control
    field: str
    definition: str
    value: object
    kind: str | None
    sentence: str
    expected: str  # yes | no
    expected_reason: str | None = None  # for "no": NEGATED | WRONG_ROLE | DIFFERENT_BASIS | OTHER


LIMIT = ("facility.limit", "The approved facility limit: the maximum principal the lender commits to lend.")
GUARANTOR = ("parties.guarantor.name", "The name of the party that guarantees the facility.")
LVR = ("covenants.lvr.max", "The maximum loan-to-value ratio covenant.")
LCR = ("covenants.lcr.max", "The maximum loan-to-cost ratio covenant.")
ICR = ("covenants.icr.min", "The minimum interest cover ratio covenant.")
TR_A = ("facility.tranches[tranche-a].limit", "The limit of Tranche A.")
TR_B = ("facility.tranches[tranche-b].limit", "The limit of Tranche B.")
MARGIN = ("pricing.margin", "The margin over the base rate, per annum.")
RATE = ("pricing.interest_rate", "The all-in interest rate charged, per annum.")
EST_FEE = ("pricing.establishment_fee", "The establishment fee, as a percentage of the limit.")
MATURITY = ("facility.maturity_date", "The date the facility must be repaid in full.")
TERM = ("facility.term_months", "The initial term of the facility, in months.")
VALUATION = ("security.valuation.as_is", "The as-is valuation of the security property.")
CONTRACT_EX = ("costs.contract_sum.ex_gst", "The building contract sum, excluding GST.")
CONTRACT_INC = ("costs.contract_sum.inc_gst", "The building contract sum, including GST.")
INITIAL_LIMIT = ("facility.initial_limit", "The facility limit at first drawdown, before any reduction.")

M, P, D, N = "money", "percent", "date", "number"
L = 12400000


def _c(
    id: str,
    category: str,
    fd: tuple[str, str],
    value: object,
    kind: str | None,
    sentence: str,
    expected: str,
    reason: str | None = None,
) -> Case:
    return Case(id, category, fd[0], fd[1], value, kind, sentence, expected, reason)


# fmt: off
CASES: tuple[Case, ...] = (
    # --- negation: the value is stated, and denied or not in effect ------------
    _c("neg-01", "negation", LIMIT, L, M, "The Facility Limit of $12,400,000 is the figure that is not approved by the committee.", "no", "NEGATED"),
    _c("neg-02", "negation", LIMIT, L, M, "The Facility Limit of $12,400,000 has no approval from the credit committee for the works.", "no", "NEGATED"),
    _c("neg-03", "negation", LIMIT, L, M, "The Facility Limit of $12,400,000 lacks approval from the credit committee for the works.", "no", "NEGATED"),
    _c("neg-04", "negation", LIMIT, L, M, "The Facility Limit of $12,400,000 is invalid and unenforceable for the construction works.", "no", "NEGATED"),
    _c("neg-05", "negation", LIMIT, L, M, "The Facility Limit of $12,400,000 is pending, and is subject to approval that was refused.", "no", "NEGATED"),
    _c("neg-06", "negation", LIMIT, L, M, "The Facility Limit of $12,400,000 is not inclusive of GST or approved for the construction.", "no", "NEGATED"),
    _c("neg-07", "negation", LIMIT, L, M, "The Facility Limit is $12,400,000 and the borrower's request for it was denied last week.", "no", "NEGATED"),
    _c("neg-08", "negation", GUARANTOR, "Example Holdings Pty Ltd", None, "The guarantor is Example Holdings Pty Ltd, whose guarantee was denied for the facility.", "no", "NEGATED"),
    _c("neg-09", "negation", LIMIT, L, M, "The amount of $12,400,000 is not the approved limit under this facility agreement.", "no", "NEGATED"),
    _c("neg-10", "negation", LIMIT, L, M, "A facility limit of $12,400,000 was rejected by the credit committee on review.", "no", "NEGATED"),
    _c("neg-11", "negation", LIMIT, L, M, "The facility limit is no longer $12,400,000 following the variation this month.", "no", "NEGATED"),
    _c("neg-12", "negation", LIMIT, L, M, "The facility limit ceased to be $12,400,000 following the variation this month.", "no", "NEGATED"),
    _c("neg-13", "negation", LIMIT, L, M, "The facility limit, formerly $12,400,000, has been varied this month by the lender.", "no", "NEGATED"),
    _c("neg-14", "negation", LIMIT, L, M, "The facility limit was previously $12,400,000 before the variation this month took effect.", "no", "NEGATED"),
    _c("neg-15", "negation", LIMIT, L, M, "All amounts apart from $12,400,000 are approved by the committee this month for the project.", "no", "NEGATED"),
    _c("neg-16", "negation", LIMIT, L, M, "The approval covers stage one but not $12,400,000 for the second stage works.", "no", "NEGATED"),
    _c("neg-17", "negation", LIMIT, L, M, "THE FACILITY LIMIT IS NOT $12,400,000 UNDER THE REVISED TERMS OF THE OFFER.", "no", "NEGATED"),
    _c("neg-18", "negation", LIMIT, L, M, "The facility limit isn't $12,400,000 under the revised terms of the offer letter.", "no", "NEGATED"),
    _c("neg-19", "negation", GUARANTOR, "Example Holdings Pty Ltd", None, "Guarantor: not Example Holdings Pty Ltd, which has been released by the Lender.", "no", "NEGATED"),
    _c("neg-20", "negation", GUARANTOR, "Example Holdings Pty Ltd", None, "The guarantor will be Beta Pty Ltd rather than Example Holdings Pty Ltd per the letter.", "no", "NEGATED"),
    _c("neg-21", "negation", LIMIT, L, M, "Requested Facility Limit: $12,400,000: NOT APPROVED by the credit committee on review.", "no", "NEGATED"),
    _c("neg-22", "negation", LIMIT, L, M, "The Facility Limit is $12,400,000 (status: not approved) for the construction works here.", "no", "NEGATED"),
    _c("neg-23", "negation", LIMIT, L, M, "The Facility Limit of $12,400,000 was withdrawn by the lender for the construction works.", "no", "NEGATED"),
    _c("neg-24", "negation", LIMIT, L, M, "The Facility Limit of $12,400,000 hasn’t been approved by the lender for the works.", "no", "NEGATED"),
    _c("neg-25", "negation", LVR, 65, P, "The LVR covenant is 65% (not agreed) under the facility agreement for the works.", "no", "NEGATED"),
    _c("neg-26", "negation", LVR, 65, P, "The LVR of 65% is, however, not approved by the credit committee on review.", "no", "NEGATED"),
    _c("neg-27", "negation", GUARANTOR, "Example Holdings Pty Ltd", None, "The guarantor, Example Holdings Pty Ltd, is not a party to the facility agreement.", "no", "NEGATED"),
    _c("neg-28", "negation", LIMIT, L, M, "The Facility Limit of $12,400,000 is replaced by $10,000,000 following the review today.", "no", "NEGATED"),
    _c("neg-29", "negation", LIMIT, L, M, "The Facility Limit of $12,400,000 was never approved by the lender for the works here.", "no", "NEGATED"),
    _c("neg-30", "negation", LIMIT, L, M, "The Facility Limit of $12,400,000 is no longer available to the borrower for the works.", "no", "NEGATED"),
    # --- controls: negation-looking wording that still states the value -------
    _c("ctl-01", "control", LIMIT, L, M, "The Facility Limit is $12,400,000 and must not be exceeded at any time during the term.", "yes"),
    _c("ctl-02", "control", LIMIT, L, M, "The Facility Limit is $12,400,000 and the lender will not charge a line fee this year.", "yes"),
    _c("ctl-03", "control", LIMIT, L, M, "The Facility Limit is $12,400,000 unless the lender does not approve an increase later.", "yes"),
    _c("ctl-04", "control", LIMIT, L, M, "The limit is $12,400,000 unless the presales hurdle is not met by the sponsor.", "yes"),
    _c("ctl-05", "control", LIMIT, L, M, "The Facility Limit is $12,400,000, with no fees payable on the undrawn balance here.", "yes"),
    _c("ctl-06", "control", LIMIT, L, M, "The Facility Limit is $12,400,000, which the borrower cannot increase without consent.", "yes"),
    _c("ctl-07", "control", LIMIT, L, M, "The Facility Limit is $12,400,000 for works that do not include the stage two land.", "yes"),
    _c("ctl-08", "control", LIMIT, L, M, "The limit (not including capitalised interest) is $12,400,000 for the facility term.", "yes"),
    _c("ctl-09", "control", LIMIT, L, M, "Deposited Plan No. 12345 secures the facility limit of $12,400,000 for this loan.", "yes"),
    _c("ctl-10", "control", LIMIT, L, M, "The borrower is not a trust, and the facility limit is $12,400,000 for the term.", "yes"),
    _c("ctl-11", "control", LIMIT, L, M, "Notwithstanding clause 5, the facility limit is $12,400,000 for the term of the loan.", "yes"),
    _c("ctl-12", "control", CONTRACT_EX, 4120000, M, "The contract sum, excluding GST, is $4,120,000 under the fixed price building contract.", "yes"),
    _c("ctl-13", "control", LIMIT, L, M, "The facility is without recourse to the sponsor and the limit is $12,400,000 in total.", "yes"),
    _c("ctl-14", "control", LVR, 65, P, "The LVR must not exceed 65% at any time during the term of the facility.", "yes"),
    _c("ctl-15", "control", ICR, "1.5x", None, "The interest cover ratio must be no less than 1.5x at each test date.", "yes"),
    _c("ctl-16", "control", LIMIT, L, M, "Mezzanine: not relevant; the limit is $12,400,000 for the works under the contract.", "yes"),
    _c("ctl-17", "control", LIMIT, L, M, "The Facility Limit is $12,400,000 (see note 4) for the construction of the works here.", "yes"),
    _c("ctl-18", "control", GUARANTOR, "Beta Pty Ltd", None, "The guarantor will be Beta Pty Ltd rather than Example Holdings Pty Ltd per the letter.", "yes"),
    _c("ctl-19", "control", LIMIT, 10000000, M, "The Facility Limit of $12,400,000 is replaced by $10,000,000 following the review today.", "yes"),
    _c("ctl-20", "control", LIMIT, L, M, "The Facility Limit is $12,400,000 and cannot be exceeded without lender consent here.", "yes"),
    _c("ctl-21", "control", LIMIT, L, M, "The Facility Limit is $12,400,000 on a declining balance basis for the construction works.", "yes"),
    _c("ctl-22", "control", LIMIT, L, M, "The Facility Limit is $12,400,000, subject to a cancellation fee of 1% for the works.", "yes"),
    # --- role: which figure is which ------------------------------------------
    _c("role-01", "role", TR_B, 6000000, M, "Tranche A is $5,000,000 and Tranche B is $6,000,000 under this facility.", "yes"),
    _c("role-02", "role", TR_A, 6000000, M, "Tranche A is $5,000,000 and Tranche B is $6,000,000 under this facility.", "no", "WRONG_ROLE"),
    _c("role-03", "role", TR_A, 5000000, M, "Tranche A is $5,000,000 and Tranche B is $6,000,000 under this facility.", "yes"),
    _c("role-04", "role", LVR, 80, P, "The LVR covenant is 65% and the LCR covenant is 80 per cent for the term.", "no", "WRONG_ROLE"),
    _c("role-05", "role", LVR, 65, P, "The LVR covenant is 65% and the LCR covenant is 80 per cent for the term.", "yes"),
    _c("role-06", "role", LCR, 80, P, "The LVR covenant is 65% and the LCR covenant is 80 per cent for the term.", "yes"),
    _c("role-07", "role", INITIAL_LIMIT, 6000000, M, "The facility limit is $6,000,000 (reducing to $5,000,000 after first sale).", "yes"),
    _c("role-08", "role", INITIAL_LIMIT, 5000000, M, "The facility limit is $6,000,000 (reducing to $5,000,000 after first sale).", "no", "WRONG_ROLE"),
    _c("role-09", "role", MATURITY, date(2027, 6, 30), D, "Maturity is 30 June 2027 and the first interest date is 30 September 2026 for this loan.", "yes"),
    _c("role-10", "role", MATURITY, date(2026, 9, 30), D, "Maturity is 30 June 2027 and the first interest date is 30 September 2026 for this loan.", "no", "WRONG_ROLE"),
    _c("role-11", "role", TERM, 12, N, "The term is 12 months with an option to extend by 24 months at our discretion.", "yes"),
    _c("role-12", "role", TERM, 24, N, "The term is 12 months with an option to extend by 24 months at our discretion.", "no", "WRONG_ROLE"),
    _c("role-13", "role", MARGIN, 5.25, P, "Base 3.25% p.a. and margin 5.25% p.a. are payable monthly in arrears by the borrower.", "yes"),
    _c("role-14", "role", MARGIN, 3.25, P, "Base 3.25% p.a. and margin 5.25% p.a. are payable monthly in arrears by the borrower.", "no", "WRONG_ROLE"),
    _c("role-15", "role", EST_FEE, 1.0, P, "Interest rate is 8.95% p.a. and an establishment fee of 1.00% is payable upfront.", "yes"),
    _c("role-16", "role", RATE, 1.0, P, "Interest rate is 8.95% p.a. and an establishment fee of 1.00% is payable upfront.", "no", "WRONG_ROLE"),
    _c("role-17", "role", LIMIT, L, M, "A facility limit of $12,400,000 being 65% of the valuation of $19,076,923 applies.", "yes"),
    _c("role-18", "role", VALUATION, L, M, "A facility limit of $12,400,000 being 65% of the valuation of $19,076,923 applies.", "no", "WRONG_ROLE"),
    _c("role-19", "role", VALUATION, 19076923, M, "A facility limit of $12,400,000 being 65% of the valuation of $19,076,923 applies.", "yes"),
    _c("role-20", "role", TR_B, 6000000, M, "Facility | Tranche A | $5,000,000 | Tranche B | $6,000,000 | total", "yes"),
    _c("role-21", "role", LIMIT, L, M, "Requested Facility Limit: $12,400,000; approved Facility Limit: $11,000,000 by the credit committee.", "no", "WRONG_ROLE"),
    _c("role-22", "role", LIMIT, 11000000, M, "Requested Facility Limit: $12,400,000; approved Facility Limit: $11,000,000 by the credit committee.", "yes"),
    _c("role-23", "role", MARGIN, 4.5, P, "Interest is charged at BBSY plus a margin of 4.50% per annum on the drawn balance.", "yes"),
    _c("role-24", "role", RATE, 4.5, P, "Interest is charged at BBSY plus a margin of 4.50% per annum on the drawn balance.", "no", "DIFFERENT_BASIS"),
    _c("role-25", "role", CONTRACT_INC, 4120000, M, "The contract sum, excluding GST, is $4,120,000 under the fixed price building contract.", "no", "DIFFERENT_BASIS"),
    _c("role-26", "role", MARGIN, 5.25, P, "The margin will be in the range 3.25%–5.25% depending on the LVR tier achieved.", "no", "DIFFERENT_BASIS"),
)
# fmt: on

# Neutral text around each sentence, so the model sees a page, not a bare line.
BEFORE = "Schedule 1 - Facility terms.\n"
AFTER = "\nThe remaining terms are set out in the facility agreement."
