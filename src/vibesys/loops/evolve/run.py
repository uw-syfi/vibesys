"""Evolve-owned operations used by the small orchestration scheduler."""

# Cursor errors include the exact unsafe state at each recovery boundary.
# ruff: noqa: TRY003

from __future__ import annotations

import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from vibesys.domains.registry import resolve_domain
from vibesys.evaluators.gates import BenchmarkContract
from vibesys.evaluators.metrics import MetricSpace
from vibesys.events import FrameworkSource
from vibesys.loops.evolve.loop import (
    _bootstrap_seed,
    _discard_working_tree,
    _evaluate_candidate,
    _evaluate_in_subcontext,
    _initialize_search_policy,
    _LoopSearchEffects,
    _persist_evolve_state,
)
from vibesys.loops.evolve.policy_flow import (
    CandidateOutcome,
    EvolveSearch,
    SelectionSettings,
    parallel_enabled,
)
from vibesys.loops.evolve.search_policy import (
    OpenEvolveSearchConfig,
    SearchSelection,
)
from vibesys.loops.evolve.state import (
    CandidateOutcomeRecord,
    CandidatePlanRecord,
    EvolutionStateStore,
    EvolveResumeError,
    GenerationCursor,
    GenerationJournal,
)
from vibesys.render.sink import output_sink
from vs_agent.api import CandidateProgress

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vibesys.domains.base import DomainDefinition
    from vibesys.loops.evolve.orchestration import EvolveOptions
    from vibesys.loops.evolve.population import Individual, Population
    from vibesys.orchestration.runtime import RunContext
    from vibesys.run import LoopContext


_CANDIDATE_CRITERIA = (
    "The candidate obeys the input bundle's contract, the accuracy "
    "command passes, and the benchmark sanity step completes without "
    "modifying evaluator-owned files."
)


def _plan_record(plan: SearchSelection) -> CandidatePlanRecord:
    return CandidatePlanRecord(
        parent_id=plan.parent.id,
        inspiration_ids=tuple(individual.id for individual in plan.inspirations),
        policy_parent_id=plan.policy_parent_id,
        target_island=plan.target_island,
    )


def _outcome_record(outcome: CandidateOutcome) -> CandidateOutcomeRecord:
    return CandidateOutcomeRecord(
        passed=outcome.passed,
        parent_id=outcome.parent_id,
        inspiration_ids=tuple(outcome.inspiration_ids),
        summary=outcome.summary,
        feedback=outcome.feedback,
        commit=outcome.commit,
        perf_metric=outcome.perf_metric,
        perf_unit=outcome.perf_unit,
        metrics=dict(outcome.metrics),
        policy_parent_id=outcome.policy_parent_id,
        target_island=outcome.target_island,
    )


def _record_outcome(record: CandidateOutcomeRecord) -> CandidateOutcome:
    return CandidateOutcome(
        passed=record.passed,
        parent_id=record.parent_id,
        inspiration_ids=list(record.inspiration_ids),
        summary=record.summary,
        feedback=record.feedback,
        commit=record.commit,
        perf_metric=record.perf_metric,
        perf_unit=record.perf_unit,
        metrics=dict(record.metrics),
        policy_parent_id=record.policy_parent_id,
        target_island=record.target_island,
    )


def _openevolve_config(options: EvolveOptions) -> OpenEvolveSearchConfig | None:
    configured = (
        options.openevolve_population_size,
        options.openevolve_archive_size,
        options.openevolve_num_islands,
        options.openevolve_migration_interval,
        options.openevolve_migration_rate,
    )
    if all(value is None for value in configured):
        return None
    defaults = OpenEvolveSearchConfig()
    return OpenEvolveSearchConfig(
        population_size=options.openevolve_population_size or defaults.population_size,
        archive_size=options.openevolve_archive_size or defaults.archive_size,
        num_islands=options.openevolve_num_islands or defaults.num_islands,
        migration_interval=options.openevolve_migration_interval or defaults.migration_interval,
        migration_rate=options.openevolve_migration_rate
        if options.openevolve_migration_rate is not None
        else defaults.migration_rate,
    )


def _validate_cursor(
    cursor: GenerationCursor | None, population: Population, children_per_generation: int
) -> None:
    """Reject incomplete old state and any ambiguous paid-work boundary."""
    completed = cursor.completed if cursor is not None else 0
    active = cursor.active if cursor is not None else None
    if cursor is not None and cursor.rng_state is None:
        raise EvolveResumeError("evolve cursor lacks sampler state; replay would diverge")
    if active is None:
        if any(individual.generation > completed for individual in population.all):
            raise EvolveResumeError(
                "evolve state has candidates beyond its completed generation cursor; "
                "resuming could repeat paid work"
            )
        return
    if active.generation != completed + 1 or active.next_child > children_per_generation + 1:
        raise EvolveResumeError("evolve generation cursor does not match the run budget")
    if active.phase in {"planning", "evaluating"}:
        raise EvolveResumeError(
            f"evolve generation {active.generation} stopped during {active.phase}; "
            "the child may have consumed paid work without a durable result"
        )
    recorded_ids = set(active.recorded.values())
    population_ids = {
        individual.id for individual in population.all if individual.generation == active.generation
    }
    if population_ids != recorded_ids or any(
        slot < 1 or slot >= active.next_child for slot in active.recorded
    ):
        raise EvolveResumeError(
            "evolve population and child cursor disagree; resuming could duplicate candidates"
        )
    if active.phase == "recording" and any(slot not in active.outcomes for slot in active.plans):
        raise EvolveResumeError("evolve generation has missing paid candidate outcomes")


@dataclass(slots=True)
class EvolveRun:
    """One opened run's search state and candidate execution settings."""

    host: RunContext
    options: EvolveOptions
    ctx: LoopContext
    state_store: EvolutionStateStore
    population: Population
    search: EvolveSearch
    effects: _LoopSearchEffects
    domain: DomainDefinition
    benchmark: BenchmarkContract
    rng: random.Random
    objective: str

    @classmethod
    def open(cls, host: RunContext, options: EvolveOptions) -> EvolveRun:
        """Load committed state and initialize one search policy."""
        ctx = host.run_context
        request = host.request
        bundle = request.input_bundle
        space = options.metric_space
        state_store = EvolutionStateStore(ctx.state.portable("evolve"))
        population = state_store.load_population()
        cursor = state_store.load_cursor()
        if cursor is None and request.resume is not None:
            raise EvolveResumeError(
                "this evolve run has no child journal; its previous paid work cannot be replayed safely"
            )
        _validate_cursor(cursor, population, options.children_per_generation)
        rng = random.Random(options.seed)  # noqa: S311  # sampling, not security
        if cursor is None:
            state_store.save_cursor(GenerationCursor(rng_state=rng.getstate()))
        else:
            rng.setstate(cast("tuple[int, tuple[int, ...], float | None]", cursor.rng_state))
        recorded_space = state_store.load_metric_space()
        if recorded_space not in {space, MetricSpace()}:
            raise ValueError("recorded evolve metric space differs from the run descriptor")
        state_store.save_population(population)
        state_store.save_metric_space(space)
        policy_name, policy = _initialize_search_policy(
            ctx,
            population,
            state_store,
            requested=options.search_policy,
            seed=options.seed,
            config=_openevolve_config(options),
            space=space,
        )
        _persist_evolve_state(ctx, state_store, label="evolve: initialize search state")
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
        output_sink().run_configured(
            run_log_path=str(ctx.run_log_path),
            project_root=str(ctx.project_root),
            objective=objective,
            search_policy=policy_name.value,
            benchmark_contract=benchmark.declared,
            pareto_objectives=pareto,
        )
        return cls(
            host=host,
            options=options,
            ctx=ctx,
            state_store=state_store,
            population=population,
            search=EvolveSearch(population, policy, space),
            effects=_LoopSearchEffects(ctx, state_store),
            domain=resolve_domain(bundle.domain),
            benchmark=benchmark,
            rng=rng,
            objective=objective,
        )

    @property
    def parallel(self) -> bool:
        """Return whether the selected environment can isolate candidates."""
        supported = self.ctx.run_environment_view.supports_parallel_candidate_evaluation
        return parallel_enabled(self.options.max_parallelism, supported=supported)

    @property
    def first_generation(self) -> int:
        """Resume an unfinished generation at its next durable child slot."""
        cursor = self.state_store.load_cursor() or GenerationCursor()
        return cursor.active.generation if cursor.active is not None else cursor.completed + 1

    def _journal(self, generation: int) -> GenerationJournal:
        cursor = self.state_store.load_cursor()
        if cursor is None or cursor.active is None or cursor.active.generation != generation:
            raise EvolveResumeError(f"evolve generation {generation} has no active journal")
        return cursor.active

    def next_child(self, generation: int) -> int:
        """Return the first child slot not yet durably accounted for."""
        return self._journal(generation).next_child

    def _save_journal(self, journal: GenerationJournal, *, label: str) -> None:
        self.state_store.save_journal(journal.generation - 1, journal, self.rng.getstate())
        _persist_evolve_state(self.ctx, self.state_store, label=label)

    def _selection(self, record: CandidatePlanRecord) -> SearchSelection:
        individuals = {individual.id: individual for individual in self.population.all}
        try:
            return SearchSelection(
                parent=individuals[record.parent_id],
                inspirations=[individuals[id_] for id_ in record.inspiration_ids],
                policy_parent_id=record.policy_parent_id,
                target_island=record.target_island,
            )
        except KeyError as exc:
            raise EvolveResumeError("recorded evolve plan references a missing individual") from exc

    def report_parallel_mode(self) -> None:
        """Explain when requested concurrency falls back to serial work."""
        if self.options.max_parallelism > 1 and not self.parallel:
            self.ctx.lprint(
                f"[parallel] --max-parallelism={self.options.max_parallelism} "
                "ignored: this environment cannot isolate candidates"
            )

    def bootstrap(self) -> Individual | None:
        """Produce a verified generation-zero seed, repairing WIP on resume."""
        return _bootstrap_seed(
            self.ctx,
            objective=self.objective,
            space=self.options.metric_space,
            modality=self.options.modality,
            domain_definition=self.domain,
            pass_criteria=_CANDIDATE_CRITERIA,
            max_attempts=self.options.bootstrap_max_attempts,
            population=self.population,
            state_store=self.state_store,
            search_policy=self.search.search_policy,
            keep_deployments=self.options.keep_deployments,
            accuracy_timeout_seconds=self.host.request.input_bundle.manifest.accuracy.timeout_seconds,
            benchmark_contract=self.benchmark,
        )

    def begin_generation(self, generation: int) -> None:
        """Open or resume the generation's durable child journal."""
        cursor = self.state_store.load_cursor() or GenerationCursor()
        if cursor.active is None:
            if generation != cursor.completed + 1:
                raise EvolveResumeError("evolve generation would skip the committed budget cursor")
            journal = GenerationJournal(
                generation=generation, mode="parallel" if self.parallel else "serial"
            )
            self._save_journal(journal, label=f"evolve: begin generation {generation}")
        elif cursor.active.generation != generation or cursor.active.mode != (
            "parallel" if self.parallel else "serial"
        ):
            raise EvolveResumeError("evolve generation or evaluation mode changed on resume")
        self.ctx.switch_log_file(f"gen{generation:03d}")
        self.ctx.lprint(
            f"\n{'=' * 60}\n  Generation {generation}/{self.options.max_generations} — "
            f"population={len(self.population)} (passed={len(self.population.passed)})\n"
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
        with self.ctx.progress(progress):
            self.ctx.lprint(f"\n--- {progress.label()} ---\n")
            yield

    def _sample_candidate(self) -> SearchSelection | None:
        """Checkpoint the sampler before any paid candidate work."""
        settings = SelectionSettings(
            self.options.k_top_inspirations,
            self.options.k_random_inspirations,
            self.options.selection_temperature,
            self.options.frontier_bias,
        )
        return self.search.plan(self.effects, rng=self.rng, settings=settings)

    def plan_candidate(self, generation: int, child_idx: int) -> SearchSelection | None:
        """Recover a recorded selection or save a new one before evaluation."""
        journal = self._journal(generation)
        if child_idx != journal.next_child or journal.mode != "serial":
            raise EvolveResumeError("serial evolve child slot is out of order")
        if journal.phase in {"planned", "recording"}:
            return self._selection(journal.plans[child_idx])
        if journal.phase != "ready":
            raise EvolveResumeError("serial evolve child cannot be safely replanned")
        self._save_journal(
            journal.model_copy(update={"phase": "planning"}),
            label=f"evolve: plan g{generation}c{child_idx}",
        )
        plan = self._sample_candidate()
        if plan is None:
            self._save_journal(
                journal.model_copy(update={"next_child": child_idx + 1}),
                label=f"evolve: skip g{generation}c{child_idx}",
            )
            return None
        self._save_journal(
            journal.model_copy(
                update={"phase": "planned", "plans": {child_idx: _plan_record(plan)}}
            ),
            label=f"evolve: selected g{generation}c{child_idx}",
        )
        return plan

    def plan_generation(self, generation: int) -> list[tuple[int, SearchSelection]]:
        """Plan parallel children from one pre-generation population snapshot."""
        journal = self._journal(generation)
        if journal.mode != "parallel":
            raise EvolveResumeError("parallel plan requested for a serial generation")
        if journal.phase in {"planned", "recording"}:
            return [(slot, self._selection(plan)) for slot, plan in sorted(journal.plans.items())]
        if journal.phase != "ready" or journal.next_child != 1:
            raise EvolveResumeError("parallel evolve generation cannot be safely replanned")
        self._save_journal(
            journal.model_copy(update={"phase": "planning"}),
            label=f"evolve: plan generation {generation}",
        )
        plans: list[tuple[int, SearchSelection]] = []
        for child_idx in range(1, self.options.children_per_generation + 1):
            plan = self._sample_candidate()
            if plan is None:
                continue
            if plan.parent.commit is None:
                output_sink().framework_warning(
                    f"parent {plan.parent.id} has no commit; cannot isolate "
                    f"candidate g{generation}c{child_idx}; skipping",
                    source=FrameworkSource.LOOP,
                )
                continue
            plans.append((child_idx, plan))
        self._save_journal(
            journal.model_copy(
                update={
                    "phase": "planned",
                    "plans": {slot: _plan_record(plan) for slot, plan in plans},
                }
            ),
            label=f"evolve: selected generation {generation}",
        )
        return plans

    def evaluate_candidate(
        self, generation: int, child_idx: int, plan: SearchSelection
    ) -> CandidateOutcome | None:
        """Evaluate once, then durably stage the outcome before admission."""
        journal = self._journal(generation)
        if child_idx != journal.next_child or journal.mode != "serial":
            raise EvolveResumeError("serial evolve evaluation is out of order")
        if journal.phase == "recording":
            record = journal.outcomes.get(child_idx)
            return _record_outcome(record) if record is not None else None
        if journal.phase != "planned" or journal.plans.get(child_idx) != _plan_record(plan):
            raise EvolveResumeError("serial evolve plan differs from its durable selection")
        self._save_journal(
            journal.model_copy(update={"phase": "evaluating"}),
            label=f"evolve: evaluate g{generation}c{child_idx}",
        )
        parent = plan.parent
        if parent.commit and not self.ctx.git.checkout_tree(parent.commit, clean=True):
            output_sink().framework_warning(
                f"could not check out parent {parent.id} "
                f"(commit {parent.commit[:8]}); skipping candidate",
                source=FrameworkSource.LOOP,
            )
            outcome = None
        else:
            outcome = _evaluate_candidate(
                self.ctx,
                generation=generation,
                child_idx=child_idx,
                parent=parent,
                inspirations=plan.inspirations,
                objective=self.objective,
                space=self.options.metric_space,
                modality=self.options.modality,
                domain_definition=self.domain,
                pass_criteria=_CANDIDATE_CRITERIA,
                keep_deployments=self.options.keep_deployments,
                policy_parent_id=plan.policy_parent_id,
                target_island=plan.target_island,
                accuracy_timeout_seconds=self.host.request.input_bundle.manifest.accuracy.timeout_seconds,
                benchmark_contract=self.benchmark,
            )
        self._save_journal(
            journal.model_copy(
                update={
                    "phase": "recording",
                    "outcomes": {child_idx: _outcome_record(outcome)}
                    if outcome is not None
                    else {},
                }
            ),
            label=f"evolve: evaluated g{generation}c{child_idx}",
        )
        return outcome

    def evaluate_parallel(
        self, generation: int, plans: list[tuple[int, SearchSelection]]
    ) -> dict[int, CandidateOutcome]:
        """Evaluate isolated children once and durably stage all outcomes."""
        journal = self._journal(generation)
        if journal.mode != "parallel":
            raise EvolveResumeError("parallel evaluation requested for a serial generation")
        if journal.phase == "recording":
            return {
                slot: _record_outcome(outcome)
                for slot, outcome in journal.outcomes.items()
                if slot >= journal.next_child
            }
        if (
            journal.phase != "planned"
            or {slot: _plan_record(plan) for slot, plan in plans} != journal.plans
        ):
            raise EvolveResumeError("parallel evolve plans differ from their durable selection")
        self._save_journal(
            journal.model_copy(update={"phase": "evaluating"}),
            label=f"evolve: evaluate generation {generation}",
        )
        if not plans:
            outcomes: dict[int, CandidateOutcome] = {}
        else:
            outcomes = self._evaluate_parallel_pool(generation, plans)
        self._save_journal(
            journal.model_copy(
                update={
                    "phase": "recording",
                    "outcomes": {
                        slot: _outcome_record(outcome) for slot, outcome in outcomes.items()
                    },
                }
            ),
            label=f"evolve: evaluated generation {generation}",
        )
        return outcomes

    def _evaluate_parallel_pool(
        self, generation: int, plans: list[tuple[int, SearchSelection]]
    ) -> dict[int, CandidateOutcome]:
        """Run the bounded pool only after the in-flight marker is committed."""
        cap = min(self.options.max_parallelism, len(plans))
        self.ctx.lprint(
            f"[parallel] generation {generation}: evaluating {len(plans)} "
            f"candidate(s), up to {cap} concurrently"
        )
        worktree_lock = threading.Lock()
        outcomes: dict[int, CandidateOutcome] = {}
        with ThreadPoolExecutor(max_workers=cap, thread_name_prefix=f"gen{generation}") as pool:
            futures = {
                pool.submit(
                    _evaluate_in_subcontext,
                    self.ctx,
                    config=self.host.request.config,
                    agent_backend=self.host.request.agent_backend,
                    cli_provider=self.host.request.cli_provider,
                    generation=generation,
                    child_idx=child_idx,
                    parent=plan.parent,
                    inspirations=plan.inspirations,
                    objective=self.objective,
                    space=self.options.metric_space,
                    modality=self.options.modality,
                    domain_definition=self.domain,
                    pass_criteria=_CANDIDATE_CRITERIA,
                    keep_deployments=self.options.keep_deployments,
                    policy_parent_id=plan.policy_parent_id,
                    target_island=plan.target_island,
                    worktree_lock=worktree_lock,
                    accuracy_timeout_seconds=self.host.request.input_bundle.manifest.accuracy.timeout_seconds,
                    benchmark_contract=self.benchmark,
                ): child_idx
                for child_idx, plan in plans
            }
            for future in as_completed(futures):
                outcomes[futures[future]] = future.result()
        return outcomes

    def record_candidate(
        self,
        generation: int,
        child_idx: int,
        outcome: CandidateOutcome | None,
        *,
        serial: bool,
    ) -> Individual | None:
        """Admit one durable result and advance its child slot in the same checkpoint."""
        journal = self._journal(generation)
        if journal.phase != "recording" or journal.next_child != child_idx:
            raise EvolveResumeError("evolve candidate recording is out of order")
        record = journal.outcomes.get(child_idx)
        if (record is None) != (outcome is None) or (
            record is not None and outcome is not None and record != _outcome_record(outcome)
        ):
            raise EvolveResumeError("evolve result differs from its durable paid outcome")
        individual = None
        recorded = dict(journal.recorded)
        if outcome is not None:
            individual = self.search.record(outcome, generation=generation, effects=self.effects)
            recorded[child_idx] = individual.id
            if serial and not outcome.passed:
                _discard_working_tree(self.ctx)
        next_phase = "recording" if not serial else "ready"
        self.state_store.save_journal(
            generation - 1,
            journal.model_copy(
                update={
                    "phase": next_phase,
                    "next_child": child_idx + 1,
                    "plans": journal.plans if not serial else {},
                    "outcomes": journal.outcomes if not serial else {},
                    "recorded": recorded,
                }
            ),
            self.rng.getstate(),
        )
        _persist_evolve_state(
            self.ctx,
            self.state_store,
            label=(
                f"evolve: record individual {individual.id}"
                if individual is not None
                else f"evolve: skip g{generation}c{child_idx}"
            ),
        )
        return individual

    def complete_generation(self, generation: int) -> None:
        """Persist search-policy generation state after all outcomes."""
        journal = self._journal(generation)
        if journal.next_child != self.options.children_per_generation + 1 or journal.phase not in {
            "ready",
            "recording",
        }:
            raise EvolveResumeError(
                "evolve generation ended before every child slot was accounted for"
            )
        self.search.search_policy.finish_generation(generation)
        self.state_store.save_completed_generation(generation, self.rng.getstate())
        _persist_evolve_state(
            self.ctx, self.state_store, label=f"evolve: complete generation {generation}"
        )

    def report_final(self, best: Individual | None) -> None:
        """Report the Pareto frontier and chosen scalar candidate."""
        space = self.options.metric_space
        if space.objectives:
            frontier = self.population.frontier(space)
            self.ctx.lprint(f"\nFinal Pareto frontier ({len(frontier)} individuals):")
            for individual in frontier:
                readings = " ".join(
                    f"{axis.name}={individual.metrics[axis.name]:g}"
                    if axis.name in individual.metrics
                    else f"{axis.name}=n/a"
                    for axis in space.objectives
                )
                self.ctx.lprint(
                    f"  #{individual.id}: {readings} "
                    f"(commit {individual.commit[:8] if individual.commit else 'n/a'})"
                )
        if best is None:
            self.ctx.lprint("\nNo passing individual produced. Inspect logs.")
            return
        self.ctx.lprint(
            f"\nFinal scalar-best: individual #{best.id} "
            f"perf={best.perf_metric} {best.perf_unit or ''} "
            f"(commit {best.commit[:8] if best.commit else 'n/a'})"
        )
