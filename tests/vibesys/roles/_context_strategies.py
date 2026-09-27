"""Shared hypothesis strategies for building valid ``Role`` context/reply instances.

Not a test module itself (leading underscore keeps pytest from collecting
it): ``test_role_contracts.py`` and ``test_reply_properties.py`` both import
from here so the two files agree on what "a minimal valid instance" means
for a given pydantic model.

Two model-specific wrinkles, both worked around here rather than in the
tests that use them:

- **Nested ``default_factory`` fields.** Hypothesis's Pydantic plugin
  generates a field-type-inferred strategy for a nested ``BaseModel`` (e.g.
  ``PlanContext.profiler_summary: ProfilerSummary | None``) that does not
  apply that nested model's own ``default_factory`` correctly, so a plain
  ``st.builds(PlanContext)`` fails validation on ``ProfilerSummary.metrics``.
  Building the nested model independently with ``st.builds(NestedModel)``
  and passing it in as an explicit ``st.builds(Parent, field=...)`` override
  sidesteps the bug (verified against the current hypothesis/pydantic
  versions pinned in ``uv.lock``).
- **``modality``.** Several templates gate a dynamic
  ``{% include "_modality/" ~ modality ~ "/..." %}`` on this field; only a
  falsy value (``None``) is guaranteed to resolve without needing to pick a
  real modality directory name out of ``prompts/shared/_modality/``. Every
  context model with a ``modality`` field gets it forced to ``None``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import strategies as st

from vibesys.evaluators.perf_reply import ProfilerSummary
from vibesys.roles.designer import PlanContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

_PROFILER_SUMMARY = st.builds(ProfilerSummary, metrics=st.just({}))

# Context-model field overrides, keyed by the exact model class (never by
# field name alone: the same field name can mean different things on
# different models.
_CONTEXT_OVERRIDES: dict[type[BaseModel], dict[str, st.SearchStrategy]] = {
    PlanContext: {"profiler_summary": st.none() | _PROFILER_SUMMARY},
}

_CONTEXT_VALIDITY: dict[type[BaseModel], Callable[[BaseModel], bool]] = {}


def context_strategy(model: type[BaseModel]) -> st.SearchStrategy[BaseModel]:
    """A hypothesis strategy of valid instances of a ``Role.context`` model."""
    overrides = dict(_CONTEXT_OVERRIDES.get(model, {}))
    if "modality" in model.model_fields:
        overrides.setdefault("modality", st.none())
    strategy = st.builds(model, **overrides)
    validity = _CONTEXT_VALIDITY.get(model)
    return strategy if validity is None else strategy.filter(validity)


def reply_strategy(model: type[BaseModel]) -> st.SearchStrategy[BaseModel]:
    """A hypothesis strategy of valid instances of a ``Role.reply`` model."""
    return st.builds(model)
