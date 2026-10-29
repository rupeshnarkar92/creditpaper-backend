"""The base every typed model in this system is built on.

One line of configuration, and it is load-bearing: pydantic does not validate
assignment by default, so `model.thesis = "junk"` and `deal.name = "not a Fact"`
both succeeded and produced a model whose declared types were simply untrue.

That matters more here than in most codebases. D1 says the typed deal model IS
the product; every stage after extraction — derivation, the check registry,
citation resolution, the renderer, the API — reads those types and trusts them.
`apply()` had grown its own declared-type coercion to compensate, which covered
the writes that went through `apply()` and nothing else. This makes the guarantee
belong to the model itself.

It lives in `contracts/` because it is the lowest layer: `ProseBlock` and `Signal`
are contract types and need the same rule as the engine's deal classes, and a
second copy of the policy would be a second thing to keep in step.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DealBase(BaseModel):
    """A pydantic model that re-validates when a field is assigned.

    Not a performance concern at this scale: a deal model is assembled once from
    a few hundred facts, not mutated in a loop.

    Note the limit, so it is not mistaken for more than it is — pydantic validates
    *field* assignment, not mutation of a container a field holds.
    `model.prose[9] = "text"` is a dict mutation and still slips past, which is
    why `apply.set_prose()` exists and why `tests/test_write_path.py` guards it
    structurally.
    """

    model_config = ConfigDict(validate_assignment=True)
