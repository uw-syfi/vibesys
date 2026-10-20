"""Measure a comparison baseline with the executor that measures candidates.

``ComparisonMeasurer`` is the shell around the pure parts of ``_comparison``: it looks the
subject up in the cache, and otherwise runs the task's accuracy and benchmark stages once per
sample on an evaluation executor, reads the benchmark's headline metric from the trusted evidence,
and keeps the reading; it runs only when asked to (``vibesys compare``), never from a run. The executor is the same kind a candidate's measurement goes through (a
Slurm job today); what differs is only the service it runs the benchmark against, which the
caller's ``SampleExecutors`` builds.

Failure is a value. An engine that cannot be served, a stage that fails, a job that outlasts its
budget, or an executor that refuses each end in a ``ComparisonFailed`` carrying the reason, and a
machine that cannot run the comparison ends in ``ComparisonNotConfigured``. Nothing is raised for
them, so a comparison never blocks the search that asked for it. A stop (cancellation) cancels the
job and propagates.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    ExecutorObservation,
    ExecutorRejectedError,
    ExecutorSubmissionError,
    PollingEvaluationExecutor,
    PollPhase,
    SemanticEvaluationStage,
    TrustedEvidence,
)
from vs_runtime._comparison import (
    ComparisonFailed,
    ComparisonMeasured,
    ComparisonNotConfigured,
    ComparisonReading,
    ComparisonSample,
    ComparisonStore,
    ComparisonSubject,
    bounded_reason,
)

if TYPE_CHECKING:
    from vs_sim.api import Clock, Sleeper

_PURPOSE = "baseline"
_STAGES = (("accuracy", EvidenceKind.ACCURACY), ("benchmark", EvidenceKind.BENCHMARK))
_BENCHMARK = "benchmark"


class ComparisonExecutor(PollingEvaluationExecutor, Protocol):
    """A pollable evaluation executor for one sample, released when the sample is read."""

    async def close(self) -> None:
        """Release the executor; its job is cancelled unless it already ended."""
        ...


class ComparisonUnavailableError(Exception):
    """The machine cannot run this comparison; the message says what it lacks."""


class SampleExecutors(Protocol):
    """Builds the executor of one sample: the comparison's server, avoiding some nodes."""

    def executor_for(
        self, subject: ComparisonSubject, *, avoiding: tuple[str, ...]
    ) -> ComparisonExecutor:
        """An executor that serves *subject*'s engine, excluding the nodes in *avoiding*.

        Raises ``ComparisonUnavailableError`` when this machine cannot serve the engine.
        """
        ...


@dataclass(frozen=True, slots=True)
class ComparisonPlan:
    """How one comparison is measured."""

    revision: str
    """The commit of the trusted checkout that supplies the task's files."""
    samples: int
    """How many runs to take; each is a job of its own."""
    budget_seconds: float
    """The longest one sample may take, queueing included."""
    headline_metric: str | None = None
    """The metric the task reports as its headline; the benchmark's first metric when unset."""
    spread_across_nodes: bool = True
    """Whether each sample avoids the nodes the earlier ones ran on."""
    remeasure: bool = False
    """Measure again even when the cache holds a reading for the subject's key (``compare --force``)."""


@dataclass(frozen=True, slots=True)
class _Taken:
    """One sample, with how its benchmark names the metric it read."""

    sample: ComparisonSample
    metric: str
    unit: str | None
    direction: Literal["max", "min"] | None


@dataclass(frozen=True, slots=True)
class ComparisonOutcome:
    """What measuring one comparison produced."""

    reading: ComparisonReading
    cached: bool
    """Whether *reading* came from the cache, without running anything."""


class ComparisonMeasurer:
    """Measures comparisons through evaluation executors, caching what it measures."""

    def __init__(
        self,
        store: ComparisonStore,
        executors: SampleExecutors,
        *,
        clock: Clock,
        sleeper: Sleeper,
        poll_seconds: float = 15.0,
    ) -> None:
        """Keep readings in *store*; *clock* and *sleeper* pace the polls of a running job."""
        self._store = store
        self._executors = executors
        self._clock = clock
        self._sleeper = sleeper
        self._poll_seconds = poll_seconds

    async def measure(self, subject: ComparisonSubject, plan: ComparisonPlan) -> ComparisonOutcome:
        """The reading of *subject*: cached when its key holds one, else measured now."""
        stored = self._store.get(subject)
        if stored is not None and not plan.remeasure:
            return ComparisonOutcome(stored.reading, cached=True)
        attempt = self._store.begin_attempt(subject)
        # A stop or an unexpected error leaves the attempt open, so the next one resumes its jobs.
        outcome = await self._attempt(subject, plan, attempt)
        self._store.conclude_attempt(subject, attempt)
        return outcome

    async def _attempt(
        self, subject: ComparisonSubject, plan: ComparisonPlan, attempt: int
    ) -> ComparisonOutcome:
        taken: list[_Taken] = []
        for index in range(plan.samples):
            avoiding = (
                tuple(dict.fromkeys(t.sample.host for t in taken if t.sample.host is not None))
                if plan.spread_across_nodes
                else ()
            )
            result = await self._sample(subject, plan, attempt, index, avoiding)
            if not isinstance(result, _Taken):
                return ComparisonOutcome(result, cached=False)
            taken.append(result)
        if not taken:
            return ComparisonOutcome(
                ComparisonFailed(reason="no sample was requested"), cached=False
            )
        reading = ComparisonMeasured(
            metric=taken[0].metric,
            unit=taken[0].unit,
            direction=taken[0].direction,
            samples=tuple(t.sample for t in taken),
        )
        self._store.put(subject, reading, measured_at=self._clock.now())
        return ComparisonOutcome(reading, cached=False)

    async def _sample(
        self,
        subject: ComparisonSubject,
        plan: ComparisonPlan,
        attempt: int,
        index: int,
        avoiding: tuple[str, ...],
    ) -> _Taken | ComparisonFailed | ComparisonNotConfigured:
        try:
            held = self._executors.executor_for(subject, avoiding=avoiding)
        except ComparisonUnavailableError as unavailable:
            return ComparisonNotConfigured(reason=bounded_reason(str(unavailable)))
        handle = f"cmp-{subject.key[:24]}-a{attempt}-s{index}"
        try:
            terminal = await self._run(held, subject, plan, handle)
        except ExecutorRejectedError as rejected:
            return ComparisonFailed(reason=bounded_reason(f"the executor refused it: {rejected}"))
        except ExecutorSubmissionError as unsure:
            return ComparisonFailed(
                reason=bounded_reason(f"the job could not be submitted: {unsure}")
            )
        finally:
            await asyncio.shield(held.close())
        if isinstance(terminal, ComparisonFailed):
            return terminal
        return _read_sample(terminal, plan.headline_metric, index, plan.samples)

    async def _run(
        self,
        held: ComparisonExecutor,
        subject: ComparisonSubject,
        plan: ComparisonPlan,
        handle: str,
    ) -> ExecutorObservation | ComparisonFailed:
        executor = held
        deadline = self._clock.now() + plan.budget_seconds
        await executor.submit(
            _request(subject, plan, handle, deadline_at=deadline), handle_id=handle
        )
        try:
            while True:
                polled = await executor.poll(handle)
                if polled.phase is PollPhase.ENDED and polled.terminal is not None:
                    return polled.terminal
                if self._clock.now() >= deadline:
                    await executor.cancel(handle)
                    return ComparisonFailed(
                        reason=bounded_reason(
                            f"it did not finish within {plan.budget_seconds:g} seconds"
                        )
                    )
                await self._sleeper.sleep(self._poll_seconds)
        except asyncio.CancelledError:
            await asyncio.shield(executor.cancel(handle))
            raise


def _request(
    subject: ComparisonSubject, plan: ComparisonPlan, handle: str, *, deadline_at: float
) -> EvaluationRequest:
    """The ordered accuracy and benchmark stages of one sample, at the trusted checkout."""
    fingerprints = EvidenceFingerprints(
        candidate=ContentDigest.sha256(f"git-commit:{plan.revision}".encode()),
        evaluator=ContentDigest.sha256(subject.evaluator.encode()),
        workload=ContentDigest.sha256(f"{subject.key}:workload".encode()),
        environment=ContentDigest.sha256(subject.hardware.encode()),
    )
    steps = tuple(
        EvaluationStep(
            name=name,
            payload=SemanticEvaluationStage(
                snapshot=plan.revision,
                kind=kind,
                fingerprints=fingerprints,
                deadline_at_s=deadline_at,
                purpose=_PURPOSE,
            ).model_dump(mode="json"),
        )
        for name, kind in _STAGES
    )
    return EvaluationRequest(
        key=handle,
        owner_scope=f"comparison:{subject.spec.name}",
        stages=steps,
        stop_on_failure=True,
    )


def _read_sample(
    terminal: ExecutorObservation, headline: str | None, index: int, total: int
) -> _Taken | ComparisonFailed:
    """The sample a finished job measured, or why it measured none."""
    where = f"sample {index + 1} of {total}: "
    if terminal.state is EvaluationState.CANCELED:
        return ComparisonFailed(reason=bounded_reason(where + "the job was cancelled"))
    for step in terminal.stage_results:
        evidence = None if step.result is None else TrustedEvidence.model_validate(step.result)
        cause = step.operator_failure or step.failure
        if evidence is not None and evidence.outcome is EvidenceOutcome.FAILED:
            cause = evidence.semantic_summary or "it failed"
        if cause:
            return ComparisonFailed(reason=bounded_reason(f"{where}{step.name} failed: {cause}"))
    if terminal.state is not EvaluationState.SUCCEEDED:
        cause = terminal.operator_failure or terminal.failure or "the job failed"
        return ComparisonFailed(reason=bounded_reason(where + cause))
    by_name = {step.name: step for step in terminal.stage_results}
    step = by_name.get(_BENCHMARK)
    if step is None or step.result is None:
        return ComparisonFailed(reason=bounded_reason(where + "the benchmark reported nothing"))
    evidence = TrustedEvidence.model_validate(step.result)
    metric = next((m for m in evidence.metrics if headline is None or m.name == headline), None)
    if metric is None:
        named = f"metric {headline!r}" if headline is not None else "a metric"
        return ComparisonFailed(reason=bounded_reason(where + f"the benchmark reported no {named}"))
    host = next((s.host for s in terminal.stage_results if s.host is not None), None)
    return _Taken(
        ComparisonSample(value=metric.value, host=host), metric.name, metric.unit, metric.direction
    )


__all__ = [
    "ComparisonExecutor",
    "ComparisonMeasurer",
    "ComparisonOutcome",
    "ComparisonPlan",
    "ComparisonUnavailableError",
    "SampleExecutors",
]
