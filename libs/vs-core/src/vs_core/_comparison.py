"""Samples of one measured input, their comparison with a base, and when a base is measured.

A *measurement* is every conclusive run of one measured input under one evaluation. Its runs
are ``Sample``s of it: they are aggregated, never picked among, and none is refused because
there are several. This module owns the pure decisions over samples:

- ``evaluation_of`` names an evaluation (everything but the commit and purpose), so two runs of
  one input in one host and gates are samples of one measurement;
- ``compare`` compares a candidate's samples with its base's: a ``Comparison``;
- ``base_missing``, ``remeasures_base`` and ``remeasures_candidate`` decide when either side
  is measured again, and nothing else does.

The statistic
-------------

Let the candidate have ``m`` samples with mean ``c`` and the base ``n`` samples with mean ``b``.
The ratio is ``r = c / b``. A run's relative deviation is its distance from its own side's mean
over that mean. ``noise`` is one standard error of the ratio of two means (first order). More
samples shrink it, a wider spread or a higher floor grow it, and a shared host shows as spread.
A verdict needs the difference to exceed a critical number of noises: ``better`` when the
candidate improves on the base by more than that (a ratio above one for a ``max`` metric, below
one for a ``min`` metric), ``worse`` when it is worse by more than that, and ``within_noise``
otherwise.

- A task that declares a floor (``relative_noise``) has stated its noise. The runs' spread is
  the pooled relative deviation, never taken below the floor (which is also all there is to go
  on while each side holds one sample)::

      s = sqrt(sum(relative deviations ** 2) / (m + n - 2))        (0 when m + n <= 2)
      sigma = max(s, floor)
      noise = r * sigma * sqrt(1/m + 1/n)

  and the critical number is ``VERDICT_NOISES`` (2, about 95% two-sided for a known noise),
  from one run a side.
- A task that declares none (``relative_noise`` is 0) has not said its runs are exact: it has
  said nothing, and the noise is only what the runs show. Each side's spread is its own, since
  a candidate's code can change how much its runs vary, and the candidate side grows with every
  run of it while the base stops at three. This is Welch's t test: with ``v_c`` and ``v_b``
  each side's relative variance (squared relative deviations over ``m - 1`` and ``n - 1``)::

      noise = r * sqrt(v_c/m + v_b/n)
      df = (v_c/m + v_b/n) ** 2 / ((v_c/m) ** 2 / (m - 1) + (v_b/n) ** 2 / (n - 1))

  (the Welch-Satterthwaite degrees of freedom). The critical number is the two-sided 95%
  Student-t quantile at the floor of ``df`` (`_t_critical`), conservative because the quantile
  falls as the degrees grow, so a verdict at equal means stays near the intended 5% whatever
  the two spreads and counts. The comparison gives a verdict only once each side holds
  ``MIN_SAMPLES_WITHOUT_FLOOR`` runs and the noise is above zero. Fewer runs, or identical
  runs, are no estimate of a noise: identical readings of a quantized or cached benchmark show
  none, not that there is none. Until then the comparison is ``within_noise`` and a close call
  (`close_call`), which earns both sides more samples (below). Any fixed default floor would be a
  number nobody measured; requiring the key would refuse every task that has a benchmark and no
  ``[pareto]`` table.

When each side is measured
--------------------------

The base is measured once per (measured input, evaluation): the first time a candidate is
compared with it and no run of it is held or running, and again when every run of it a
candidate waited on ended without a sample (`vs_core._base_runs`). It is measured again only
when its samples are stale (older than ``TaskTerms.base_staleness`` on the run clock) or when a
candidate's result is a close call (``within_noise``, `close_call`), because one more base
sample narrows the noise. A close call never takes a base past `_base_max_samples` fresh
samples: ``BASE_MAX_SAMPLES`` (2, so at most one extra base run) under a declared floor, and
``MIN_SAMPLES_WITHOUT_FLOOR`` (3, so at most two extra base runs) without one, the fewest that
can give a verdict. Nor does it chase staleness: when base samples go stale faster than a base
run takes, the run a close call earned can land without growing the base's fresh samples (the
sample it was meant to join expired meanwhile), and that run is the last one a close call earns
either side of that comparison (`base_run_stalled`). The stop counts only the runs core took
for the comparison (its scope and candidate input), so a later candidate of the attempt,
compared once the base went stale, takes its own runs of the base. Every run of an identity in
a scope counts toward its submission limit (`vs_core._base_runs`), so an attempt's base is run
at most ``MAX_SUBMISSIONS_PER_MEASUREMENT`` times in its scope, however many candidate inputs
its agent measures, and core keeps at most one run of each side in flight per scope.

Core's runs serve the comparison only. The agent's turn never waits for them (they are not its
attempt's history, `vs_core._evaluation_history`); the comparison it reads next grows as they
land.

Without a declared floor the candidate needs those three runs too, and nobody else gives them:
an agent's call for content it already measured is answered with that evaluation, and the
official check adds at most one run. So a close call without a floor also earns the candidate's
input more runs in its scope (`remeasures_candidate`), up to ``MIN_SAMPLES_WITHOUT_FLOOR`` in
all. Under a floor one run a side can give a verdict, and core takes no run of the candidate.

A comparison without a floor whose ``MIN_SAMPLES_WITHOUT_FLOOR`` runs a side all read alike can
never give a verdict (`verdict_unreachable`): a deterministic benchmark reads the same again. It
earns neither side another run, and the official check is answered by the samples held instead
of adding one (`official_needs_samples`), so the runs a comparison costs are bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, inf, sqrt
from typing import TYPE_CHECKING

from ._values import digest
from .types.common import (
    MAX_SUBMISSIONS_PER_MEASUREMENT,
    EvidenceKind,
    ObservationStatus,
    RevisionRef,
    Value,
)
from .types.evaluation import (
    EvidenceRef,
    MeasurementIdentity,
    MeasurementStageIdentity,
    OwnedJob,
    PreparedSubmissionReceipt,
)
from .types.measurement import (
    BaseRef,
    Comparison,
    ComparisonVerdict,
    Direction,
    EffectVerdict,
    Reading,
    Sample,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .types.common import RequestId, RunFacts, Scope, Seconds, TaskTerms
    from .types.evaluation import EvaluationState
    from .types.kernel import RunState

__all__ = [
    "BASE_MAX_SAMPLES",
    "MAX_CORE_BASE_RUNS",
    "MIN_SAMPLES_WITHOUT_FLOOR",
    "VERDICT_NOISES",
    "ComparisonPolicy",
    "Measured",
    "Moment",
    "as_answer",
    "base_in_flight",
    "base_missing",
    "base_run_stalled",
    "claimed_base",
    "close_call",
    "compare",
    "comparison_of",
    "comparison_of_result",
    "comparison_settling",
    "comparisons_of",
    "comparisons_to_publish",
    "effect_verdict",
    "evaluation_of",
    "fresh",
    "latest_comparisons",
    "moment_of",
    "official_needs_samples",
    "policy_of",
    "remeasures_base",
    "remeasures_candidate",
    "run_reading",
    "running_baseline",
    "sample_of",
    "samples_of",
    "terms_policy",
    "verdict_unreachable",
]

VERDICT_NOISES = 2.0
"""How many standard errors a difference must exceed for a verdict under a declared floor."""
BASE_MAX_SAMPLES = 2
"""The most fresh samples a close call earns a base under a declared floor: one run, then one
more, never a chase.

A base run costs as much as a candidate's, so a close call narrows the noise with one more base
sample and stops there."""
MIN_SAMPLES_WITHOUT_FLOOR = 3
"""The runs each side needs before a comparison under no declared floor gives a verdict.

Three a side is the fewest that give each side's spread two degrees of freedom, so Welch's
degrees of freedom are at least 2 and the Student-t critical number at most 4.303; it is also
the most fresh samples a close call earns a base then, and the most runs of its input a close
call earns a candidate."""
MAX_CORE_BASE_RUNS = MIN_SAMPLES_WITHOUT_FLOOR * MAX_SUBMISSIONS_PER_MEASUREMENT
"""The most runs of an attempt's base core takes in its scope over the attempt's lifetime.

The base is one measurement however many candidate inputs the agent measures, and its samples
go stale (``TaskTerms.base_staleness``), so a long attempt measures it again for a later
candidate. This is enough for five comparisons each to take the base to the runs a verdict
needs without a floor, and it is the limit of the submission budget core opens for the base
(`vs_core._base_runs`), so it holds whoever asked for each run.
"""

_T_FIRST_DEGREES = MIN_SAMPLES_WITHOUT_FLOOR - 1
"""The fewest degrees of freedom a comparison that can give a verdict has (Welch's are never
below the smaller side's runs less one)."""
_T_975 = (
    4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip
"""The two-sided 95% Student-t quantile for ``_T_FIRST_DEGREES`` (2) to 30 degrees of freedom.

No stdlib or dependency of vs-core computes a Student-t quantile, so this is the standard table
to three decimals; `test_comparison_statistic` checks every entry against a numerically
integrated Student-t distribution."""
_DEGREES_SLACK = 1e-9
"""Added before taking the floor of Welch's degrees of freedom: degrees that are a whole number
in exact arithmetic (equal spreads and counts, or one side's runs all alike) can come out a few
units in the last place below it, and the floor would then drop a whole degree."""


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

    @property
    def floor_declared(self) -> bool:
        """Whether the task stated its noise; a floor of 0 states nothing (module docstring)."""
        return self.noise_floor > 0


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
    return terms_policy(facts.task)


def terms_policy(task: TaskTerms | None) -> ComparisonPolicy:
    """The comparison policy ``task`` fixes; the default for no task terms."""
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


def _measures(evidence: EvidenceRef) -> bool:
    """Whether ``evidence`` is a measurement: a trusted, successful benchmark record with a reading.

    Only such a record gives a sample (`sample_of`) and only such a record states an answer
    (`as_answer`): a failed or untrusted record keeps the reading its run reported.
    """
    return (
        evidence.kind == EvidenceKind.BENCHMARK
        and evidence.status == ObservationStatus.SUCCEEDED
        and evidence.provenance == "trusted"
        and evidence.reading is not None
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
        or not _measures(evidence)
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


def as_answer(state: EvaluationState, job: OwnedJob, evidence: EvidenceRef) -> EvidenceRef:
    """``evidence`` as the official measurement's answer: its reading is the samples' mean.

    The official measurement of an input is answered by every sample the scope holds of it, the
    run that answers included when it is a new one (`vs_core._measurement_reuse`). The
    benchmark record that carries the answer through the ledger, and so to every reader that
    ranks candidates, states the mean of those samples: exactly the candidate value the
    comparison with the base states (`comparison_of`) at the moment the answer is stored, never
    one run's reading. A later sample of the same input in the same scope (another run of it
    after the official check) moves the comparison but not the stored answer. The run's own
    reading stays in its ``Sample``.

    Only a measurement record states an answer (`_measures`): a failed or untrusted record
    keeps the reading its run reported, so a failed run never reads as the mean of runs that
    passed. Any other record, and a record whose samples read another metric, is returned as
    it is. Only the headline metric is aggregated: a record's other objectives are the
    answering run's own, since a sample holds the headline reading only.
    """
    plan = job.plan
    reading = evidence.reading
    if (
        plan.purpose != "official"
        or reading is None
        or not _measures(evidence)
        or plan.measured_input is None
        or not isinstance(plan.candidate, RevisionRef)
    ):
        return evidence
    measured = Measured(
        evaluation_of(MeasurementIdentity.from_plan(plan, plan.candidate)), plan.measured_input
    )
    held = tuple(s for s in samples_of(state, measured) if s.scope == job.scope)
    own = sample_of(job, evidence, 0.0)
    if own is not None and all(s.run != own.run for s in held):
        held = (*held, own)
    if not held or any(s.reading.metric != reading.metric for s in held):
        return evidence
    mean = _mean(tuple(s.reading.value for s in held))
    return evidence.model_copy(update={"reading": reading.model_copy(update={"value": mean})})


def run_reading(state: EvaluationState, job: OwnedJob, evidence: EvidenceRef) -> Reading | None:
    """The reading ``job``'s own run took for ``evidence``: its sample's, else the record's.

    An official run's ledger record states the measurement's answer (`as_answer`), so what the
    run itself read is its ``Sample``.
    """
    if evidence.reading is None:
        return None
    own = next((s for s in state.samples if s.run == job.submission_id), None)
    return evidence.reading if own is None else own.reading


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
    if policy.floor_declared:
        sigma = max(_pooled_spread(mine, c, theirs, b), policy.noise_floor)
        noise = abs(ratio) * sigma * sqrt(1 / m + 1 / n)
        critical = VERDICT_NOISES
    else:
        error, degrees = _welch(mine, c, theirs, b)
        noise = abs(ratio) * error
        critical = _t_critical(degrees)
    gain = ratio - 1 if direction == "max" else 1 - ratio
    verdict = (
        ComparisonVerdict.WITHIN_NOISE
        if not _noise_known(m, n, noise, policy)
        else ComparisonVerdict.BETTER
        if gain > critical * noise
        else ComparisonVerdict.WORSE
        if gain < -critical * noise
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


def _t_critical(degrees: float) -> float:
    """The two-sided 95% Student-t quantile at the floor of ``degrees`` of freedom.

    The floor, because the quantile falls as the degrees grow, so rounding down never gives a
    verdict that the exact degrees would not; beyond the table's 30 its last entry, slightly
    above the normal 1.96. Below ``_T_FIRST_DEGREES`` there is no verdict (`_noise_known`), so
    the bar is infinite.
    """
    whole = floor(degrees + _DEGREES_SLACK)
    if whole < _T_FIRST_DEGREES:
        return inf
    return _T_975[min(whole - _T_FIRST_DEGREES, len(_T_975) - 1)]


def _noise_known(m: int, n: int, noise: float, policy: ComparisonPolicy) -> bool:
    """Whether a comparison of ``m`` and ``n`` runs with this ``noise`` has an estimate of it.

    A declared floor is one. Without it, the runs must be ``MIN_SAMPLES_WITHOUT_FLOOR`` a side
    and show a spread above zero: zero spread from fewer runs, or from identical runs, is no
    measurement of the noise.
    """
    return policy.floor_declared or (min(m, n) >= MIN_SAMPLES_WITHOUT_FLOOR and noise > 0)


def _pooled_spread(mine: tuple[float, ...], c: float, theirs: tuple[float, ...], b: float) -> float:
    """The pooled relative deviation of the runs from their own side's mean (0 below 3 runs).

    Used only under a declared floor, which bounds it from below.
    """
    degrees = len(mine) + len(theirs) - 2
    if degrees <= 0 or c == 0 or b == 0:
        return 0.0
    return sqrt((_squares(mine, c) + _squares(theirs, b)) / degrees)


def _welch(
    mine: tuple[float, ...], c: float, theirs: tuple[float, ...], b: float
) -> tuple[float, float]:
    """The relative standard error of the ratio and its Welch-Satterthwaite degrees of freedom.

    Each side contributes its own relative variance over its count (module docstring); a side
    with one run, identical runs or a zero mean contributes none. Zero error has zero degrees.
    """
    parts = tuple(
        (_variance(values, mean) / len(values), len(values) - 1)
        for values, mean in ((mine, c), (theirs, b))
    )
    total = sum(part for part, _ in parts)
    if total == 0:
        return 0.0, 0.0
    return sqrt(total), total**2 / sum(part**2 / free for part, free in parts if part > 0)


def _variance(values: tuple[float, ...], mean: float) -> float:
    """One side's relative variance: squared relative deviations over its runs less one."""
    free = len(values) - 1
    if free == 0 or mean == 0:
        return 0.0
    return _squares(values, mean) / free


def _squares(values: tuple[float, ...], mean: float) -> float:
    """The sum of squared relative deviations from ``mean``: exactly 0 for identical runs.

    A float mean of identical values can differ from them in the last bit, which would show a
    spread of about 1e-17 where the runs showed none.
    """
    if min(values) == max(values):
        return 0.0
    return sum(((x - mean) / mean) ** 2 for x in values)


def comparison_of(
    state: EvaluationState, scope: Scope, measured: Measured, moment: Moment
) -> Comparison | None:
    """The scope's candidate input compared with its base, over every sample held at ``moment``.

    Every run of the input in the scope is a sample of it, whoever asked for the run (the
    strategy's own check adds one to the agent's); the base is the one an agent's run named.
    """
    mine, anchor = _anchored(state, scope, measured)
    if anchor is None:
        return None
    base = fresh(samples_of(state, Measured(measured.evaluation, anchor.measured_input)), moment)
    return compare(mine, base, scope=scope, anchor=anchor, policy=moment.policy)


def _anchored(
    state: EvaluationState, scope: Scope, measured: Measured
) -> tuple[tuple[Sample, ...], BaseRef | None]:
    """The scope's samples of the input and the base its latest anchored sample names."""
    mine = tuple(s for s in samples_of(state, measured) if s.scope == scope)
    return mine, next((s.base for s in reversed(mine) if s.base is not None), None)


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
    state: EvaluationState, scope: Scope, inputs: Iterable[str], moment: Moment
) -> tuple[Comparison, ...]:
    """The comparison each of the scope's candidate ``inputs`` has now, over the samples held.

    Each (input, base input) appears at most once, in the order its comparison was first
    logged; an input with no comparison logged yet (its base is not measured) is left out. The
    log is append-only, so its newest entry may rest on base samples that have since gone
    stale: what is given is the comparison the samples fresh at ``moment`` make
    (`comparison_of`). When only some base samples went stale it is recomputed over the rest,
    and when none is fresh the input is left out, so no reader is told a verdict whose base no
    longer counts.
    """
    wanted = frozenset(inputs)
    subjects = dict.fromkeys(
        (comparison.candidate_input, comparison.base_input)
        for comparison in state.comparisons
        if comparison.scope == scope and comparison.candidate_input in wanted
    )
    found: list[Comparison] = []
    for candidate_input, base_input in subjects:
        latest = next(
            (
                s
                for s in reversed(state.samples)
                if s.scope == scope
                and s.measured_input == candidate_input
                and s.base is not None
                and s.base.measured_input == base_input
            ),
            None,
        )
        if latest is None:
            continue
        now = comparison_of(state, scope, Measured(latest.evaluation, candidate_input), moment)
        if now is not None and now.base_input == base_input and now not in found:
            found.append(now)
    return tuple(found)


def comparison_settling(state: EvaluationState, comparison: Comparison) -> bool:
    """Whether a run that will feed ``comparison`` has not ended: it changes when that run lands.

    Any run of either side counts, whoever asked for it: a run of its base in any scope (core's,
    another candidate's, or the strategy's run core claimed, `_base_under_way`), since a base's
    samples are shared by every candidate compared with it, and a run of the candidate's input
    in the comparison's own scope (`base_in_flight`).
    """
    evaluation = next(
        (
            s.evaluation
            for s in reversed(state.samples)
            if s.scope == comparison.scope
            and s.measured_input == comparison.candidate_input
            and s.base is not None
            and s.base.measured_input == comparison.base_input
        ),
        None,
    )
    if evaluation is None:
        return False
    base = Measured(evaluation, comparison.base_input)
    candidate = Measured(evaluation, comparison.candidate_input)
    return _base_under_way(state, comparison, base) or base_in_flight(
        state, candidate, comparison.scope
    )


def comparisons_to_publish(
    state: EvaluationState,
    scope: Scope,
    covered: Iterable[str],
    told: Iterable[tuple[Comparison, bool]],
    moment: Moment,
) -> tuple[tuple[Comparison, ...], tuple[str, ...]]:
    """The comparisons a resume tells the scope's agent of, and the inputs of those still settling.

    ``covered`` are the candidate inputs the wait's jobs measured; ``told`` is every comparison
    the scope's earlier resumes told of, oldest first, each with whether it was still settling
    then. Of the comparisons the scope holds now (`latest_comparisons`), one is told when its
    input is covered, or when its verdict or whether it is settling differs from what the last
    resume that told of it said (one no resume told of differs). Core's runs land after the
    resume that waited for the candidate, so an agent that resumed on a provisional comparison
    learns where it ended at its next resume, whatever that resume waited for. A comparison is
    settling while a run of either side that will feed it has not ended (`comparison_settling`).
    """
    last: dict[tuple[str, str], tuple[ComparisonVerdict, bool]] = {}
    for comparison, settling in told:
        last[comparison.candidate_input, comparison.base_input] = (comparison.verdict, settling)
    wanted = frozenset(covered)
    held = (c.candidate_input for c in state.comparisons if c.scope == scope)
    found: list[Comparison] = []
    settling_now: list[str] = []
    for comparison in latest_comparisons(state, scope, held, moment):
        settling = comparison_settling(state, comparison)
        subject = (comparison.candidate_input, comparison.base_input)
        if comparison.candidate_input in wanted or last.get(subject) != (
            comparison.verdict,
            settling,
        ):
            found.append(comparison)
            if settling and comparison.candidate_input not in settling_now:
                settling_now.append(comparison.candidate_input)
    return tuple(found), tuple(settling_now)


def effect_verdict(
    comparison: Comparison, minimum_ratio: float, policy: ComparisonPolicy
) -> EffectVerdict:
    """What ``comparison`` says of an expected effect: the smallest ratio that justifies a change.

    ``minimum_ratio`` is the bar over the base in the metric's own direction (above one for a
    ``max`` metric, below one for ``min``; one asks for any improvement). The candidate's gain
    over the base is held against the gain the bar asks for, with the comparison's own noise
    and a critical number of noises::

        margin = gain - required gain
        supported     if margin >  critical * noise
        disproven     if margin < -critical * noise
        within_noise  otherwise

    ``policy`` is the one the comparison was made under (`compare`). Under a declared floor the
    critical number is ``VERDICT_NOISES``, and with a bar of one this is exactly the
    comparison's own verdict. Without one, a comparison with no estimate of its noise
    (`_noise_known`) says nothing of any bar, and otherwise the critical number is the Student-t
    quantile at the fewest degrees Welch's test can have, ``min(m, n) - 1`` (the stored
    comparison does not carry its exact degrees): at least the one the verdict used, so the
    effect is never called where the comparison would not be, and with a bar of one it is the
    comparison's verdict or ``within_noise``.
    """
    m, n = comparison.candidate_samples, comparison.base_samples
    if not _noise_known(m, n, comparison.noise, policy):
        return EffectVerdict.WITHIN_NOISE
    critical = VERDICT_NOISES if policy.floor_declared else _t_critical(min(m, n) - 1)
    sign = 1.0 if comparison.direction == "max" else -1.0
    margin = sign * (comparison.ratio - minimum_ratio)
    if margin > critical * comparison.noise:
        return EffectVerdict.SUPPORTED
    if margin < -critical * comparison.noise:
        return EffectVerdict.DISPROVEN
    return EffectVerdict.WITHIN_NOISE


def comparison_of_result(
    state: EvaluationState, moment: Moment, scope: Scope, candidates: Iterable[RevisionRef]
) -> Comparison | None:
    """The current comparison of the candidate a finished measurement measured, if it has one.

    The measurement's own run, a re-read of an earlier run, or any other run of the same commit
    in the scope is a sample of one input, so the input is the latest sample's of the scope at
    one of the ``candidates`` (the commits the result's evidence names); None when none was
    sampled or the input has no base's samples.
    """
    wanted = tuple(candidates)
    sample = next(
        (s for s in reversed(state.samples) if s.scope == scope and s.commit in wanted), None
    )
    if sample is None:
        return None
    return comparison_of(state, scope, Measured(sample.evaluation, sample.measured_input), moment)


def close_call(comparison: Comparison) -> bool:
    """Whether the candidate's result is within the critical number of noises of its base.

    That is exactly a ``within_noise`` verdict: the verdict was taken at the critical number for
    the comparison's own spreads (`compare`), which the stored comparison does not carry, so
    the verdict is the only place that bar is known. A comparison with no estimate of its noise
    (``_noise_known``) is ``within_noise`` whatever its gain: more samples are exactly what it
    lacks. A verdict, even from a zero noise under a declared floor (a candidate that read 0),
    is no close call.
    """
    return comparison.verdict is ComparisonVerdict.WITHIN_NOISE


def verdict_unreachable(comparison: Comparison, policy: ComparisonPolicy) -> bool:
    """Whether no further run can give ``comparison`` a verdict, so none is taken for it.

    Under no declared floor, once each side holds ``MIN_SAMPLES_WITHOUT_FLOOR`` runs that all
    read alike the noise is zero, and it stays zero: a deterministic (quantized or cached)
    benchmark reads the same value again, and no number of runs shows a noise it does not have.
    A declared floor is a noise estimate, so a comparison under one is never in this state.
    """
    return (
        not policy.floor_declared
        and min(comparison.candidate_samples, comparison.base_samples) >= MIN_SAMPLES_WITHOUT_FLOOR
        and comparison.noise == 0
    )


def _base_max_samples(policy: ComparisonPolicy) -> int:
    """The most fresh samples a close call earns a base (module docstring).

    ``BASE_MAX_SAMPLES`` under a declared floor; without one ``MIN_SAMPLES_WITHOUT_FLOOR``, the
    fewest a side needs for a verdict.
    """
    return BASE_MAX_SAMPLES if policy.floor_declared else MIN_SAMPLES_WITHOUT_FLOOR


def base_in_flight(state: EvaluationState, measured: Measured, scope: Scope | None = None) -> bool:
    """Whether a run of this measurement was admitted and has not ended (``scope``'s, or any)."""
    for budget in state.submission_budgets:
        identity = budget.identity
        if (
            (scope is not None and budget.scope != scope)
            or identity.measured_input != measured.measured_input
            or evaluation_of(identity) != measured.evaluation
        ):
            continue
        latest = budget.receipts[-1] if budget.receipts else None
        if isinstance(latest, PreparedSubmissionReceipt) and (
            latest.observation is None or not latest.observation.terminal
        ):
            return True
    return False


def running_baseline(state: EvaluationState, base: BaseRef) -> RequestId | None:
    """The strategy's own run of ``base`` that has not ended, if one is under way.

    The strategy measures the run's input from a plan that names no measured input, so
    `base_in_flight` (which matches on one) does not see it. It is the same revision under the
    same gates and host as an agent's base run would be, so a second run would measure the base
    twice: a wasted job on a contended cluster.
    """
    for budget in state.submission_budgets:
        identity = budget.identity
        latest = budget.receipts[-1] if budget.receipts else None
        if (
            identity.purpose == "baseline"
            and identity.measured_input is None
            and identity.candidate == base.commit
            and isinstance(latest, PreparedSubmissionReceipt)
            and (latest.observation is None or not latest.observation.terminal)
        ):
            return latest.request_id
    return None


def claimed_base(state: EvaluationState, request_id: RequestId) -> BaseRef | None:
    """The base a claimed run of the strategy's is counted as samples of, if it is claimed."""
    claim = next((c for c in state.base_claims if c.request_id == request_id), None)
    return None if claim is None else claim.base


def base_missing(state: EvaluationState, measured: Measured, moment: Moment) -> bool:
    """Whether the base must be measured: no fresh sample of it is held or being taken."""
    held = fresh(samples_of(state, measured), moment)
    return not held and not base_in_flight(state, measured)


def base_run_stalled(
    state: EvaluationState, comparison: Comparison, evaluation: str, moment: Moment
) -> bool:
    """Whether a close call's base run for this comparison landed without growing the base.

    Core records how many fresh samples of the base were held when it asked for a run
    (``CoreRun.fresh_when_asked``); a close call asks only while one is held. A run that landed
    with a sample, yet left no more fresh samples than that, replaced one that went stale while
    it ran, so the base goes stale faster than it runs and a verdict that needs more fresh base
    samples cannot be reached for this candidate: another run of either side would do the same.

    Only the runs core took for this comparison (its scope and candidate input) count. An
    earlier candidate's base runs landed while that candidate's samples were fresh; once they
    went stale they say nothing of how fast the base goes stale, and counting them would stop
    every later candidate of the attempt at one run a side.
    """
    base = Measured(evaluation, comparison.base_input)
    taken = samples_of(state, base)
    landed = {sample.run for sample in taken}
    held = len(fresh(taken, moment))
    return any(
        run.scope == comparison.scope
        and run.candidate_input == comparison.candidate_input
        and run.request_id in landed
        and run.fresh_when_asked >= max(held, 1)
        for run in state.core_runs
    )


def remeasures_base(
    state: EvaluationState, comparison: Comparison, evaluation: str, moment: Moment
) -> bool:
    """Whether a close call is worth one more run of the base: it narrows the noise.

    Only while the base holds fewer than `_base_max_samples` fresh samples, no run of it is under
    way (core's in any scope, or the strategy's own run core claimed), and core's last base run
    in the scope grew its fresh samples (`base_run_stalled`), so a close call is never chased
    with base run after base run.
    """
    base = Measured(evaluation, comparison.base_input)
    held = fresh(samples_of(state, base), moment)
    return (
        close_call(comparison)
        and not verdict_unreachable(comparison, moment.policy)
        and len(held) < _base_max_samples(moment.policy)
        and not _base_under_way(state, comparison, base)
        and not base_run_stalled(state, comparison, evaluation, moment)
    )


def _base_under_way(state: EvaluationState, comparison: Comparison, base: Measured) -> bool:
    """Whether a run of the base has not ended: core's own, or the strategy's run it claimed."""
    claimed = {claim.request_id for claim in state.base_claims}
    running = running_baseline(
        state, BaseRef(commit=comparison.base, measured_input=comparison.base_input)
    )
    return base_in_flight(state, base) or (running is not None and running in claimed)


def remeasures_candidate(
    state: EvaluationState, comparison: Comparison, evaluation: str, moment: Moment
) -> bool:
    """Whether a close call is worth one more run of the candidate's input, in its scope.

    Only under no declared floor, while the candidate holds fewer than
    ``MIN_SAMPLES_WITHOUT_FLOOR`` runs and none of its input is running in the scope: its side
    needs that many before any verdict (module docstring), and only core can take them, since a
    repeat of the same content is answered with the evaluation already made. Not once the base
    is known to go stale faster than it runs (`base_run_stalled`): its side cannot get there.
    """
    candidate = Measured(evaluation, comparison.candidate_input)
    return (
        not moment.policy.floor_declared
        and close_call(comparison)
        and comparison.candidate_samples < MIN_SAMPLES_WITHOUT_FLOOR
        and not base_in_flight(state, candidate, comparison.scope)
        and not base_run_stalled(state, comparison, evaluation, moment)
    )


def official_needs_samples(
    state: EvaluationState, scope: Scope, measured: Measured, moment: Moment
) -> bool:
    """Whether the scope's samples of an input fall short of settling its comparison.

    The official measurement of an input the scope already measured adds a run only then:

    - the samples held are a close call against the base, so one more narrows the noise,
      unless no run can give them a verdict (`verdict_unreachable`);
    - or the samples name a base but no sample of it is fresh at ``moment`` (its runs went
      stale, failed or are still running), so they have no verdict at all, and the answer must
      not rest on the runs the agent happened to make: the official check adds a run of its
      own.

    With no base named, or samples that cannot be compared (another metric, no direction), the
    samples held are all there is to ask for.
    """
    _, anchor = _anchored(state, scope, measured)
    if anchor is None:
        return False
    if not fresh(samples_of(state, Measured(measured.evaluation, anchor.measured_input)), moment):
        return True
    held = comparison_of(state, scope, measured, moment)
    return held is not None and close_call(held) and not verdict_unreachable(held, moment.policy)
