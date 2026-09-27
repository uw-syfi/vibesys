"""Evolutionary-search policy over explicit runtime capabilities."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.constants import DomainName
from vibesys.orchestration.domains.base import DomainRole
from vibesys.orchestration.domains.registry import resolve_domain
from vibesys.orchestration.domains.rendering import render_domain_section
from vibesys.orchestration.evolve.agents import JUDGE, MUTATOR, PROFILER
from vibesys.orchestration.evolve.models import (
    CandidateJudgeContext,
    CandidateProfilerContext,
    EvolveOptions,
    EvolveState,
    JudgeResponse,
    MutatorContext,
    MutatorResponse,
)
from vibesys.orchestration.evolve.population import (
    CandidateOutcome,
    Individual,
    PopulationConfig,
    PopulationSearch,
    PopulationState,
    Proposal,
)
from vibesys.orchestration.evolve.prompts import render_judge, render_mutator, render_profiler
from vibesys.orchestration.profilers import ProfilerKind, ProfilerSummary, profiler_definition
from vibesys.orchestration.review import Verdict
from vs_runtime.api import (
    BenchmarkEvaluation,
    BenchmarkObjective,
    MetricDirection,
    Run,
    RunStatus,
    StructuredResponseError,
    Workspace,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from pydantic import BaseModel

    from vs_runtime.api import AgentSession

_CANDIDATE_REQUIREMENTS = (
    "The candidate obeys the input bundle's contract, the accuracy "
    "command passes, and the benchmark sanity step completes without "
    "modifying evaluator-owned files."
)
_CLEANUP_ERROR = "evolve agent cleanup failed"
_CANDIDATE_CLEANUP_ERROR = "candidate cleanup failed"
_INTERFACE = "inprocess"
_PARETO_PROFILER_ADDENDUM = """\

## Pareto-frontier mode — emit *all* configured metrics

This run is in Pareto-frontier mode. In addition to the headline `perf_metric` / `perf_unit`, populate the `metrics` field of `ProfilerSummary` with the numeric value of EVERY objective listed below — read each one from the benchmark tool's JSON output, do not derive, do not invert.

Objectives to report (use these exact key names in `metrics`):

{objective_list}

If the benchmark JSON does not contain a field, set its entry to `null` rather than substituting a derived number — the framework will treat the offspring as missing on that axis and exclude it from the frontier (which is correct: an unmeasured axis cannot be compared).
"""


async def _capture_cleanup(operation: Awaitable[None]) -> BaseException | None:
    """Complete one cleanup operation without preventing later cleanup."""
    try:
        await operation
    except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-920436 [BLE001]; cleanup must include cancellation and continue through every owned session/workspace, so catching narrower exception families would leak later resources.
        return error
    return None


@dataclass(frozen=True, slots=True)
class _Sessions:
    mutator: AgentSession
    judge: AgentSession
    profiler: AgentSession | None

    async def close(self) -> None:
        """Close every conversation while preserving all cleanup attempts."""
        errors: list[BaseException] = []
        for session in (self.profiler, self.judge, self.mutator):
            if session is None:
                continue
            while error := await _capture_cleanup(session.close()):
                if isinstance(error, asyncio.CancelledError):
                    continue
                errors.append(error)
                break
        if errors:
            raise BaseExceptionGroup(_CLEANUP_ERROR, errors)


@dataclass(frozen=True, slots=True)
class _CandidateTask:
    generation: int
    child: int
    parent: Individual | None
    inspirations: tuple[Individual, ...]
    cold_start: bool
    repair_seed: bool = False
    policy_parent_id: str | None = None
    target_island: int | None = None


async def _open_sessions(run: Run, workspace: Workspace) -> _Sessions:
    mutator = await run.agents.create_session(MUTATOR, workspace=workspace)
    judge = await run.agents.create_session(JUDGE, workspace=workspace)
    profiler = (
        await run.agents.create_session(PROFILER, workspace=workspace)
        if ProfilerKind(run.facts.profiler_id) is not ProfilerKind.NONE
        else None
    )
    return _Sessions(mutator=mutator, judge=judge, profiler=profiler)


def _fallback_mutator() -> MutatorResponse:
    return MutatorResponse(
        summary="Mutator produced no structured response.",
        hypothesis="unknown",
        expected_behavior="unknown",
    )


def _fallback_judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="Judge produced no structured response.",
        feedback="No structured response received.",
        verdict=Verdict.FAIL,
    )


def _fallback_profiler() -> ProfilerSummary:
    return ProfilerSummary(
        analysis="Profiler produced no structured response.",
        bottlenecks="n/a",
        suggestions="n/a",
        perf_metric=None,
        perf_unit=None,
    )


class _EvolveRun:
    """One crash-recoverable evolutionary campaign."""

    def __init__(self, run: Run, options: EvolveOptions) -> None:
        self.run = run
        self.options = options
        self.root = run.workspaces.root
        self.objectives = tuple(
            BenchmarkObjective(
                name=item.name,
                direction=(
                    MetricDirection.MAXIMIZE
                    if item.direction == "max"
                    else MetricDirection.MINIMIZE
                ),
            )
            for item in options.metric_space.objectives
        )
        self.search: PopulationSearch
        self.state: EvolveState
        self.root_sessions: _Sessions
        self.domain = resolve_domain(DomainName(run.facts.domain_id))

    async def initialize(self) -> None:
        loaded = await self.run.state.load(EvolveState)
        selector_config = self.options.openevolve_config()
        selector = self.options.search_policy
        if selector is None:
            selector = (
                "openevolve"
                if selector_config is not None
                or (loaded is not None and loaded.population.selector_state is not None)
                else "vibesys"
            )
        self.search = PopulationSearch(
            PopulationConfig(
                space=self.options.metric_space,
                selection_temperature=self.options.selection_temperature,
                frontier_bias=self.options.frontier_bias,
                k_top_inspirations=self.options.k_top_inspirations,
                k_random_inspirations=self.options.k_random_inspirations,
                selector=selector,
                openevolve=selector_config,
                seed=self.options.seed,
            )
        )
        if loaded is not None and loaded.metric_space != self.options.metric_space:
            message = "recorded evolve metric space differs from the plugin options"
            raise ValueError(message)
        self.state = loaded or EvolveState(
            population=self.search.initial(), metric_space=self.options.metric_space
        )
        self.root_sessions = await _open_sessions(self.run, self.root)
        await self._commit("evolve: initialize search state")

    async def _commit(self, label: str) -> None:
        await self.run.state.commit(self.state, workspace=self.root, label=label)

    async def execute(self) -> RunStatus:
        await self.initialize()
        try:
            if self.search.needs_bootstrap(self.state.population):
                await self.run.control.checkpoint()
                if not await self._bootstrap():
                    return RunStatus.FAILED
            start = (
                self.state.generation_start.generation
                if self.state.generation_start is not None
                else self.state.population.generation + 1
            )
            for generation in range(start, self.options.max_generations + 1):
                await self.run.control.checkpoint()
                await self._run_generation(generation)
            best = self.search.best(self.state.population)
            if best is not None and best.commit is not None:
                await self.run.workspaces.adopt(best.commit)
                await self.root.snapshot(f"evolve: select individual {best.id}")
            return RunStatus.SUCCEEDED
        finally:
            primary = sys.exception()
            if error := await _capture_cleanup(self.root_sessions.close()):
                if primary is None:
                    raise error
                primary.add_note(f"root session cleanup also failed: {error}")

    async def _bootstrap(self) -> bool:
        for attempt in range(1, self.options.bootstrap_max_attempts + 1):
            wip = self.search.wip_seed(self.state.population)
            if (
                wip is not None
                and wip.commit is not None
                and not await self.root.try_restore(wip.commit)
            ):
                wip = None
            previous = self.root.revision
            outcome = await self._evaluate(
                workspace=self.root,
                sessions=self.root_sessions,
                task=_CandidateTask(
                    generation=0,
                    child=attempt,
                    parent=None,
                    inspirations=(),
                    cold_start=True,
                    repair_seed=wip is not None,
                ),
            )
            if not outcome.passed:
                revision = self.root.revision
                if revision == previous:
                    revision = None
                outcome = outcome.model_copy(update={"commit": revision})
            individual, population = self.search.admit(self.state.population, outcome)
            self.state = self.state.model_copy(update={"population": population})
            if individual.commit is not None:
                await self.root.retain(
                    individual.commit,
                    label=(
                        f"individual-{individual.id}"
                        if individual.passed
                        else f"wip-seed-{individual.id}"
                    ),
                )
            await self._commit(f"evolve: record bootstrap attempt {attempt}")
            if individual.passed:
                return True
        return False

    async def _run_generation(self, generation: int) -> None:
        if self.state.generation_start is None:
            population = self.search.end_generation(self.state.population)
            self.state = self.state.model_copy(
                update={
                    "population": population,
                    "generation_start": population,
                    "admitted_slots": 0,
                }
            )
            await self._commit(f"evolve: begin generation {generation}")
        generation_start = self.state.generation_start
        if generation_start is None:
            message = "generation start must be durable before evaluation"
            raise RuntimeError(message)
        proposals = self._proposals(generation_start)
        pending = range(self.state.admitted_slots + 1, self.options.children_per_generation + 1)
        if self.options.max_parallelism > 1 and self.run.workspaces.supports_parallel_candidates:
            outcomes = await self._parallel(generation, proposals, tuple(pending))
            for slot in pending:
                await self._admit(generation, slot, outcomes.get(slot))
        else:
            for slot in pending:
                await self.run.control.checkpoint()
                proposal = proposals[slot - 1]
                outcome = (
                    await self._serial_candidate(generation, slot, proposal)
                    if proposal is not None
                    else None
                )
                await self._admit(generation, slot, outcome)
        self.state = self.state.model_copy(update={"generation_start": None, "admitted_slots": 0})
        await self._commit(f"evolve: complete generation {generation}")

    def _proposals(self, start: PopulationState) -> list[Proposal | None]:
        state = start
        proposals: list[Proposal | None] = []
        for _ in range(self.options.children_per_generation):
            proposal, state = self.search.propose(state)
            proposals.append(proposal)
        return proposals

    async def _serial_candidate(
        self, generation: int, slot: int, proposal: Proposal
    ) -> CandidateOutcome | None:
        parent = proposal.parent
        if parent.commit is None or not await self.root.try_restore(parent.commit):
            return None
        return await self._evaluate(
            workspace=self.root,
            sessions=self.root_sessions,
            task=_CandidateTask(
                generation=generation,
                child=slot,
                parent=parent,
                inspirations=proposal.inspirations,
                cold_start=False,
                policy_parent_id=proposal.policy_parent_id,
                target_island=proposal.target_island,
            ),
        )

    async def _parallel(
        self,
        generation: int,
        proposals: list[Proposal | None],
        pending: tuple[int, ...],
    ) -> dict[int, CandidateOutcome]:
        semaphore = asyncio.Semaphore(self.options.max_parallelism)

        async def one(slot: int) -> tuple[int, CandidateOutcome | None]:
            proposal = proposals[slot - 1]
            if proposal is None or proposal.parent.commit is None:
                return slot, None
            async with semaphore:
                return slot, await self._isolated_candidate(generation, slot, proposal)

        tasks: list[asyncio.Task[tuple[int, CandidateOutcome | None]]] = []
        try:
            async with asyncio.TaskGroup() as group:
                tasks.extend(group.create_task(one(slot)) for slot in pending)
        except BaseExceptionGroup as error:
            if len(error.exceptions) == 1:
                raise error.exceptions[0] from None
            raise
        results = [task.result() for task in tasks]
        return {slot: outcome for slot, outcome in results if outcome is not None}

    async def _isolated_candidate(
        self, generation: int, slot: int, proposal: Proposal
    ) -> CandidateOutcome:
        candidate = await self.run.workspaces.create_candidate(proposal.parent.commit)
        sessions = None
        try:
            sessions = await _open_sessions(self.run, candidate)
            return await self._evaluate(
                workspace=candidate,
                sessions=sessions,
                task=_CandidateTask(
                    generation=generation,
                    child=slot,
                    parent=proposal.parent,
                    inspirations=proposal.inspirations,
                    cold_start=False,
                    policy_parent_id=proposal.policy_parent_id,
                    target_island=proposal.target_island,
                ),
            )
        finally:
            primary = sys.exception()
            errors: list[BaseException] = []
            if sessions is not None and (error := await _capture_cleanup(sessions.close())):
                errors.append(error)
            if error := await _capture_cleanup(candidate.discard()):
                errors.append(error)
            if primary is not None:
                for error in errors:
                    primary.add_note(f"candidate cleanup also failed: {error}")
            elif errors:
                raise BaseExceptionGroup(_CANDIDATE_CLEANUP_ERROR, errors)

    async def _admit(self, generation: int, slot: int, outcome: CandidateOutcome | None) -> None:
        individual = None
        if outcome is not None:
            individual, population = self.search.admit(self.state.population, outcome)
            self.state = self.state.model_copy(update={"population": population})
        self.state = self.state.model_copy(update={"admitted_slots": slot})
        label = (
            f"evolve: record individual {individual.id}"
            if individual is not None
            else f"evolve: skip g{generation}c{slot}"
        )
        await self._commit(label)

    async def _evaluate(
        self,
        *,
        workspace: Workspace,
        sessions: _Sessions,
        task: _CandidateTask,
    ) -> CandidateOutcome:
        mutation = await self._mutate(
            sessions.mutator,
            parent=task.parent,
            inspirations=task.inspirations,
            cold_start=task.cold_start,
            repair_seed=task.repair_seed,
        )
        verdict = await self._judge(sessions.judge)
        feedback = verdict.feedback if verdict.verdict is Verdict.FAIL else None
        benchmark = None
        if feedback is None:
            accuracy = await self.run.evaluation.accuracy(workspace)
            feedback = accuracy.feedback
        if feedback is None and self.run.facts.benchmark_configured:
            benchmark = await self.run.evaluation.benchmark(workspace, objectives=self.objectives)
            feedback = benchmark.feedback
        if feedback is not None:
            return CandidateOutcome(
                passed=False,
                parent_id=task.parent.id if task.parent is not None else None,
                inspiration_ids=tuple(item.id for item in task.inspirations),
                summary=mutation.summary,
                feedback=feedback,
                policy_parent_id=task.policy_parent_id,
                target_island=task.target_island,
            )
        profile = await self._profile(sessions.profiler)
        label = "gen-0-seed" if task.cold_start else f"gen-{task.generation}-child-{task.child}"
        revision = await workspace.snapshot(label)
        metric, unit, metrics = self._fitness(profile, benchmark)
        code = await self.run.workspaces.export_patch(revision) if self.search.needs_code else None
        return CandidateOutcome(
            passed=True,
            parent_id=task.parent.id if task.parent is not None else None,
            inspiration_ids=tuple(item.id for item in task.inspirations),
            summary=mutation.summary,
            feedback=verdict.feedback,
            commit=revision,
            perf_metric=metric,
            perf_unit=unit,
            metrics=metrics,
            policy_parent_id=task.policy_parent_id,
            target_island=task.target_island,
            code=code,
        )

    async def _mutate(
        self,
        session: AgentSession,
        *,
        parent: Individual | None,
        inspirations: tuple[Individual, ...],
        cold_start: bool,
        repair_seed: bool,
    ) -> MutatorResponse:
        context = MutatorContext(
            accuracy_command=self.run.facts.accuracy_command,
            benchmark_command=self.run.facts.benchmark_command,
            domain_implementer=render_domain_section(
                self.domain, DomainRole.IMPLEMENTER, **self._domain_context()
            ),
            failed_lessons=self.search.failure_lessons(self.state.population),
            inspirations=list(inspirations),
            interface=_INTERFACE,
            is_cold_start=cold_start,
            modality=self.options.modality,
            num_failed_attempts=sum(not item.passed for item in self.state.population.individuals),
            objective=self.run.facts.objective,
            objectives=None,
            parent=parent,
            reference_path=self.run.facts.reference_location,
            repair_seed=repair_seed,
            runtime_notes=self.run.facts.environment_notes,
        )
        try:
            return await session.turn(render_mutator(context), response=MutatorResponse)
        except StructuredResponseError:
            return _fallback_mutator()

    async def _judge(self, session: AgentSession) -> JudgeResponse:
        context = CandidateJudgeContext(
            accuracy_command=self.run.facts.accuracy_command,
            benchmark_command=self.run.facts.benchmark_command,
            domain_judge=render_domain_section(
                self.domain, DomainRole.JUDGE, **self._domain_context()
            ),
            interface=_INTERFACE,
            modality=self.options.modality,
            objective=self.run.facts.objective,
            pass_criteria=_CANDIDATE_REQUIREMENTS,
            runtime_notes=self.run.facts.environment_notes,
        )
        try:
            return await session.turn(render_judge(context), response=JudgeResponse)
        except StructuredResponseError:
            return _fallback_judge()

    async def _profile(self, session: AgentSession | None) -> ProfilerSummary | None:
        kind = ProfilerKind(self.run.facts.profiler_id)
        if kind is ProfilerKind.NONE or session is None:
            return None
        definition = profiler_definition(kind)
        objectives = "\n".join(
            f"- `{item.name}` ({'maximize' if item.direction == 'max' else 'minimize'})"
            for item in self.options.metric_space.objectives
        )
        addendum = _PARETO_PROFILER_ADDENDUM.format(objective_list=objectives) if objectives else ""
        context = CandidateProfilerContext(
            benchmark_command=self.run.facts.benchmark_command,
            domain_profiler=render_domain_section(
                self.domain, DomainRole.PROFILER, **self._domain_context()
            ),
            modality=self.options.modality,
            objective=self.run.facts.objective,
            pareto_objectives_addendum=addendum,
            profile_execution=self.run.facts.profile_execution.value,
            profile_focus=(
                "Measure the headline metric for this candidate; rank top kernel-level bottlenecks."
            ),
            profiler_mcp_name=definition.mcp_name,
            profiler_support_name=definition.support_name,
            runtime_notes=self.run.facts.environment_notes,
        )
        try:
            return await session.turn(
                render_profiler(kind.value, context), response=ProfilerSummary
            )
        except StructuredResponseError:
            return _fallback_profiler()
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-920437 [BLE001]; profiling is advisory and provider failures are not normalized to one stable runtime exception yet; restricting this catch would make an optional profile abort accepted candidates.
            self.run.observations.warning(f"profiler failed: {error}")
            return None

    def _domain_context(self) -> dict[str, object]:
        facts = self.run.facts
        return {
            "modality": self.options.modality,
            "interface": _INTERFACE,
            "reference_path": facts.reference_location,
            "benchmark_command": facts.benchmark_command,
            "accuracy_command": facts.accuracy_command,
            "runtime_notes": facts.environment_notes,
            "profile_execution": facts.profile_execution.value,
            "workspace_sources": tuple(item.model_dump() for item in facts.workspace_sources),
        }

    @staticmethod
    def _fitness(
        profile: ProfilerSummary | None,
        benchmark: BenchmarkEvaluation | None,
    ) -> tuple[float | None, str | None, dict[str, float]]:
        metrics = dict(profile.metrics) if profile and profile.metrics else {}
        if benchmark is not None and benchmark.metric_value is not None:
            trusted = (
                dict(benchmark.row)
                if benchmark.row is not None
                else {benchmark.metric_name: benchmark.metric_value}
                if benchmark.metric_name is not None
                else {}
            )
            return (
                benchmark.metric_value,
                benchmark.metric_unit or (profile.perf_unit if profile else None),
                metrics | trusted,
            )
        return (
            profile.perf_metric if profile else None,
            profile.perf_unit if profile else None,
            metrics,
        )


async def orchestrate(run: Run, raw_options: BaseModel) -> RunStatus:
    """Run one evolutionary search campaign."""
    return await _EvolveRun(run, EvolveOptions.model_validate(raw_options)).execute()


__all__ = ["orchestrate"]
