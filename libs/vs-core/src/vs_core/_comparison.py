"""Samples of one measured input, their comparison with a base, and when a base is measured.

A *measurement* is every conclusive run of one measured input under one evaluation. Its runs
are ``Sample``s of it: they are aggregated, never picked among, and none is refused because
there are several. This module owns the pure decisions over samples:

- ``evaluation_of`` names an evaluation (everything but the commit and purpose), so two runs of
  one input in one host and gates are samples of one measurement;
- ``compare`` compares a candidate's samples with its base's: a ``Comparison``;
- ``base_missing`` and ``remeasures_base`` decide when the base is measured, and nothing else
  does.

The statistic
-------------

Let the candidate have ``m`` samples with mean ``c`` and the base ``n`` samples with mean ``b``.
The ratio is ``r = c / b``. A run's relative deviation is its distance from its own side's mean
over that mean. The runs' spread is the pooled relative deviation::

    s = sqrt(sum(relative deviations ** 2) / (m + n - 2))        (0 when m + n <= 2)

and it is never taken below the task's declared per-run noise ``relative_noise`` (the floor,
which is also all there is to go on while each side holds one sample)::

    sigma = max(s, floor)
    noise = r * sigma * sqrt(1/m + 1/n)

``noise`` is one standard error of the ratio of two means (first order). More samples shrink
it, a wider spread or a higher floor grow it, and a shared host shows as spread. A verdict
needs two standard errors, which is conservative (about 95%): ``better`` when the candidate
improves on the base by more than ``2 * noise`` (a ratio above one for a ``max`` metric, below
one for a ``min`` metric), ``worse`` when it is worse by more than that, and ``within_noise``
otherwise.

When the base is measured
-------------------------

The base is measured once per (measured input, evaluation): the first time a candidate is
compared with it and no run of it is held or running. It is measured again only when its
samples are stale (older than ``TaskTerms.base_staleness`` on the run clock) or when a
candidate's result is a close call, within ``CLOSE_CALL_NOISES`` of the bar, because one more
base sample narrows the noise. A base never gets more than ``BASE_MAX_SAMPLES`` fresh samples.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import TYPE_CHECKING

from ._values import digest
from .types.common import EvidenceKind, ObservationStatus, RevisionRef, Value
from .types.evaluation import (
    EvidenceRef,
    MeasurementIdentity,
    MeasurementStageIdentity,
    OwnedJob,
    PreparedSubmissionReceipt,
)
from .types.measurement import BaseRef, Comparison, ComparisonVerdict, Direction, Sample

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .types.common import RunFacts, Scope, Seconds
    from .types.evaluation import EvaluationState
    from .types.kernel import RunState

__all__ = [
    "BASE_MAX_SAMPLES",
    "CLOSE_CALL_NOISES",
    "VERDICT_NOISES",
    "ComparisonPolicy",
    "Measured",
    "Moment",
    "base_in_flight",
    "base_missing",
    "close_call",
    "compare",
    "comparison_of",
    "comparisons_of",
    "evaluation_of",
    "fresh",
    "latest_comparisons",
    "moment_of",
    "official_needs_samples",
    "policy_of",
    "remeasures_base",
    "sample_of",
    "samples_of",
]

VERDICT_NOISES = 2.0
"""How many standard errors a difference must exceed before it is better or worse."""
CLOSE_CALL_NOISES = 2.0
"""A result within this many noises of the bar is a close call: another sample is worth it."""
BASE_MAX_SAMPLES = 2
"""The most fresh samples a close call earns a base: one run, then one more, never a chase.

A base run costs as much as a candidate's, and an agent's turn waits for every run of its scope,
so a close call narrows the noise with one more base sample and stops there."""


@dataclass(frozen=True, slots=True)
class Measured:
    """The key of one measurement: an evaluation of one measured input."""

    evaluation: str
    measured_input: str


@dataclass(frozen=True, slots=True)
class ComparisonPolicy:
    """What the task fixes about comparing: its noise floor, base staleness and axes."""

    noise_floor: float = 0.0
    """The declared relative variation of one run (``TaskTerms.relative_noise``)."""
    base_max_age: float | None = None
    """Seconds after which a base sample is stale; None never lets one go stale."""
    directions: tuple[tuple[str, Direction], ...] = ()
    """The task's objectives by metric name, for a reading that states no direction."""


@dataclass(frozen=True, slots=True)
class Moment:
    """When a decision over samples is taken (the run clock) and the policy it is taken under."""

    now: Seconds
    policy: ComparisonPolicy


def moment_of(run: RunState) -> Moment:
    """The moment of a run's own clock, under the comparison policy its task fixes."""
    return Moment(now=run.now_at, policy=policy_of(run.facts))


def policy_of(facts: RunFacts) -> ComparisonPolicy:
    """The comparison policy a run's task fixes; the default for a run without task terms."""
    task = facts.task
    if task is None:
        return ComparisonPolicy()
    return ComparisonPolicy(
        noise_floor=task.relative_noise,
        base_max_age=task.base_staleness,
        directions=tuple((item.name, item.direction) for item in task.objectives),
    )


class _Evaluation(Value):
    """What two runs share when they are the same measurement of an input."""

    evaluator_digest: str
    workload_digest: str
    environment_digest: str
    recipe_digest: str
    stages: tuple[MeasurementStageIdentity, ...]
    accuracy_stage: str | None
    accuracy_gated: tuple[EvidenceKind, ...]


def evaluation_of(identity: MeasurementIdentity) -> str:
    """The digest of an evaluation: the identity of a run less its commit, purpose and input.

    Two runs share it exactly when they are the same measurement of an input: the same
    evaluator, workload, environment (host), recipe, gates and accuracy gating.
    """
    return digest(
        _Evaluation(
            evaluator_digest=identity.evaluator_digest,
            workload_digest=identity.workload_digest,
            environment_digest=identity.environment_digest,
            recipe_digest=identity.recipe_digest,
            stages=identity.stages,
            accuracy_stage=identity.accuracy_stage,
            accuracy_gated=identity.accuracy_gated,
        )
    )


def sample_of(
    job: OwnedJob, evidence: EvidenceRef, observed_at: Seconds, measured_input: str | None = None
) -> Sample | None:
    """The sample a finished run gives, or None when it is not a measurement record.

    The run must be a framework run (not a re-read of one: ``reuses`` is unset), and its
    benchmark record trusted, successful and carrying the headline reading. The input is the
    plan's own or, for a run whose plan names none, the ``measured_input`` the runtime
    digested at its commit; with neither there is no sample, since a sample cannot exist
    without its commit and the input the evaluation read there.
    """
    plan = job.plan
    named = plan.measured_input or measured_input
    if (
        job.reuses is not None
        or evidence.kind != EvidenceKind.BENCHMARK
        or evidence.status != ObservationStatus.SUCCEEDED
        or evidence.provenance != "trusted"
        or evidence.reading is None
        or named is None
        or not isinstance(plan.candidate, RevisionRef)
    ):
        return None
    return Sample(
        run=job.submission_id,
        scope=job.scope,
        commit=plan.candidate,
        measured_input=named,
        evaluation=evaluation_of(MeasurementIdentity.from_plan(plan, plan.candidate)),
        gates=tuple(stage.stage_id for stage in plan.stages),
        host=plan.environment_digest,
        dedicated_resources=plan.dedicated_measurement_resources,
        reading=evidence.reading,
        observed_at=observed_at,
        base=job.base,
    )


def samples_of(state: EvaluationState, measured: Measured) -> tuple[Sample, ...]:
    """Every sample of one measurement, in the order its runs ended."""
    return tuple(
        sample
        for sample in state.samples
        if sample.evaluation == measured.evaluation
        and sample.measured_input == measured.measured_input
    )


def fresh(samples: Iterable[Sample], moment: Moment) -> tuple[Sample, ...]:
    """The samples young enough to count at ``moment``."""
    limit = moment.policy.base_max_age
    return tuple(s for s in samples if limit is None or moment.now - s.observed_at <= limit)


def _mean(values: tuple[float, ...]) -> float:
    return sum(values) / len(values)


def _direction(samples: tuple[Sample, ...], policy: ComparisonPolicy) -> Direction | None:
    stated = {s.reading.direction for s in samples}
    if len(stated) == 1 and None not in stated:
        return next(iter(stated))
    if stated != {None}:
        return None
    named = dict(policy.directions)
    return named.get(samples[0].reading.metric)


def compare(
    candidate: tuple[Sample, ...],
    base: tuple[Sample, ...],
    *,
    scope: Scope,
    anchor: BaseRef,
    policy: ComparisonPolicy,
) -> Comparison | None:
    """The candidate's samples compared with the base's, or None when they cannot be compared.

    ``anchor`` names the base the samples are compared with. They cannot be compared when
    either side is empty, the samples read different metrics, no direction is known for the
    metric, or the base's mean is zero (a ratio over it means nothing).
    """
    if not candidate or not base:
        return None
    both = (*candidate, *base)
    reading = candidate[-1].reading
    direction = _direction(both, policy)
    if direction is None or {s.reading.metric for s in both} != {reading.metric}:
        return None
    mine = tuple(s.reading.value for s in candidate)
    theirs = tuple(s.reading.value for s in base)
    c, b = _mean(mine), _mean(theirs)
    if b == 0:
        return None
    ratio = c / b
    m, n = len(mine), len(theirs)
    spread = _pooled_spread(mine, c, theirs, b)
    sigma = max(spread, policy.noise_floor)
    noise = abs(ratio) * sigma * sqrt(1 / m + 1 / n)
    gain = ratio - 1 if direction == "max" else 1 - ratio
    verdict = (
        ComparisonVerdict.BETTER
        if gain > VERDICT_NOISES * noise
        else ComparisonVerdict.WORSE
        if gain < -VERDICT_NOISES * noise
        else ComparisonVerdict.WITHIN_NOISE
    )
    last = candidate[-1]
    return Comparison(
        scope=scope,
        metric=reading.metric,
        unit=reading.unit,
        direction=direction,
        candidate=last.commit,
        candidate_input=last.measured_input,
        base=anchor.commit,
        base_input=anchor.measured_input,
        candidate_value=c,
        base_value=b,
        ratio=ratio,
        noise=noise,
        verdict=verdict,
        candidate_samples=m,
        base_samples=n,
    )


def _pooled_spread(mine: tuple[float, ...], c: float, theirs: tuple[float, ...], b: float) -> float:
    """The pooled relative deviation of the runs from their own side's mean (0 below 3 runs)."""
    degrees = len(mine) + len(theirs) - 2
    if degrees <= 0 or c == 0 or b == 0:
        return 0.0
    squares = sum(((x - c) / c) ** 2 for x in mine) + sum(((x - b) / b) ** 2 for x in theirs)
    return sqrt(squares / degrees)


def comparison_of(
    state: EvaluationState, scope: Scope, measured: Measured, moment: Moment
) -> Comparison | None:
    """The scope's candidate input compared with its base, over every sample held at ``moment``.

    Every run of the input in the scope is a sample of it, whoever asked for the run (the
    strategy's own check adds one to the agent's); the base is the one an agent's run named.
    """
    mine = tuple(s for s in samples_of(state, measured) if s.scope == scope)
    anchor = next((s.base for s in reversed(mine) if s.base is not None), None)
    if anchor is None:
        return None
    base = fresh(samples_of(state, Measured(measured.evaluation, anchor.measured_input)), moment)
    return compare(mine, base, scope=scope, anchor=anchor, policy=moment.policy)


def comparisons_of(state: EvaluationState, moment: Moment) -> tuple[Comparison, ...]:
    """The current comparison of every (scope, candidate input) that has a base's samples."""
    seen: dict[tuple[str, str, str], Sample] = {}
    for sample in state.samples:
        if sample.base is not None:
            key = (sample.scope.model_dump_json(), sample.evaluation, sample.measured_input)
            seen[key] = sample
    found = (
        comparison_of(state, s.scope, Measured(s.evaluation, s.measured_input), moment)
        for s in seen.values()
    )
    return tuple(item for item in found if item is not None)


def latest_comparisons(
    state: EvaluationState, scope: Scope, inputs: Iterable[str]
) -> tuple[Comparison, ...]:
    """The current comparison, as logged, of each of the scope's candidate ``inputs``.

    Each input appears at most once, in the order its comparison was first logged; an input
    with no comparison yet (its base is not measured) is left out.
    """
    wanted = frozenset(inputs)
    current: dict[tuple[str, str], Comparison] = {}
    for comparison in state.comparisons:
        if comparison.scope == scope and comparison.candidate_input in wanted:
            current[(comparison.candidate_input, comparison.base_input)] = comparison
    return tuple(current.values())


def close_call(comparison: Comparison, bar: float = 1.0) -> bool:
    """Whether the candidate's ratio is within ``CLOSE_CALL_NOISES`` of ``bar``."""
    return abs(comparison.ratio - bar) <= CLOSE_CALL_NOISES * comparison.noise


def base_in_flight(state: EvaluationState, measured: Measured) -> bool:
    """Whether a run of this measurement was admitted and has not ended (any scope's)."""
    for budget in state.submission_budgets:
        identity = budget.identity
        if (
            identity.measured_input != measured.measured_input
            or evaluation_of(identity) != measured.evaluation
        ):
            continue
        latest = budget.receipts[-1] if budget.receipts else None
        if isinstance(latest, PreparedSubmissionReceipt) and (
            latest.observation is None or not latest.observation.terminal
        ):
            return True
    return False


def base_missing(state: EvaluationState, measured: Measured, moment: Moment) -> bool:
    """Whether the base must be measured: no fresh sample of it is held or being taken."""
    held = fresh(samples_of(state, measured), moment)
    return not held and not base_in_flight(state, measured)


def remeasures_base(
    state: EvaluationState, comparison: Comparison, evaluation: str, moment: Moment
) -> bool:
    """Whether a close call is worth one more run of the base: it narrows the noise.

    Only while the base holds fewer than ``BASE_MAX_SAMPLES`` fresh samples and none is
    running, so a close call is never chased with base run after base run.
    """
    base = Measured(evaluation, comparison.base_input)
    held = fresh(samples_of(state, base), moment)
    return (
        close_call(comparison) and len(held) < BASE_MAX_SAMPLES and not base_in_flight(state, base)
    )


def official_needs_samples(
    state: EvaluationState, scope: Scope, measured: Measured, moment: Moment
) -> bool:
    """Whether the scope's samples of an input fall short of settling its comparison.

    The official measurement of an input the scope already measured adds a run only then: the
    samples held are a close call against the base, so one more narrows the noise. With no
    comparison (no base, or nothing to compare) the samples held are all there is to ask for.
    """
    held = comparison_of(state, scope, measured, moment)
    return held is not None and close_call(held)
