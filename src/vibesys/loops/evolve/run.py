"""Evolve-owned operations used by the small orchestration scheduler.

One generation's control flow: derive every child slot's :class:`Proposal`
up front from the generation-start :class:`~vibesys.search.population.models.PopulationState`
(a pure, deterministic replay of ``PopulationSearch.propose``), evaluate the
slots not yet admitted (serially, or in a bounded parallel pool when the
environment supports it), then admit outcomes sequentially in slot order,
committing durable state after each admit. A resumed run recomputes the same
proposals from the same generation-start snapshot and only evaluates the
slots ``EvolveState.admitted_slots`` has not already accounted for, so no
admitted candidate is ever re-evaluated.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vibesys.domains.registry import resolve_domain
from vibesys.evaluators.gates import BenchmarkContract
from vibesys.evaluators.metrics import MetricSpace
from vibesys.events import FrameworkSource
from vibesys.loops.evolve.loop import (
    _bootstrap_seed,
    _discard_working_tree,
    _evaluate_candidate,
    _evaluate_in_subcontext,
    _persist_evolve_state,
)
from vibesys.loops.evolve.state import EvolutionStateStore, EvolveState
from vibesys.search.population.models import (
    CandidateOutcome,
    Individual,
    OpenEvolveSelectorConfig,
    PopulationConfig,
    PopulationState,
    Proposal,
)
from vibesys.search.population.search import PopulationSearch
from vs_agent.api import CandidateProgress

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vibesys.domains.base import DomainDefinition
    from vibesys.loops.evolve.orchestration import EvolveOptions
    from vibesys.orchestration.runtime import RunContext
    from vibesys.runtime import AgentHandle


_CANDIDATE_CRITERIA = (
    "The candidate obeys the input bundle's contract, the accuracy "
    "command passes, and the benchmark sanity step completes without "
    "modifying evaluator-owned files."
)


def _openevolve_config(options: EvolveOptions) -> OpenEvolveSelectorConfig | None:
    configured = (
        options.openevolve_population_size,
        options.openevolve_archive_size,
        options.openevolve_num_islands,
        options.openevolve_migration_interval,
        options.openevolve_migration_rate,
    )
    if all(value is None for value in configured):
        return None
    defaults = OpenEvolveSelectorConfig()
    return OpenEvolveSelectorConfig(
        population_size=options.openevolve_population_size or defaults.population_size,
        archive_size=options.openevolve_archive_size or defaults.archive_size,
        num_islands=options.openevolve_num_islands or defaults.num_islands,
        migration_interval=options.openevolve_migration_interval or defaults.migration_interval,
        migration_rate=options.openevolve_migration_rate
        if options.openevolve_migration_rate is not None
        else defaults.migration_rate,
    )


def _resolve_selector(
    options: EvolveOptions, existing: EvolveState | None
) -> tuple[Literal["vibesys", "openevolve"], OpenEvolveSelectorConfig | None]:
    """Pick the selector for this run: explicit request, else inferred.

    Resuming an existing run whose ``compare_resume`` policy already forbids
    changing any ``search_policy``/``openevolve_*`` field (other than
    ``max_generations``) means the CLI-resolved config here always matches
    what produced the persisted state, so no separate "recorded config"
    lookup is needed on resume.
    """
    config = _openevolve_config(options)
    requested = options.search_policy
    if requested is None:
        infer_openevolve = config is not None or (
            existing is not None and existing.population.selector_state is not None
        )
        return ("openevolve" if infer_openevolve else "vibesys"), config
    if requested == "vibesys" and config is not None:
        raise ValueError("OpenEvolve configuration requires the OpenEvolve search policy")  # noqa: TRY003
    return requested, config


@dataclass(slots=True)
class EvolveRun:
    """One opened run's durable search state and candidate execution settings."""

    host: RunContext
    options: EvolveOptions
    agents: dict[str, AgentHandle]
    state_store: EvolutionStateStore
    search: PopulationSearch
    state: EvolveState
    domain: DomainDefinition
    benchmark: BenchmarkContract
    objective: str

    @classmethod
    async def open(cls, host: RunContext, options: EvolveOptions) -> EvolveRun:
        """Load committed state and bind one search policy to this run."""
        request = host.request
        bundle = request.input_bundle
        space = options.metric_space
        state_store = EvolutionStateStore(host.state.namespace)
        existing = state_store.load()
        recorded_space = state_store.load_metric_space()
        if recorded_space not in {space, MetricSpace()}:
            raise ValueError("recorded evolve metric space differs from the run descriptor")  # noqa: TRY003
        selector, openevolve_config = _resolve_selector(options, existing)
        config = PopulationConfig(
            space=space,
            selection_temperature=options.selection_temperature,
            frontier_bias=options.frontier_bias,
            k_top_inspirations=options.k_top_inspirations,
            k_random_inspirations=options.k_random_inspirations,
            selector=selector,
            openevolve=openevolve_config,
            seed=options.seed,
        )
        search = PopulationSearch(config)
        state = existing if existing is not None else EvolveState(population=search.initial())
        state_store.save_metric_space(space)
        await _persist_evolve_state(
            host, state_store, state, label="evolve: initialize search state"
        )
        benchmark = BenchmarkContract(
            result_spec=bundle.benchmark_result,
            result_protocol=bundle.benchmark_result_protocol,
            timeout_seconds=bundle.manifest.benchmark.timeout_seconds,
        )
        objective = request.objective or bundle.objective
        pareto = None
        if space.objectives:
            axes = ", ".join(f"{axis.name}({axis.direction})" for axis in space.objectives)
            pareto = (
                f"[{axes}], frontier_bias={options.frontier_bias}, "
                f"tolerance={space.relative_noise:.0%}"
            )
        host.run_configured(
            run_log_path=str(host.environment.run_log_path),
            project_root=str(host.workspaces.root.path),
            objective=objective,
            search_policy=selector,
            benchmark_contract=benchmark.declared,
            pareto_objectives=pareto,
        )
        agents = {
            role: await host.agents.spawn(host.agents.default_definition(role))
            for role in ("implementer", "judge", "profiler")
        }
        return cls(
            host=host,
            options=options,
            agents=agents,
            state_store=state_store,
            search=search,
            state=state,
            domain=resolve_domain(bundle.domain),
            benchmark=benchmark,
            objective=objective,
        )

    @property
    def parallel(self) -> bool:
        """Return whether the selected environment can isolate candidates."""
        supported = self.host.environment.view.supports_parallel_candidate_evaluation
        return self.options.max_parallelism > 1 and supported

    @property
    def next_generation(self) -> int:
        """The generation number this run should evaluate next.

        Whether a generation is freshly starting or being resumed
        mid-flight, resume this same in-progress generation instead of
        skipping past it.
        """
        if self.state.generation_start is not None:
            return self.state.generation_start.generation
        return self.state.population.generation + 1

    def report_parallel_mode(self) -> None:
        """Explain when requested concurrency falls back to serial work."""
        if self.options.max_parallelism > 1 and not self.parallel:
            self.host.log(
                f"[parallel] --max-parallelism={self.options.max_parallelism} "
                "ignored: this environment cannot isolate candidates"
            )

    async def persist(self, *, label: str) -> None:
        """Commit the exact durable state this run currently holds."""
        await _persist_evolve_state(self.host, self.state_store, self.state, label=label)

    async def bootstrap(self) -> Individual | None:
        """Produce a verified generation-zero seed, repairing WIP on resume."""
        seed, state = await _bootstrap_seed(
            self.host,
            self.agents,
            objective=self.objective,
            space=self.options.metric_space,
            modality=self.options.modality,
            domain_definition=self.domain,
            pass_criteria=_CANDIDATE_CRITERIA,
            max_attempts=self.options.bootstrap_max_attempts,
            state_store=self.state_store,
            search=self.search,
            state=self.state,
            keep_deployments=self.options.keep_deployments,
            accuracy_timeout_seconds=self.host.request.input_bundle.manifest.accuracy.timeout_seconds,
            benchmark_contract=self.benchmark,
        )
        self.state = state
        return seed

    async def begin_generation(self, generation: int) -> None:
        """Advance to *generation* and snapshot its generation-start state.

        Advancing ``population.generation`` here (rather than after every
        slot is admitted) is what makes ``Individual.generation`` read
        *generation* for every child admitted this generation:
        ``PopulationSearch.admit`` tags each individual with the state's
        current generation counter. A durable ``generation_start`` is also
        what lets a resumed process recompute every child slot's proposal
        deterministically without replaying anything that happened earlier
        this generation.
        """
        if self.state.generation_start is None:
            population = self.search.end_generation(self.state.population)
            self.state = self.state.model_copy(
                update={
                    "population": population,
                    "generation_start": population,
                    "admitted_slots": 0,
                }
            )
            await self.persist(label=f"evolve: begin generation {generation}")
        passed = sum(
            1
            for individual in self.state.population.individuals
            if individual.passed and individual.commit
        )
        self.host.switch_log(f"gen{generation:03d}")
        self.host.log(
            f"\n{'=' * 60}\n  Generation {generation}/{self.options.max_generations} — "
            f"population={len(self.state.population.individuals)} (passed={passed})\n"
            f"{'=' * 60}\n"
        )

    @contextmanager
    def candidate_progress(self, generation: int, child_idx: int) -> Iterator[None]:
        """Attribute one serial candidate's turns and gates to its slot."""
        progress = CandidateProgress(
            generation,
            self.options.max_generations,
            child_idx,
            self.options.children_per_generation,
        )
        with self.host.agents.progress(progress):
            self.host.log(f"\n--- {progress.label()} ---\n")
            yield

    def _proposals(self, generation_start: PopulationState) -> list[Proposal | None]:
        """Deterministically replay every child slot's proposal.

        Pure and side-effect-free: given the same ``generation_start`` this
        always returns the same list, so it is safe to recompute on every
        resume rather than persist.
        """
        state = generation_start
        proposals: list[Proposal | None] = []
        for _ in range(self.options.children_per_generation):
            proposal, state = self.search.propose(state)
            proposals.append(proposal)
        return proposals

    async def run_generation(self, generation: int) -> None:
        """Evaluate and admit every not-yet-admitted slot of one generation."""
        await self.begin_generation(generation)
        assert self.state.generation_start is not None  # noqa: S101  # set by begin_generation
        proposals = self._proposals(self.state.generation_start)
        total = self.options.children_per_generation
        pending = range(self.state.admitted_slots + 1, total + 1)
        if self.parallel:
            targets: list[tuple[int, Proposal]] = []
            for slot in pending:
                proposal = proposals[slot - 1]
                if proposal is None:
                    continue
                if proposal.parent.commit is None:
                    self.host.warning(
                        f"parent {proposal.parent.id} has no commit; cannot isolate "
                        f"candidate g{generation}c{slot}; skipping",
                        source=FrameworkSource.LOOP,
                    )
                    continue
                targets.append((slot, proposal))
            outcomes = await self._evaluate_parallel_pool(generation, targets)
            for slot in pending:
                await self._admit_slot(generation, slot, outcomes.get(slot))
        else:
            for slot in pending:
                await self.host.control.boundary()
                proposal = proposals[slot - 1]
                outcome: CandidateOutcome | None = None
                if proposal is not None:
                    with self.candidate_progress(generation, slot):
                        outcome = await self._evaluate_serial(generation, slot, proposal)
                await self._admit_slot(generation, slot, outcome)
        await self._end_generation(generation)

    async def _evaluate_serial(
        self, generation: int, child_idx: int, proposal: Proposal
    ) -> CandidateOutcome | None:
        """Check the parent out in the shared workspace, then evaluate."""
        parent = proposal.parent
        parent_commit = parent.commit
        try:
            if parent_commit:
                await self.host.workspaces.root.restore(parent_commit, clean=True)
        except Exception:  # noqa: BLE001  # preserve the skipped-candidate policy
            self.host.warning(
                f"could not check out parent {parent.id} "
                f"(commit {parent_commit[:8] if parent_commit else 'n/a'}); skipping candidate",
                source=FrameworkSource.LOOP,
            )
            return None
        return await _evaluate_candidate(
            self.host,
            self.agents,
            generation=generation,
            child_idx=child_idx,
            parent=parent,
            inspirations=list(proposal.inspirations),
            objective=self.objective,
            space=self.options.metric_space,
            modality=self.options.modality,
            domain_definition=self.domain,
            pass_criteria=_CANDIDATE_CRITERIA,
            keep_deployments=self.options.keep_deployments,
            policy_parent_id=proposal.policy_parent_id,
            target_island=proposal.target_island,
            accuracy_timeout_seconds=self.host.request.input_bundle.manifest.accuracy.timeout_seconds,
            benchmark_contract=self.benchmark,
            needs_code=self.search.needs_code,
        )

    async def _evaluate_parallel_pool(
        self, generation: int, targets: list[tuple[int, Proposal]]
    ) -> dict[int, CandidateOutcome]:
        """Run the bounded pool of isolated-worktree evaluations."""
        if not targets:
            return {}
        cap = min(self.options.max_parallelism, len(targets))
        self.host.log(
            f"[parallel] generation {generation}: evaluating {len(targets)} "
            f"candidate(s), up to {cap} concurrently"
        )
        semaphore = asyncio.Semaphore(cap)

        async def evaluate(child_idx: int, proposal: Proposal) -> tuple[int, CandidateOutcome]:
            async with semaphore:
                outcome = await _evaluate_in_subcontext(
                    self.host,
                    generation=generation,
                    child_idx=child_idx,
                    parent=proposal.parent,
                    inspirations=list(proposal.inspirations),
                    objective=self.objective,
                    space=self.options.metric_space,
                    modality=self.options.modality,
                    domain_definition=self.domain,
                    pass_criteria=_CANDIDATE_CRITERIA,
                    keep_deployments=self.options.keep_deployments,
                    policy_parent_id=proposal.policy_parent_id,
                    target_island=proposal.target_island,
                    accuracy_timeout_seconds=self.host.request.input_bundle.manifest.accuracy.timeout_seconds,
                    benchmark_contract=self.benchmark,
                    needs_code=self.search.needs_code,
                )
                return child_idx, outcome

        return dict(await asyncio.gather(*(evaluate(slot, proposal) for slot, proposal in targets)))

    async def _admit_slot(
        self, generation: int, child_idx: int, outcome: CandidateOutcome | None
    ) -> None:
        """Admit one durable outcome (or skip) and advance the slot cursor.

        Committing here, sequentially in slot order, is the run's sole
        crash-recovery boundary: a resumed process only re-evaluates slots
        past ``admitted_slots``, so a slot recorded here is never redone.
        """
        individual: Individual | None = None
        if outcome is not None:
            individual, new_population = self.search.admit(self.state.population, outcome)
            self.state = self.state.model_copy(
                update={"population": new_population, "admitted_slots": child_idx}
            )
            self._log_admission(generation, individual, outcome)
            if not self.parallel and not outcome.passed:
                await _discard_working_tree(self.host)
        else:
            self.state = self.state.model_copy(update={"admitted_slots": child_idx})
        label = (
            f"evolve: record individual {individual.id}"
            if individual is not None
            else f"evolve: skip g{generation}c{child_idx}"
        )
        await self.persist(label=label)

    def _log_admission(
        self, generation: int, individual: Individual, outcome: CandidateOutcome
    ) -> None:
        if outcome.passed:
            metrics_repr = (
                " ".join(f"{key}={value:g}" for key, value in individual.metrics.items())
                if individual.metrics
                else f"{individual.perf_metric} {individual.perf_unit or ''}"
            )
            self.host.log(
                f"[Gen {generation}] Cand {individual.id} PASSED — "
                f"{metrics_repr} (parent={outcome.parent_id})"
            )
        else:
            feedback = (outcome.feedback or "").splitlines()
            self.host.log(
                f"[Gen {generation}] Cand {individual.id} FAILED — "
                f"feedback: {feedback[0][:120] if feedback else ''}"
            )

    async def _end_generation(self, generation: int) -> None:
        """Close out the generation once every slot is accounted for.

        The generation counter itself already advanced in ``begin_generation``
        (see its docstring); this only clears the in-progress markers so
        ``next_generation`` moves on.
        """
        self.state = self.state.model_copy(update={"generation_start": None, "admitted_slots": 0})
        await self.persist(label=f"evolve: complete generation {generation}")

    def report_final(self, best: Individual | None) -> None:
        """Report the Pareto frontier and chosen scalar candidate."""
        space = self.options.metric_space
        if space.objectives:
            frontier = self.search.frontier(self.state.population)
            self.host.log(f"\nFinal Pareto frontier ({len(frontier)} individuals):")
            for individual in frontier:
                readings = " ".join(
                    f"{axis.name}={individual.metrics[axis.name]:g}"
                    if axis.name in individual.metrics
                    else f"{axis.name}=n/a"
                    for axis in space.objectives
                )
                self.host.log(
                    f"  #{individual.id}: {readings} "
                    f"(commit {individual.commit[:8] if individual.commit else 'n/a'})"
                )
        if best is None:
            self.host.log("\nNo passing individual produced. Inspect logs.")
            return
        self.host.log(
            f"\nFinal scalar-best: individual #{best.id} "
            f"perf={best.perf_metric} {best.perf_unit or ''} "
            f"(commit {best.commit[:8] if best.commit else 'n/a'})"
        )
