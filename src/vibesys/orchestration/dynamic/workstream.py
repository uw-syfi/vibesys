"""One workstream attempt: implement, review, evaluate, and record its round."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE
from vibesys.orchestration.dynamic.input_gate import benchmark_objectives
from vibesys.orchestration.dynamic.models import (
    EvaluationResult,
    ImplementerResult,
    ReviewResult,
    WorkstreamPhase,
)
from vibesys.orchestration.dynamic.prompts import render_implementation, render_review
from vs_loop_state.api import HypothesisOutcome
from vs_runtime.api import StructuredResponseError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from pydantic import BaseModel

    from vibesys.orchestration.dynamic.models import (
        DynamicOptions,
        DynamicState,
        DynamicWorkstream,
        EvidenceReference,
        WorkstreamPlan,
    )
    from vibesys.orchestration.dynamic.rounds import Rounds
    from vs_runtime.api import AgentSession, CandidateWorkspace, Run

_READY_OUTCOMES = frozenset({HypothesisOutcome.NOMINATED, HypothesisOutcome.SUPPORTED})
# Phases with a retained implementation; an attempt resumes after it.
_IMPLEMENTED_PHASES = frozenset(
    {WorkstreamPhase.IMPLEMENTED, WorkstreamPhase.REVIEWED, WorkstreamPhase.EVALUATED}
)
_TERMINAL_OUTCOMES = frozenset(
    {
        HypothesisOutcome.NOMINATED,
        HypothesisOutcome.SUPPORTED,
        HypothesisOutcome.DISPROVEN,
        HypothesisOutcome.IMPLEMENTATION_FAILED,
        HypothesisOutcome.INCONCLUSIVE,
        HypothesisOutcome.BLOCKED,
    }
)


class IncompleteCheckpointError(RuntimeError):
    """A post-implementation phase was persisted without its retained result."""

    @classmethod
    def at(cls, hypothesis_id: str, phase: WorkstreamPhase) -> IncompleteCheckpointError:
        """Describe the workstream whose checkpoint lacks its implementation."""
        return cls(
            f"dynamic workstream {hypothesis_id!r} has phase {phase.value!r} "
            "without a retained implementation"
        )


@dataclass(frozen=True, slots=True)
class _RecreatedWorktree:
    """A worktree created fresh for a hypothesis whose agent session may resume."""

    revision: str
    # The revision the hypothesis's previous attempt ended at, if known.
    remembered: str | None

    @classmethod
    def after(
        cls, item: DynamicWorkstream, workspace: CandidateWorkspace
    ) -> _RecreatedWorktree | None:
        """Return the reset facts when an earlier implementer turn of ``item`` exists."""
        resumed = item.budget.started or bool(item.prior_attempt)
        if not resumed or workspace.revision is None:
            return None
        return cls(
            revision=workspace.revision,
            remembered=item.candidate_revision or item.prior_revision,
        )


class DynamicAttemptError(RuntimeError):
    """One isolated workstream attempt failed after its failure was persisted."""

    def __init__(self, message: str, *, repeated: bool = False) -> None:
        """Record whether this failure repeats the previous attempt's, before any agent turn."""
        super().__init__(message)
        self.repeated = repeated

    @classmethod
    def from_cause(
        cls, hypothesis_id: str, cause: BaseException, *, repeated: bool = False
    ) -> DynamicAttemptError:
        """Describe the isolated slot and retain the original failure as the cause."""
        return cls(f"{hypothesis_id}: {cause}", repeated=repeated)


@dataclass(slots=True)
class Workstreams:
    """Runs workstream attempts against one run's durable state.

    Each phase transition is committed before the next stage starts, so a
    resumed attempt continues at its first incomplete stage. A finished
    workstream is recorded as a round through ``rounds``.
    """

    run: Run
    options: DynamicOptions
    state: DynamicState
    rounds: Rounds
    lock: asyncio.Lock
    commit: Callable[[str], Awaitable[None]]
    # Agent turns started per hypothesis in this process; an attempt that
    # started none failed in setup, before any agent could act.
    _agent_turns: dict[str, int] = field(default_factory=dict)

    async def execute(self, plan: WorkstreamPlan) -> None:
        """Run one attempt of a workstream; every failure is a retryable attempt failure.

        Workspace creation and the pre-attempt transitions are inside the
        attempt boundary, so a transient worktree error spends a retry of this
        slot instead of ending the run.
        """
        index = workstream_index(self.state, plan.hypothesis_id)
        workspace: CandidateWorkspace | None = None
        turns_at_start = self._agent_turns.get(plan.hypothesis_id, 0)
        # Attempts spent when the work began; unchanged at a failure means no
        # implementer turn charged this attempt.
        spent_at_start: int | None = None
        try:
            workspace = await self._open_attempt(index)
            spent_at_start = self.state.workstreams[index].budget.spent
            if workspace is not None:
                await self._run_attempt(index, plan, workspace)
        except asyncio.CancelledError:
            # Keep the durable phase: resume continues from the last checkpoint
            # and redoes an interrupted implementation.
            raise
        except Exception as error:
            before_turn = self._agent_turns.get(plan.hypothesis_id, 0) == turns_at_start
            repeated = await self._record_failure(
                index, str(error), spent_at_start=spent_at_start, before_turn=before_turn
            )
            raise DynamicAttemptError.from_cause(
                plan.hypothesis_id, error, repeated=repeated
            ) from error
        finally:
            if workspace is not None:
                await self._discard(plan.hypothesis_id, workspace)

    async def _record_failure(
        self, index: int, error: str, *, spent_at_start: int | None, before_turn: bool
    ) -> bool:
        """Persist a failed attempt; return whether it repeats a setup failure.

        A failed turn ends ``failed``, and an attempt that no implementer turn
        charged is charged here. A setup failure (no agent turn started) that
        repeats the previous attempt's setup failure is deterministic: retrying
        it spends the slot's budget on the same error, so the slot gives up at
        once. The error text is kept for the planner, which otherwise sees only
        ``failed``.
        """
        async with self.lock:
            current = self.state.workstreams[index]
            repeated = before_turn and current.setup_failure and current.last_error == error
            changes: dict[str, object] = {"last_error": error, "setup_failure": before_turn}
            if current.phase is WorkstreamPhase.IMPLEMENTING:
                changes["phase"] = WorkstreamPhase.FAILED
            if spent_at_start is None or current.budget.spent == spent_at_start:
                changes["budget"] = current.budget.charge()
            self.state.workstreams[index] = current.model_copy(update=changes, deep=True)
            await self.commit(f"dynamic: {current.hypothesis_id} attempt failed")
        return repeated

    async def _open_attempt(self, index: int) -> CandidateWorkspace | None:
        """Settle durable bookkeeping and open the attempt's workspace, if work remains."""
        item = self.state.workstreams[index]
        if item.phase is WorkstreamPhase.EVALUATED and (
            item.evaluation is None or item.evaluation.accepted
        ):
            await self.rounds.record(index)
            return None
        if item.phase is WorkstreamPhase.IMPLEMENTING:
            await self._refund_interrupted_attempt(index)
        # Keyed by hypothesis: every attempt and continuation of this
        # hypothesis works at one path, so its agent sessions resume. A retry
        # starts from this workstream's last retained candidate, as the next
        # in-process attempt would, so the review feedback applies to it.
        return await self.run.workspaces.create_candidate(
            item.candidate_revision or item.parent_revision,
            member_id=item.hypothesis_id,
        )

    async def _discard(self, hypothesis_id: str, workspace: CandidateWorkspace) -> None:
        """Release an attempt's workspace without replacing the attempt's result.

        The result is already durable when cleanup runs: raising here would
        mask the attempt's own error or make a recorded workstream retry. A
        leaked worktree resurfaces as a creation error inside the next attempt
        boundary of the same hypothesis.
        """
        try:
            await workspace.discard()
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-031017 [BLE001]; cleanup after a durable result must not replace that result.
            # > Narrowing to one type would let another cleanup failure (an
            # > ExceptionGroup from the runtime's teardown) end the run or retry
            # > a recorded workstream; the error is reported, not dropped.
            self.run.observations.note(
                f"dynamic workstream {hypothesis_id} workspace cleanup failed: {error}"
            )

    async def _run_attempt(
        self,
        index: int,
        plan: WorkstreamPlan,
        workspace: CandidateWorkspace,
    ) -> None:
        item = self.state.workstreams[index]
        call = item.planning_call
        parent = item.parent_revision
        resume_implemented = item.phase in _IMPLEMENTED_PHASES
        # A session of this hypothesis may already exist and resume here; it
        # remembers edits that the recreated worktree no longer has.
        reset = _RecreatedWorktree.after(item, workspace)
        feedback = item.feedback
        completed = False
        if resume_implemented:
            completed, feedback = await self._assess(index, plan, workspace)
            await self._remember_feedback(index, feedback)
        for _attempt in range(
            self.state.workstreams[index].budget.spent,
            self.options.max_retries_per_round,
        ):
            if completed:
                break
            await self._update(
                index,
                phase=WorkstreamPhase.IMPLEMENTING,
                charge=True,
            )
            implementation = await self._implement(
                plan,
                workspace,
                parent,
                feedback=feedback,
                reset=reset,
            )
            reset = None
            revision = await workspace.snapshot(
                f"dynamic: {plan.hypothesis_id} implementation planning call {call}"
            )
            implementation = _bind_evidence_revision(implementation, revision)
            await workspace.retain(
                revision,
                label=f"dynamic-{plan.hypothesis_id}-call-{call}",
            )
            await self._update(
                index,
                phase=WorkstreamPhase.IMPLEMENTED,
                candidate_revision=revision,
                implementation=implementation,
                clear_downstream=True,
            )
            completed, feedback = await self._assess(index, plan, workspace)
            await self._remember_feedback(index, feedback)
        if not completed:
            await self._update(index, phase=WorkstreamPhase.FAILED)
        await self.rounds.record(index)

    async def _remember_feedback(self, index: int, feedback: str | None) -> None:
        """Persist correction guidance so a retry after a failure still receives it."""
        async with self.lock:
            current = self.state.workstreams[index]
            if current.feedback == feedback:
                return
            self.state.workstreams[index] = current.model_copy(
                update={"feedback": feedback}, deep=True
            )
            await self.commit(f"dynamic: {current.hypothesis_id} feedback")

    async def _refund_interrupted_attempt(self, index: int) -> None:
        """Uncount an implementation attempt that a stop or crash interrupted.

        A failed attempt is marked ``failed``; ``implementing`` at entry means
        the attempt never finished, so it must not consume the retry budget.
        The budget bounds the refunds, so an attempt that crashes the process
        every time eventually counts as failed and cannot loop forever.
        """
        async with self.lock:
            current = self.state.workstreams[index]
            refunded = current.budget.refund_interrupted(self.options.max_retries_per_round)
            if refunded is None:
                update: dict[str, object] = {"phase": WorkstreamPhase.FAILED}
                label = f"dynamic: {current.hypothesis_id} interrupted attempt counted"
            else:
                update = {"phase": WorkstreamPhase.PENDING, "budget": refunded}
                label = f"dynamic: {current.hypothesis_id} resume interrupted"
            self.state.workstreams[index] = current.model_copy(update=update, deep=True)
            await self.commit(label)

    async def _assess(
        self,
        index: int,
        plan: WorkstreamPlan,
        workspace: CandidateWorkspace,
    ) -> tuple[bool, str | None]:
        """Run every assessment stage after the workstream's durable phase.

        A fresh implementation (``implemented``) is reviewed and then
        evaluated; a resumed one continues at its first incomplete stage, and an
        evaluated one only reports its verdict. Returns whether the candidate is
        complete and, if not, the correction guidance for the next attempt.
        """
        item = self.state.workstreams[index]
        implementation = item.implementation
        revision = item.candidate_revision
        if implementation is None or revision is None:
            raise IncompleteCheckpointError.at(item.hypothesis_id, item.phase)
        if item.phase is WorkstreamPhase.EVALUATED and item.evaluation is not None:
            if item.evaluation.accepted:
                return True, None
            return False, _evaluation_feedback(item.evaluation)
        review = item.review
        if item.phase is WorkstreamPhase.IMPLEMENTED:
            review = await self._maybe_review(
                plan, implementation, workspace, revision, item.sequence
            )
            if review is not None:
                await self._update(index, phase=WorkstreamPhase.REVIEWED, review=review)
        if review is not None and not review.passed:
            return False, review.feedback
        evaluation = await self._maybe_evaluate(
            plan,
            implementation,
            review,
            workspace,
            revision,
            item.planning_call,
        )
        final_phase = (
            WorkstreamPhase.EVALUATED
            if evaluation is not None
            else WorkstreamPhase.REVIEWED
            if review is not None
            else WorkstreamPhase.IMPLEMENTED
        )
        await self._update(index, phase=final_phase, evaluation=evaluation)
        if evaluation is not None and not evaluation.accepted:
            return False, _evaluation_feedback(evaluation)
        return True, None

    async def _implement(
        self,
        plan: WorkstreamPlan,
        workspace: CandidateWorkspace,
        parent_revision: str,
        *,
        feedback: str | None,
        reset: _RecreatedWorktree | None,
    ) -> ImplementerResult:
        session = await self.run.agents.create_session(
            IMPLEMENTER,
            workspace=workspace,
            member_id=plan.hypothesis_id,
        )
        self._agent_turns[plan.hypothesis_id] = self._agent_turns.get(plan.hypothesis_id, 0) + 1
        try:
            return await structured_turn(
                session,
                render_implementation(
                    hypothesis_id=plan.hypothesis_id,
                    **prompt_context(self.run),
                    hypothesis=plan.hypothesis,
                    task=plan.task,
                    pass_criteria=plan.pass_criteria,
                    parent_revision=parent_revision,
                    evidence=_references_text(plan.evidence),
                    feedback=feedback,
                    prior_attempt=self.state.workstreams[
                        workstream_index(self.state, plan.hypothesis_id)
                    ].prior_attempt,
                    worktree_revision=reset.revision if reset is not None else None,
                    prior_revision=reset.remembered if reset is not None else None,
                ),
                ImplementerResult,
            )
        finally:
            await session.close()

    async def _maybe_review(
        self,
        plan: WorkstreamPlan,
        implementation: ImplementerResult,
        workspace: CandidateWorkspace,
        revision: str,
        sequence: int,
    ) -> ReviewResult | None:
        # A blocked attempt or an unchanged tree leaves nothing to assess;
        # reviewing it spends a judge turn on an empty candidate.
        unchanged = (
            revision
            == self.state.workstreams[
                workstream_index(self.state, plan.hypothesis_id)
            ].parent_revision
        )
        if implementation.outcome is HypothesisOutcome.BLOCKED or unchanged:
            return None
        # A candidate that may be promoted is always reviewed. Another
        # terminal outcome is reviewed on every `judge_every`-th workstream,
        # counted by its sequence (its round number), as a sequential loop
        # counts rounds; planning calls are not batches under slot refill.
        promotable = implementation.outcome in _READY_OUTCOMES
        due = promotable or (
            implementation.outcome in _TERMINAL_OUTCOMES
            and sequence % self.options.judge_every == 0
        )
        if not due:
            return None
        session = await self.run.agents.create_session(
            JUDGE,
            workspace=workspace,
            member_id=plan.hypothesis_id,
        )
        self._agent_turns[plan.hypothesis_id] = self._agent_turns.get(plan.hypothesis_id, 0) + 1
        try:
            return await structured_turn(
                session,
                render_review(
                    hypothesis_id=plan.hypothesis_id,
                    **prompt_context(self.run),
                    hypothesis=plan.hypothesis,
                    pass_criteria=plan.pass_criteria,
                    candidate_revision=revision,
                    summary=implementation.summary,
                    evidence=_references_text(implementation.evidence),
                ),
                ReviewResult,
            )
        finally:
            await session.close()

    async def _maybe_evaluate(  # noqa: PLR0913  # lint-waiver: LW-930108 [PLR0913]; each argument is an independently established policy fact; a carrier would expose the same state less clearly.
        self,
        plan: WorkstreamPlan,
        implementation: ImplementerResult,
        review: ReviewResult | None,
        workspace: CandidateWorkspace,
        revision: str,
        planning_call: int,
    ) -> EvaluationResult | None:
        # Every review-passed ready candidate is evaluated; `official_eval_every`
        # does not apply. Workstreams are parallel branches, so an unevaluated
        # candidate could never be adopted or built on (unlike a sequential
        # loop, where the next round builds on a provisional checkpoint).
        candidate_ready = implementation.outcome in _READY_OUTCOMES
        if not candidate_ready or review is None or not review.passed:
            return None
        available = self.run.facts.accuracy_configured or self.run.facts.benchmark_configured
        has_local_recipe = implementation.validation_recipe_artifact is not None
        if not available and not has_local_recipe:
            return None
        local = (
            await self.run.evaluation.validate_local(
                workspace,
                recipe_artifact=implementation.validation_recipe_artifact,
                report_location=(
                    f"progress/validation/dynamic-{plan.hypothesis_id}-call-{planning_call}.json"
                ),
            )
            if implementation.validation_recipe_artifact is not None
            else None
        )
        if local is not None and not local.passed:
            return EvaluationResult(
                revision=revision,
                local_validation_passed=False,
                local_validation_feedback=local.feedback,
            )
        async with asyncio.TaskGroup() as evaluations:
            accuracy_task = (
                evaluations.create_task(self.run.evaluation.accuracy(workspace))
                if self.run.facts.accuracy_configured
                else None
            )
            benchmark_task = (
                evaluations.create_task(
                    self.run.evaluation.benchmark(
                        workspace, objectives=benchmark_objectives(self.options)
                    ),
                )
                if self.run.facts.benchmark_configured
                else None
            )
        accuracy = accuracy_task.result() if accuracy_task is not None else None
        benchmark = benchmark_task.result() if benchmark_task is not None else None
        return EvaluationResult(
            revision=revision,
            local_validation_passed=local.passed if local is not None else None,
            local_validation_feedback=local.feedback if local is not None else None,
            accuracy_passed=accuracy.passed if accuracy is not None else None,
            accuracy_feedback=accuracy.feedback if accuracy is not None else None,
            benchmark_passed=benchmark.passed if benchmark is not None else None,
            benchmark_feedback=benchmark.feedback if benchmark is not None else None,
            metric_name=benchmark.metric_name if benchmark is not None else None,
            metric_value=benchmark.metric_value if benchmark is not None else None,
            metric_direction=benchmark.metric_direction if benchmark is not None else None,
            metric_unit=benchmark.metric_unit if benchmark is not None else None,
            metrics=dict(benchmark.row or {}) if benchmark is not None else {},
        )

    async def _update(  # noqa: PLR0913  # lint-waiver: LW-092703 [PLR0913]; optional fields are explicit transition outputs and avoid an untyped mutation mapping at the durability boundary.
        self,
        index: int,
        *,
        phase: WorkstreamPhase,
        charge: bool = False,
        candidate_revision: str | None = None,
        implementation: ImplementerResult | None = None,
        review: ReviewResult | None = None,
        evaluation: EvaluationResult | None = None,
        clear_downstream: bool = False,
    ) -> None:
        async with self.lock:
            current = self.state.workstreams[index]
            changes: dict[str, object] = {"phase": phase}
            if charge:
                changes["budget"] = current.budget.charge()
            if candidate_revision is not None:
                changes["candidate_revision"] = candidate_revision
            if implementation is not None:
                changes["implementation"] = implementation
            if clear_downstream:
                changes["review"] = None
                changes["evaluation"] = None
            if review is not None:
                changes["review"] = review
            if evaluation is not None:
                changes["evaluation"] = evaluation
            self.state.workstreams[index] = current.model_copy(update=changes, deep=True)
            await self.commit(f"dynamic: {current.hypothesis_id} {phase.value}")


def prompt_context(run: Run) -> dict[str, str]:
    """Return the run facts every role prompt states inline.

    The objective is inlined rather than cited by path: the effective
    objective lives in run state that agent sandboxes hide. The environment
    notes carry facts such as read-only inputs and where trusted evaluation
    runs, without which agents plan edits that fail.
    """
    return {
        "objective": run.facts.objective,
        "environment_notes": run.facts.environment_notes,
    }


def workstream_index(state: DynamicState, hypothesis_id: str) -> int:
    """Return the position of ``hypothesis_id`` in the durable workstream list."""
    return next(
        index for index, item in enumerate(state.workstreams) if item.hypothesis_id == hypothesis_id
    )


async def structured_turn[ResponseT: BaseModel](
    session: AgentSession,
    message: str,
    response: type[ResponseT],
) -> ResponseT:
    """Run one turn, asking the same conversation once to re-emit an unparseable reply.

    The follow-up keeps the agent's completed work and workspace edits; failing
    the turn instead would discard an implementation attempt or the whole run.
    """
    try:
        return await session.turn(message, response=response)
    except StructuredResponseError as error:
        correction = (
            f"Correction required: {error}. Do not redo the work. Return only the "
            f"schema-valid {response.__name__} JSON for the work already completed."
        )
        return await session.turn(correction, response=response)


def _bind_evidence_revision(result: ImplementerResult, revision: str) -> ImplementerResult:
    evidence = tuple(
        reference
        if reference.revision is not None
        else reference.model_copy(update={"revision": revision})
        for reference in result.evidence
    )
    return result.model_copy(update={"evidence": evidence})


def _references_text(references: Sequence[EvidenceReference]) -> str:
    if not references:
        return "[]"
    return json.dumps(
        [item.model_dump(mode="json") for item in references],
        separators=(",", ":"),
    )


def _evaluation_feedback(result: EvaluationResult) -> str:
    """Render trusted gate failures as compact correction guidance."""
    feedback = [
        message
        for passed, message in (
            (result.local_validation_passed, result.local_validation_feedback),
            (result.accuracy_passed, result.accuracy_feedback),
            (result.benchmark_passed, result.benchmark_feedback),
        )
        if passed is False and message
    ]
    return "Trusted evaluation failed: " + "; ".join(feedback or ["no feedback provided"])


__all__ = [
    "DynamicAttemptError",
    "IncompleteCheckpointError",
    "Workstreams",
    "prompt_context",
    "structured_turn",
    "workstream_index",
]
