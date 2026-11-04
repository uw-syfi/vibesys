"""Measured samples and the comparison of a candidate with its base.

One *measurement* is the set of conclusive runs of one measured input under one evaluation.
Each run is a ``Sample``, which cannot exist without the commit it measured and the digest of
what the evaluation read there. Samples of one input count together: nothing is picked among
them and nothing is refused for there being several (`vs_core._comparison`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from vs_core._generated.vibesys.core.v1.leaves_cv import (
    BaseRef,
    Comparison,
    Direction,
    Finite,
    Reading,
    Sample,
)
from vs_core._generated.vibesys.core.v1.vocabulary_cv import (
    ComparisonVerdict,
    EffectVerdict,
)

from .common import Count, RequestId, Scope, Seconds, Value, unset


class CoreRun(Value):
    """A run core itself asked for, on a candidate's behalf, in its scope.

    No decision and no agent call asked for it, so this record is its origin: core admitted it
    so that ``candidate`` (the run whose result needed it) has a comparison with a measured
    base. Its plan says which side it measures: the base, or (under no declared floor) the
    candidate's own input again (`vs_core._comparison`).
    """

    request_id: RequestId
    scope: Scope
    candidate: RequestId
    candidate_input: str = Field(min_length=1)
    """The measured input of ``candidate``: the comparison this run serves is of that input.

    A close call's stop is counted per comparison (`vs_core._comparison.base_run_stalled`), so
    another candidate's runs of the same base never count toward it.
    """
    fresh_when_asked: Count
    """How many fresh samples of the input it measures were held when core asked for it.

    A base run that lands without leaving more than this replaced a sample that went stale
    while it ran, and earns its close call no further base run (`vs_core._comparison`).
    """


class BaseClaim(Value):
    """A run of the strategy's own that core counts as samples of a base once it ends.

    The strategy measures the run's input before any agent works, from a plan that names no
    measured input. When an agent's evaluation then needs that base while the run is still
    going, core starts no second run of it: it claims the running one, so its result becomes the
    base's samples (``base``) when it ends, and the candidate's comparison follows.
    """

    request_id: RequestId
    base: BaseRef


class Runaway(Value):
    """An attempt whose agent has asked for an unusual number of evaluations.

    A warning and nothing more: it is recorded once per scope, when the count of the scope's
    admitted evaluations first reaches ``limit``, and it never refuses a call.
    """

    scope: Scope
    evaluations: Count
    limit: Count


class StatedComparison(Value):
    """A candidate's comparison with its base as a flow reads it from the run's view.

    ``settling`` is true while a run that will feed the comparison has not ended (core's own
    run of the base, or of the candidate's input, that a close call earned): the comparison
    changes when it lands, so it is not yet the one a verdict may rest on
    (`vs_core._comparison.comparison_settling`).
    """

    comparison: Comparison
    settling: bool
    answer_until: Seconds | None = Field(default=None, exclude_if=unset)
    """When the scope's latest official record of the candidate input stops moving
    (``OwnedJob.answer_until``): a flow that waits for this comparison takes its verdict then
    at the latest, on the record as it stands. None when no official run of it has ended."""


class ComparisonSettled(Value):
    """A run that fed a comparison ended, and no run that feeds it is under way any more.

    Core reports it at the end of the run, so a flow that waits for the comparison to settle
    (``ctx.evaluation.measure_compared``) wakes when the comparison it would read is final, and
    not at a poll. It names the comparison only: the run's view states it
    (``RunView.comparisons``), or no longer does when its base went stale meanwhile.
    """

    kind: Literal["comparison_settled"] = "comparison_settled"
    scope: Scope
    candidate_input: str = Field(min_length=1)
    base_input: str = Field(min_length=1)


__all__ = [
    "BaseClaim",
    "BaseRef",
    "Comparison",
    "ComparisonSettled",
    "ComparisonVerdict",
    "CoreRun",
    "Direction",
    "EffectVerdict",
    "Finite",
    "Reading",
    "Runaway",
    "Sample",
    "StatedComparison",
]
