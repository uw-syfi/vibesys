"""LLM-driven evolutionary search loop.

Each *generation* produces ``children_per_generation`` offspring. For
every offspring:

  1. Sample a parent from the passed-population, weighted by perf_metric.
  2. Sample a small set of peer "inspirations" so the mutator sees
     diverse strategies, not just the current best.
  3. Check the workspace out to the parent's commit.
  4. Run the *Mutator* agent (an LLM acting as the mutation operator) to
     edit code in place.
  5. Run the *Judge* on the result.
  6. If the judge passes, run the framework-owned accuracy command.
  7. If both pass, profile to obtain ``perf_metric``. Commit the workspace and
     record an Individual. Else: discard the dirty tree, record a failed
     Individual carrying failure feedback so future mutators can learn.

Before the generation loop, a dedicated *bootstrap* phase
(``_bootstrap_seed``) runs implementer → judge → accuracy iterations until
the first framework-verified implementation exists, recorded as a
generation-0 seed. So the generation loop always starts from a passing
parent and never cold-starts. The bootstrap phase owns the from-scratch /
fix-forward repair logic; on ``--resume`` it is skipped when a passing
individual is already present.

The loop intentionally does NOT have an early-stop signal — generations
run for the full ``max_generations`` budget. Termination decisions are
left to the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader

from vibesys.domains.base import DomainDefinition, DomainRole
from vibesys.domains.rendering import render_domain_section
from vibesys.evaluators.gates import (
    BenchmarkContract,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
)
from vibesys.events import FrameworkSource
from vibesys.loops.evolve.policy_flow import (
    BootstrapAttemptResult,
    CandidateOutcome,
)
from vibesys.loops.evolve.population import (
    Individual,
    Population,
)
from vibesys.loops.evolve.search_policy import (
    OpenEvolveSearchConfig,
    OpenEvolveSearchPolicy,
    SearchPolicy,
    SearchPolicyName,
    VibeSysSearchPolicy,
)
from vibesys.orchestration.runtime import MeasurementOptions
from vibesys.profilers import ProfilerKind, mcp_spec, profiler_definition
from vibesys.prompts import PROMPTS_DIR
from vibesys.render.sink import output_sink
from vibesys.schemas import JudgeResponse, MutatorResponse, ProfilerSummary, Verdict

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys.evaluators.metrics import MetricSpace, Objective
    from vibesys.loops.evolve.state import EvolutionStateStore
    from vibesys.orchestration.runtime import RunContext, WorkspaceHandle
    from vibesys.runtime import AgentHandle

_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "evolve"
_INTERFACE = "inprocess"

# Shared "no contract declared" default; the dataclass is frozen, so one
# instance is safe as a keyword default.
_NO_BENCHMARK_CONTRACT = BenchmarkContract()


class _AccuracyTimeoutMismatchError(ValueError):
    def __init__(self) -> None:
        super().__init__("evolve accuracy timeout differs from the run manifest")


# Evolve owns its role prompts, modality fragments, and profiler prompts.
# Domain role files are rendered separately and injected into the templates.
_jinja_env = Environment(  # noqa: S701  # lint-waiver: LW-010209 [S701]; these are plain-text agent prompts, and HTML escaping would alter their instructions.
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _render(name: str, **kwargs: object) -> str:
    return _jinja_env.get_template(name).render(**kwargs)


async def _persist_evolve_state(
    ctx: RunContext, state_store: EvolutionStateStore, *, label: str
) -> None:
    """Commit the exact durable evolutionary-search state tree."""
    cursor = state_store.load_cursor()
    sequence = max(
        cursor.active.generation if cursor and cursor.active else cursor.completed if cursor else 0,
        1,
    )
    await ctx.state.checkpoint(
        sequence=sequence,
        writes=state_store.checkpoint_writes(),
        publish=state_store.projection(),
    )
    ctx.log(f"[checkpoint] {label}")


def _domain_render_context(
    ctx: RunContext,
    modality: str | None,
    *,
    runtime_notes: str | None = None,
    scope: WorkspaceHandle | None = None,
) -> dict[str, object]:
    """Return the uniform context understood by every domain role file."""
    return {
        "modality": modality,
        "interface": _INTERFACE,
        "reference_path": ctx.environment.reference_path,
        "benchmark_command": ctx.environment.view_for(scope).paths.benchmark_command,
        "accuracy_command": ctx.environment.view_for(scope).paths.accuracy_command,
        "runtime_notes": (
            runtime_notes
            if runtime_notes is not None
            else ctx.environment.view_for(scope).prompt_notes
        ),
        "profile_execution": ctx.environment.view_for(scope).profile_execution,
        "workspace_sources": ctx.environment.workspace_sources,
    }


# ---------------------------------------------------------------------------
# Git helpers (dirty-tree discard)
# ---------------------------------------------------------------------------


async def _discard_working_tree(ctx: RunContext) -> None:
    """Drop any uncommitted changes left by a failed mutation attempt."""
    try:
        await ctx.workspaces.root.restore("HEAD", clean=True)
    except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010256 [BLE001]; cleanup warnings must not replace an earlier candidate failure.
        output_sink().framework_warning(
            "discard working tree failed",
            detail=str(exc),
            source=FrameworkSource.LOOP,
        )


async def _candidate_code(ctx: RunContext, commit: str) -> str:
    """Canonical multi-file patch used as OpenEvolve's program representation."""
    return await ctx.workspaces.root.candidate_patch(commit)


async def _teardown_candidate_deployment(
    ctx: RunContext, deployment: str | None, *, keep: bool
) -> None:
    """Release a candidate's per-evaluation deployment once its judge/profiler are done.

    An environment may isolate candidate runtime state in a named deployment so
    the judge never reads a prior candidate's cumulative logs. Once evaluation
    is over, hand that deployment back to the run environment. The adapter owns
    the release mechanism; the loop remains backend-agnostic.

    No-op when ``keep`` is set for post-hoc inspection or when ``deployment`` is
    None (the selected environment has no per-candidate deployment).
    """
    if keep or not deployment:
        return
    await ctx.environment.teardown_deployment(deployment)


# ---------------------------------------------------------------------------
# Profiler MCP wiring (reused from orchestrate; kept here to avoid an
# import-time dependency on the orchestrate loop)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------


def _recent_failure_lessons(
    population: Population, *, limit: int = 3, max_chars: int = 700
) -> list[str]:
    """Distinct feedback from the most-recent failed individuals.

    While the population has no passing parent, every child is a cold start
    that re-writes the server from scratch. Without this memory the search
    repeats the same bug on every seed (e.g. an identical model-init crash),
    burning generations while the population stays empty. Surfacing the recent
    distinct failure feedback lets each new seed avoid traps earlier seeds hit.

    De-duplicates on a normalized prefix so N identical failures collapse to a
    single lesson, and truncates each to keep the prompt bounded.
    """
    seen: set[str] = set()
    lessons: list[str] = []
    for ind in reversed(population.all):  # most recent first
        if ind.passed:
            continue
        fb = (ind.feedback or "").strip()
        if not fb:
            continue
        key = " ".join(fb[:160].lower().split())
        if key in seen:
            continue
        seen.add(key)
        lessons.append(fb if len(fb) <= max_chars else fb[:max_chars].rstrip() + " …")
        if len(lessons) >= limit:
            break
    return lessons


def _latest_wip_seed(population: Population) -> Individual | None:
    """Most-recent failed cold-start seed whose work was snapshotted.

    While the population has no passing parent, each round is a cold start.
    Rather than throw the failed seed away and rebuild from scratch every
    round (which makes the search re-hit the same bug forever, unable to
    bootstrap its first green candidate), we snapshot each failed seed to a
    WIP commit and let the next cold start *repair it in place* — fix-forward
    instead of restart. This returns that most-recent WIP seed so its tree can
    be checked out as the base for the next attempt.

    A WIP seed is a failed individual (``passed=False``) with ``parent_id is
    None`` that nonetheless carries a ``commit`` (its snapshotted tree).
    """
    for ind in reversed(population.all):  # most recent first
        if not ind.passed and ind.parent_id is None and ind.commit:
            return ind
    return None


def _candidate_runtime_notes(
    ctx: RunContext, generation: int, child_idx: int, *, scope: WorkspaceHandle | None = None
) -> tuple[str, str | None]:
    """Return runtime notes scoped to one environment-owned deployment.

    Environments with named deployment isolation derive the concrete candidate
    name and encode it in their runtime notes. Environments without that
    capability return their notes unchanged.
    """
    runtime = ctx.environment.candidate_runtime(generation, child_idx, scope=scope)
    return runtime.prompt_notes, runtime.deployment_name


async def _run_mutator(  # noqa: PLR0913  # lint-waiver: LW-020007 [PLR0913]; these are independent per-candidate inputs with no shared owner object; grouping them into a parameter object is deferred.
    ctx: RunContext,
    agents: dict[str, AgentHandle],
    *,
    generation: int,
    child_idx: int,
    objective: str,
    parent: Individual | None,
    inspirations: list[Individual],
    modality: str | None,
    domain_definition: DomainDefinition,
    is_cold_start: bool,
    space: MetricSpace,
    failed_lessons: list[str] | None = None,
    num_failed_attempts: int = 0,
    repair_seed: bool = False,
    runtime_notes: str | None = None,
    scope: WorkspaceHandle | None = None,
) -> MutatorResponse:
    prompt_runtime_notes = (
        runtime_notes if runtime_notes is not None else ctx.environment.view_for(scope).prompt_notes
    )
    domain_implementer = render_domain_section(
        domain_definition,
        DomainRole.IMPLEMENTER,
        **_domain_render_context(ctx, modality, runtime_notes=prompt_runtime_notes, scope=scope),
    )
    system_prompt = _render(
        "mutator_prompt.j2",
        reference_path=ctx.environment.reference_path,
        modality=modality,
        objective=objective,
        parent=parent,
        inspirations=inspirations,
        is_cold_start=is_cold_start,
        space=space,
        interface=_INTERFACE,
        domain_implementer=domain_implementer,
        runtime_notes=prompt_runtime_notes,
        profile_execution=ctx.environment.view_for(scope).profile_execution,
        accuracy_command=ctx.environment.view_for(scope).paths.accuracy_command,
        benchmark_command=ctx.environment.view_for(scope).paths.benchmark_command,
        failed_lessons=failed_lessons or [],
        num_failed_attempts=num_failed_attempts,
        repair_seed=repair_seed,
    )
    return await agents["implementer"].turn_structured(
        (
            "Edit the workspace to produce an offspring of the parent. "
            "Then return one JSON object matching the schema above."
        ),
        system_prompt=system_prompt,
        response_cls=MutatorResponse,
        fallback_factory=lambda: MutatorResponse(
            summary="Mutator produced no structured response.",
            hypothesis="unknown",
            expected_behavior="unknown",
        ),
        label=f"gen-{generation}-cand-{child_idx}-mutator",
    )


async def _run_judge(  # noqa: PLR0913  # lint-waiver: LW-020008 [PLR0913]; these are independent per-candidate inputs with no shared owner object; grouping them into a parameter object is deferred.
    ctx: RunContext,
    agents: dict[str, AgentHandle],
    *,
    generation: int,
    child_idx: int,
    modality: str | None,
    domain_definition: DomainDefinition,
    objective: str,
    pass_criteria: str,
    runtime_notes: str | None = None,
    scope: WorkspaceHandle | None = None,
) -> JudgeResponse:
    prompt_runtime_notes = (
        runtime_notes if runtime_notes is not None else ctx.environment.view_for(scope).prompt_notes
    )
    domain_judge = render_domain_section(
        domain_definition,
        DomainRole.JUDGE,
        **_domain_render_context(ctx, modality, runtime_notes=prompt_runtime_notes, scope=scope),
    )
    system_prompt = _render(
        "judge_prompt.j2",
        accuracy_command=ctx.environment.view_for(scope).paths.accuracy_command,
        benchmark_command=ctx.environment.view_for(scope).paths.benchmark_command,
        pass_criteria=pass_criteria,
        modality=modality,
        interface=_INTERFACE,
        domain_judge=domain_judge,
        runtime_notes=prompt_runtime_notes,
        profile_execution=ctx.environment.view_for(scope).profile_execution,
        objective=objective,
    )
    return await agents["judge"].turn_structured(
        "Review the offspring per the criteria above. Return only the JSON verdict.",
        system_prompt=system_prompt,
        response_cls=JudgeResponse,
        fallback_factory=lambda: JudgeResponse(
            analysis="Judge produced no structured response.",
            feedback="No structured response received.",
            verdict=Verdict.FAIL,
        ),
        label=f"gen-{generation}-cand-{child_idx}-judge",
    )


_PARETO_PROFILER_ADDENDUM = """\

## Pareto-frontier mode — emit *all* configured metrics

This run is in Pareto-frontier mode. In addition to the headline `perf_metric` / `perf_unit`, populate the `metrics` field of `ProfilerSummary` with the numeric value of EVERY objective listed below — read each one from the benchmark tool's JSON output, do not derive, do not invert.

Objectives to report (use these exact key names in `metrics`):

{objective_list}

If the benchmark JSON does not contain a field, set its entry to `null` rather than substituting a derived number — the framework will treat the offspring as missing on that axis and exclude it from the frontier (which is correct: an unmeasured axis cannot be compared).
"""


def _format_objectives_for_profiler(objectives: Sequence[Objective]) -> str:
    return "\n".join(
        f"- `{o.name}` ({'maximize' if o.direction == 'max' else 'minimize'})" for o in objectives
    )


async def _run_profiler(  # noqa: PLR0913  # lint-waiver: LW-020009 [PLR0913]; these are independent per-candidate inputs with no shared owner object; grouping them into a parameter object is deferred.
    ctx: RunContext,
    agents: dict[str, AgentHandle],
    *,
    generation: int,
    child_idx: int,
    modality: str | None,
    domain_definition: DomainDefinition,
    objective: str,
    space: MetricSpace,
    runtime_notes: str | None = None,
    scope: WorkspaceHandle | None = None,
) -> ProfilerSummary | None:
    kind = ctx.environment.profiler_kind
    if kind is ProfilerKind.NONE:
        return None
    definition = profiler_definition(kind)
    template = definition.prompt_template
    prompt_runtime_notes = (
        runtime_notes if runtime_notes is not None else ctx.environment.view_for(scope).prompt_notes
    )
    domain_profiler = render_domain_section(
        domain_definition,
        DomainRole.PROFILER,
        **_domain_render_context(ctx, modality, runtime_notes=prompt_runtime_notes, scope=scope),
    )
    base_prompt = _render(
        template,
        benchmark_command=ctx.environment.view_for(scope).paths.benchmark_command,
        modality=modality,
        interface=_INTERFACE,
        domain_profiler=domain_profiler,
        runtime_notes=prompt_runtime_notes,
        profile_execution=ctx.environment.view_for(scope).profile_execution,
        objective=objective,
        profile_focus="Measure the headline metric for this candidate; rank top kernel-level bottlenecks.",
        profiler_support_name=definition.support_name,
        profiler_mcp_name=definition.mcp_name,
    )
    if space.objectives:
        addendum = _PARETO_PROFILER_ADDENDUM.format(
            objective_list=_format_objectives_for_profiler(space.objectives),
        )
        system_prompt = base_prompt + addendum
    else:
        system_prompt = base_prompt
    spec = mcp_spec(kind)
    label = f"gen-{generation}-cand-{child_idx}-profiler"
    try:
        return await agents["profiler"].turn_structured(
            "Profile the server and return exactly one JSON object matching the schema above.",
            system_prompt=system_prompt,
            response_cls=ProfilerSummary,
            fallback_factory=lambda: ProfilerSummary(
                analysis="Profiler produced no structured response.",
                bottlenecks="n/a",
                suggestions="n/a",
                perf_metric=None,
                perf_unit=None,
            ),
            label=label,
            mcp_servers=[spec] if spec is not None else None,
        )
    except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010265 [BLE001]; configured profiler failures become framework warnings so the run continues without profile data.
        output_sink().framework_warning(
            "profiler failed", detail=str(exc), source=FrameworkSource.LOOP, round_label=label
        )
        return None


_CandidateOutcome = CandidateOutcome


async def _run_framework_accuracy_gate(
    ctx: RunContext,
    *,
    generation: int,
    child_idx: int,
    timeout_seconds: int | None = None,
    scope: WorkspaceHandle | None = None,
) -> str | None:
    """Run the immutable accuracy command and return retry feedback on failure."""
    if (
        timeout_seconds is not None
        and timeout_seconds != ctx.request.input_bundle.manifest.accuracy.timeout_seconds
    ):
        raise _AccuracyTimeoutMismatchError
    result = await ctx.evaluator.check(
        process_id=f"evolve-accuracy-{generation}-{child_idx}",
        label=f"gen-{generation}-cand-{child_idx}",
        scope=scope,
    )
    return result.feedback


async def _run_framework_benchmark_gate(
    ctx: RunContext,
    *,
    generation: int,
    child_idx: int,
    space: MetricSpace,
    scope: WorkspaceHandle | None = None,
) -> BenchmarkGateResult:
    """Run the declared trusted benchmark result contract for one candidate.

    The gate publishes the same typed ``gate_started``/``gate_finished``
    events the agent loop publishes, so a client watching an evolve run sees
    the framework's measurement of a candidate rather than only the agent
    transcripts around it.
    """
    return await ctx.evaluator.measure(
        output_slug=f"gen{generation}-cand{child_idx}",
        scope=scope,
        options=MeasurementOptions(
            objectives=space.objectives,
            label=f"gen-{generation}-cand-{child_idx}",
        ),
    )


async def _run_candidate_gates(  # noqa: PLR0913  # lint-waiver: LW-020010 [PLR0913]; these are independent per-candidate inputs with no shared owner object; grouping them into a parameter object is deferred.
    ctx: RunContext,
    *,
    generation: int,
    child_idx: int,
    contract: BenchmarkContract,
    space: MetricSpace,
    accuracy_timeout_seconds: int | None,
    scope: WorkspaceHandle | None = None,
) -> tuple[str | None, FrameworkBenchmarkOutcome | None]:
    """Run the accuracy gate, then the benchmark contract when one is declared.

    Returns ``(failure_feedback, benchmark)``. ``failure_feedback`` is ``None``
    when every gate passed; ``benchmark`` is set only when a declared contract
    ran and passed, and it carries the trusted measurement.
    """
    failure_feedback = await _run_framework_accuracy_gate(
        ctx,
        generation=generation,
        child_idx=child_idx,
        timeout_seconds=accuracy_timeout_seconds,
        scope=scope,
    )
    if failure_feedback is not None or not contract.declared:
        return failure_feedback, None
    gate = await _run_framework_benchmark_gate(
        ctx,
        generation=generation,
        child_idx=child_idx,
        space=space,
        scope=scope,
    )
    if not gate.passed:
        return gate.outcome.feedback, None
    return None, gate.outcome


def _candidate_fitness(
    summary: ProfilerSummary | None,
    benchmark: FrameworkBenchmarkOutcome | None,
) -> tuple[float | None, str | None, dict[str, float]]:
    """Resolve a candidate's recorded fitness: trusted benchmark over profiler.

    A declared benchmark result contract owns the axes it measures; the
    profiler's self-report owns the rest. The trusted row is therefore merged
    *over* the profiler's row rather than replacing it: the scalar contract
    reports one number, so on a two-axis task replacing the row would leave
    every individual incomplete on the second axis and
    :meth:`Population.frontier` -- which keeps only individuals carrying a
    value for every configured axis -- would return nothing.

    The unit comes from the evaluator's own declaration when the result
    protocol supplies one; ``objectives.toml`` names axes but does not say
    what they are measured in, so the profiler's unit is the fallback.
    """
    metrics = dict(summary.metrics) if summary and summary.metrics else {}
    if (
        benchmark is not None
        and benchmark.metric_name is not None
        and benchmark.metric_value is not None
    ):
        trusted = (
            dict(benchmark.row)
            if benchmark.row
            else {benchmark.metric_name: benchmark.metric_value}
        )
        return (
            benchmark.metric_value,
            benchmark.metric_unit or (summary.perf_unit if summary else None),
            metrics | trusted,
        )
    return (
        summary.perf_metric if summary else None,
        summary.perf_unit if summary else None,
        metrics,
    )


async def _evaluate_candidate(  # noqa: PLR0913  # lint-waiver: LW-020011 [PLR0913]; these are independent per-candidate inputs with no shared owner object; grouping them into a parameter object is deferred.
    ctx: RunContext,
    agents: dict[str, AgentHandle],
    *,
    generation: int,
    child_idx: int,
    parent: Individual,
    inspirations: list[Individual],
    objective: str,
    space: MetricSpace,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    keep_deployments: bool,
    policy_parent_id: str | None = None,
    target_island: int | None = None,
    isolated_deployment: bool = False,
    accuracy_timeout_seconds: int | None = None,
    benchmark_contract: BenchmarkContract = _NO_BENCHMARK_CONTRACT,
    scope: WorkspaceHandle | None = None,
) -> _CandidateOutcome:
    """Bind the candidate policy to this context's agents, gates, and workspace."""
    if isolated_deployment:
        cand_notes = ctx.environment.view_for(scope).prompt_notes
        cand_deployment = ctx.environment.view_for(scope).deployment_namespace
    else:
        cand_notes, cand_deployment = _candidate_runtime_notes(
            ctx, generation, child_idx, scope=scope
        )
    ctx.log(
        f"parent=#{parent.id} (perf={parent.perf_metric})"
        + (f" deployment={cand_deployment}" if cand_deployment else "")
        + f"; inspirations={[i.id for i in inspirations]}"
    )

    def outcome(
        *,
        passed: bool,
        summary: str,
        feedback: str | None,
        commit: str | None = None,
        fitness: tuple[float | None, str | None, dict[str, float]] | None = None,
    ) -> _CandidateOutcome:
        metric, unit, metrics = fitness if fitness is not None else (None, None, {})
        return _CandidateOutcome(
            passed=passed,
            parent_id=parent.id,
            inspiration_ids=[individual.id for individual in inspirations],
            summary=summary,
            feedback=feedback,
            commit=commit,
            perf_metric=metric,
            perf_unit=unit,
            metrics=metrics,
            policy_parent_id=policy_parent_id,
            target_island=target_island,
        )

    try:
        await ctx.environment.reselect_device(scope=scope)
        response = await _run_mutator(
            ctx,
            agents,
            generation=generation,
            child_idx=child_idx,
            objective=objective,
            parent=parent,
            inspirations=inspirations,
            modality=modality,
            domain_definition=domain_definition,
            is_cold_start=False,
            space=space,
            runtime_notes=cand_notes,
            scope=scope,
        )
        await ctx.environment.reselect_device(scope=scope)
        verdict = await _run_judge(
            ctx,
            agents,
            generation=generation,
            child_idx=child_idx,
            modality=modality,
            domain_definition=domain_definition,
            objective=objective,
            pass_criteria=pass_criteria,
            runtime_notes=cand_notes,
            scope=scope,
        )
        if verdict.verdict != Verdict.PASS:
            return outcome(passed=False, summary=response.summary, feedback=verdict.feedback)
        gate_feedback, benchmark = await _run_candidate_gates(
            ctx,
            generation=generation,
            child_idx=child_idx,
            contract=benchmark_contract,
            space=space,
            accuracy_timeout_seconds=accuracy_timeout_seconds,
            scope=scope,
        )
        if gate_feedback is not None:
            return outcome(passed=False, summary=response.summary, feedback=gate_feedback)
        await ctx.environment.reselect_device(scope=scope)
        profile = await _run_profiler(
            ctx,
            agents,
            generation=generation,
            child_idx=child_idx,
            modality=modality,
            domain_definition=domain_definition,
            objective=objective,
            space=space,
            runtime_notes=cand_notes,
            scope=scope,
        )
        fitness = _candidate_fitness(profile, benchmark)
        workspace = scope or ctx.workspaces.root
        commit = await workspace.snapshot(f"gen-{generation}-child-{child_idx}")
        return outcome(
            passed=True,
            summary=response.summary,
            feedback=verdict.feedback,
            commit=commit,
            fitness=fitness,
        )
    finally:
        await _teardown_candidate_deployment(ctx, cand_deployment, keep=keep_deployments)


class _LoopSearchEffects:
    """Bind evolve search effects to the current run context and state store."""

    def __init__(self, ctx: RunContext, state_store: EvolutionStateStore) -> None:
        self.ctx = ctx
        self.state_store = state_store

    async def checkpoint(self, label: str) -> None:
        await _persist_evolve_state(self.ctx, self.state_store, label=label)

    def save_population(self, population: Population) -> None:
        self.state_store.save_population(population)

    async def retain_candidate(self, label: str, commit: str) -> None:
        await self.ctx.workspaces.root.retain(label, commit)

    async def candidate_code(self, commit: str) -> str:
        return await _candidate_code(self.ctx, commit)

    def log(self, message: str) -> None:
        self.ctx.log(message)

    def warn(self, message: str) -> None:
        output_sink().framework_warning(message, source=FrameworkSource.LOOP)


async def _evaluate_in_subcontext(  # noqa: PLR0913  # lint-waiver: LW-020012 [PLR0913]; these are independent per-candidate inputs with no shared owner object; grouping them into a parameter object is deferred.
    ctx: RunContext,
    *,
    generation: int,
    child_idx: int,
    parent: Individual,
    inspirations: list[Individual],
    objective: str,
    space: MetricSpace,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    keep_deployments: bool,
    policy_parent_id: str | None,
    target_island: int | None,
    accuracy_timeout_seconds: int | None = None,
    benchmark_contract: BenchmarkContract = _NO_BENCHMARK_CONTRACT,
) -> _CandidateOutcome:
    """Evaluate one candidate in a host-owned isolated workspace."""
    inspiration_ids = [i.id for i in inspirations]
    label = f"g{generation}c{child_idx}"
    commit = parent.commit
    if commit is None:
        output_sink().framework_warning(
            f"candidate {label} has no parent commit; skipping",
            source=FrameworkSource.LOOP,
        )
        return _CandidateOutcome(
            passed=False,
            parent_id=parent.id,
            inspiration_ids=inspiration_ids,
            summary="candidate has no parent commit",
            feedback="parent individual has no commit to branch from",
        )
    try:
        scope = await ctx.workspaces.fork(commit)
    except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010257 [BLE001]; arbitrary provider setup failures are reported as a failed candidate.
        output_sink().framework_warning(
            f"candidate {label} setup failed",
            detail=str(exc),
            source=FrameworkSource.LOOP,
        )
        return _CandidateOutcome(
            passed=False,
            parent_id=parent.id,
            inspiration_ids=inspiration_ids,
            summary="candidate setup failed",
            feedback=str(exc),
        )
    agents: dict[str, AgentHandle] = {}
    try:
        for role in ("implementer", "judge", "profiler"):
            definition = ctx.agents.default_definition(role)
            agents[role] = await ctx.agents.spawn(definition, scope=scope)
        outcome = await _evaluate_candidate(
            ctx,
            agents,
            generation=generation,
            child_idx=child_idx,
            parent=parent,
            inspirations=inspirations,
            objective=objective,
            space=space,
            modality=modality,
            domain_definition=domain_definition,
            pass_criteria=pass_criteria,
            keep_deployments=keep_deployments,
            policy_parent_id=policy_parent_id,
            target_island=target_island,
            isolated_deployment=True,
            accuracy_timeout_seconds=accuracy_timeout_seconds,
            benchmark_contract=benchmark_contract,
            scope=scope,
        )
        if outcome.commit:
            # Subcontext teardown removes the linked worktree. Retain its
            # detached commit first so durable population state cannot name an
            # object that Git is then free to prune.
            await ctx.workspaces.root.retain(label, outcome.commit)
    except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010258 [BLE001]; evaluator failures become candidate outcomes so the generation can continue.
        output_sink().framework_warning(
            f"candidate {label} evaluation raised",
            detail=str(exc),
            source=FrameworkSource.LOOP,
        )
        return _CandidateOutcome(
            passed=False,
            parent_id=parent.id,
            inspiration_ids=inspiration_ids,
            summary="candidate evaluation raised",
            feedback=str(exc),
        )
    else:
        return outcome
    finally:
        try:
            await scope.discard()
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010259 [BLE001]; teardown failure is reported without hiding the candidate result.
            output_sink().framework_warning(
                f"candidate {label} teardown failed",
                detail=str(exc),
                source=FrameworkSource.LOOP,
            )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BootstrapPassingEvidence:
    summary: str
    feedback: str
    benchmark: FrameworkBenchmarkOutcome | None
    runtime_notes: str


@dataclass(slots=True)
class _BootstrapAdapter:
    """Bind one bootstrap attempt to the run's agents, gates, and state."""

    ctx: RunContext
    agents: dict[str, AgentHandle]
    objective: str
    space: MetricSpace
    modality: str | None
    domain_definition: DomainDefinition
    pass_criteria: str
    population: Population
    state_store: EvolutionStateStore
    search_policy: SearchPolicy
    keep_deployments: bool
    accuracy_timeout_seconds: int | None
    benchmark_contract: BenchmarkContract

    async def attempt(self, number: int, max_attempts: int) -> BootstrapAttemptResult:
        """Run and record one implementer, judge, gate, and profile attempt."""
        ctx = self.ctx
        ctx.log(f"\n--- bootstrap attempt {number}/{max_attempts} ---\n")
        wip_seed = await self._repair_seed()
        cand_notes, cand_deployment = _candidate_runtime_notes(ctx, 0, number)
        failed_lessons = _recent_failure_lessons(self.population)
        num_failed_attempts = sum(1 for individual in self.population.all if not individual.passed)
        base_desc = "reference" if wip_seed is None else f"repair-seed #{wip_seed.id}"
        ctx.log(
            f"bootstrap base={base_desc}"
            + (f" deployment={cand_deployment}" if cand_deployment else "")
        )
        try:
            await ctx.environment.reselect_device()
            mutator = await _run_mutator(
                ctx,
                self.agents,
                generation=0,
                child_idx=number,
                objective=self.objective,
                parent=None,
                inspirations=[],
                modality=self.modality,
                domain_definition=self.domain_definition,
                is_cold_start=True,
                space=self.space,
                failed_lessons=failed_lessons,
                num_failed_attempts=num_failed_attempts,
                repair_seed=wip_seed is not None,
                runtime_notes=cand_notes,
            )
            await ctx.environment.reselect_device()
            verdict = await _run_judge(
                ctx,
                self.agents,
                generation=0,
                child_idx=number,
                modality=self.modality,
                domain_definition=self.domain_definition,
                objective=self.objective,
                pass_criteria=self.pass_criteria,
                runtime_notes=cand_notes,
            )
            benchmark = None
            if verdict.verdict == Verdict.PASS:
                failure_feedback, benchmark = await _run_candidate_gates(
                    ctx,
                    generation=0,
                    child_idx=number,
                    contract=self.benchmark_contract,
                    space=self.space,
                    accuracy_timeout_seconds=self.accuracy_timeout_seconds,
                )
            else:
                failure_feedback = verdict.feedback
            if failure_feedback is not None:
                return await self._record_failure(number, mutator.summary, failure_feedback)
            return await self._record_seed(
                number,
                _BootstrapPassingEvidence(mutator.summary, verdict.feedback, benchmark, cand_notes),
            )
        finally:
            await _teardown_candidate_deployment(ctx, cand_deployment, keep=self.keep_deployments)

    async def _repair_seed(self) -> Individual | None:
        """Return the latest WIP seed if its tree can be checked out."""
        wip_seed = _latest_wip_seed(self.population)
        if wip_seed is not None and wip_seed.commit:
            try:
                await self.ctx.workspaces.root.restore(wip_seed.commit, clean=True)
            except Exception:  # noqa: BLE001  # lint-waiver: LW-020013 [BLE001]; any failure to restore the WIP seed falls back to rebuilding from the reference.
                output_sink().framework_warning(
                    f"could not check out WIP seed {wip_seed.id} "
                    f"(commit {wip_seed.commit[:8]}); starting from reference",
                    source=FrameworkSource.LOOP,
                )
                wip_seed = None
        return wip_seed

    async def _snapshot_wip(self, number: int) -> str | None:
        """Retain a failed tree only when the snapshot created a new commit."""
        try:
            sha_before = self.ctx.workspaces.root.revision
            sha_after = await self.ctx.workspaces.root.snapshot(f"wip-seed-bootstrap{number}")
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010260 [BLE001]; failed best-effort seed snapshot is reported while retaining the verified seed.
            output_sink().framework_warning(
                "wip-seed snapshot failed",
                detail=str(exc),
                source=FrameworkSource.LOOP,
            )
            return None
        else:
            return sha_after if sha_after and sha_after != sha_before else None

    async def _record_failure(
        self, number: int, summary: str, feedback: str
    ) -> BootstrapAttemptResult:
        """Record one failed attempt and checkpoint its optional WIP seed."""
        failed = Individual(
            id=self.population.next_id(),
            generation=0,
            parent_id=None,
            inspiration_ids=[],
            commit=await self._snapshot_wip(number),
            perf_metric=None,
            perf_unit=None,
            passed=False,
            summary=summary,
            feedback=feedback,
        )
        if failed.commit:
            await self.ctx.workspaces.root.retain(f"wip-seed-{failed.id}", failed.commit)
        self.population.add(failed)
        self.state_store.save_population(self.population)
        return BootstrapAttemptResult(
            seed=None,
            message=(
                f"[bootstrap {number}] FAILED — feedback: "
                f"{feedback.splitlines()[0][:120] if feedback else ''}"
            ),
        )

    async def _record_seed(
        self, number: int, evidence: _BootstrapPassingEvidence
    ) -> BootstrapAttemptResult:
        """Profile and checkpoint the first passing generation-zero seed."""
        ctx = self.ctx
        await ctx.environment.reselect_device()
        profile = await _run_profiler(
            ctx,
            self.agents,
            generation=0,
            child_idx=number,
            modality=self.modality,
            domain_definition=self.domain_definition,
            objective=self.objective,
            space=self.space,
            runtime_notes=evidence.runtime_notes,
        )
        commit = await ctx.workspaces.root.snapshot("gen-0-seed")
        perf_metric, perf_unit, metrics = _candidate_fitness(profile, evidence.benchmark)
        seed = Individual(
            id=self.population.next_id(),
            generation=0,
            parent_id=None,
            inspiration_ids=[],
            commit=commit,
            perf_metric=perf_metric,
            perf_unit=perf_unit,
            metrics=metrics,
            passed=True,
            summary=evidence.summary,
            feedback=evidence.feedback,
        )
        if commit:
            await ctx.workspaces.root.retain(f"individual-{seed.id}", commit)
        self.population.add(seed)
        self.state_store.save_population(self.population)
        if commit:
            self.search_policy.record(
                seed,
                code=await _candidate_code(ctx, commit) if self.search_policy.requires_code else "",
                policy_parent_id=None,
                target_island=None,
                space=self.space,
            )
        return BootstrapAttemptResult(
            seed=seed,
            message=(
                f"[bootstrap {number}] PASSED — seed #{seed.id} "
                f"perf={seed.perf_metric} {seed.perf_unit or ''} "
                f"(commit {commit[:8] if commit else 'n/a'})"
            ),
        )


async def _bootstrap_seed(  # noqa: PLR0913  # lint-waiver: LW-020014 [PLR0913]; these are independent per-candidate inputs with no shared owner object; grouping them into a parameter object is deferred.
    ctx: RunContext,
    agents: dict[str, AgentHandle],
    *,
    objective: str,
    space: MetricSpace,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    max_attempts: int,
    population: Population,
    state_store: EvolutionStateStore,
    search_policy: SearchPolicy,
    keep_deployments: bool = False,
    accuracy_timeout_seconds: int | None = None,
    benchmark_contract: BenchmarkContract = _NO_BENCHMARK_CONTRACT,
) -> Individual | None:
    """Retry and checkpoint generation-zero attempts until a seed passes."""
    ctx.switch_log("bootstrap")
    ctx.log(
        f"\n{'=' * 60}\n  Bootstrap — first passing seed "
        f"(up to {max_attempts} attempt(s))\n{'=' * 60}\n"
    )
    bootstrap = _BootstrapAdapter(
        ctx=ctx,
        agents=agents,
        objective=objective,
        space=space,
        modality=modality,
        domain_definition=domain_definition,
        pass_criteria=pass_criteria,
        population=population,
        state_store=state_store,
        search_policy=search_policy,
        keep_deployments=keep_deployments,
        accuracy_timeout_seconds=accuracy_timeout_seconds,
        benchmark_contract=benchmark_contract,
    )
    for number in range(1, max_attempts + 1):
        result = await bootstrap.attempt(number, max_attempts)
        label = (
            f"evolve: record bootstrap seed {result.seed.id}"
            if result.seed is not None
            else f"evolve: record failed bootstrap {number}"
        )
        await _persist_evolve_state(ctx, state_store, label=label)
        ctx.log(result.message)
        if result.seed is not None:
            return result.seed
    ctx.log(f"[bootstrap] exhausted {max_attempts} attempt(s) without a passing seed.")
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def _initialize_search_policy(  # noqa: PLR0913  # lint-waiver: LW-020015 [PLR0913]; these are independent per-candidate inputs with no shared owner object; grouping them into a parameter object is deferred.
    ctx: RunContext,
    population: Population,
    state_store: EvolutionStateStore,
    *,
    requested: SearchPolicyName | str | None,
    seed: int | None,
    config: OpenEvolveSearchConfig | None,
    space: MetricSpace,
) -> tuple[SearchPolicyName, SearchPolicy]:
    state_dir = state_store.namespace.external_directory("openevolve")
    if requested is None:
        policy_name = (
            SearchPolicyName.OPENEVOLVE
            if config is not None or OpenEvolveSearchPolicy.has_state(state_dir)
            else SearchPolicyName.VIBESYS
        )
    else:
        policy_name = SearchPolicyName(requested)
        if policy_name is SearchPolicyName.VIBESYS and config is not None:
            message = "OpenEvolve configuration requires the OpenEvolve search policy"
            raise ValueError(message)
    if policy_name is not SearchPolicyName.OPENEVOLVE:
        return policy_name, VibeSysSearchPolicy()

    policy = OpenEvolveSearchPolicy(
        state_dir=state_dir,
        seed=seed,
        config=config,
        space=space,
    )
    for individual in population.passed:
        if not individual.commit:
            continue
        policy.record(
            individual,
            code=await _candidate_code(ctx, individual.commit),
            policy_parent_id=(
                individual.policy_parent_id
                or (f"vibesys-{individual.parent_id}" if individual.parent_id is not None else None)
            ),
            target_island=individual.policy_target_island,
            space=space,
        )
    return policy_name, policy
